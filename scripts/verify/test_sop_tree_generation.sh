#!/bin/bash
# SOP 决策树自动生成功能验收测试
# 用法：./test_sop_tree_generation.sh
#
# 前置条件：
#   1. 服务运行：make dev-up
#   2. 设置环境变量：export INTERNAL_API_TOKEN="your-token"
#   3. 安装依赖：jq, curl

set -e

KB_URL="${KB_URL:-http://localhost:8004}"
TOKEN="${INTERNAL_API_TOKEN:-test-token}"

# 颜色输出
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}[PASS]${NC} $1"; }
fail() { echo -e "${RED}[FAIL]${NC} $1"; exit 1; }
info() { echo -e "${YELLOW}[INFO]${NC} $1"; }

cleanup() {
  info "清理测试数据..."
  if [ -n "$DOCUMENT_ID" ]; then
    curl -s -X DELETE "${KB_URL}/api/kb/documents/${DOCUMENT_ID}" \
      -H "Authorization: Bearer ${TOKEN}" > /dev/null 2>&1 || true
  fi
  if [ -n "$DOCUMENT_ID3" ]; then
    curl -s -X DELETE "${KB_URL}/api/kb/documents/${DOCUMENT_ID3}" \
      -H "Authorization: Bearer ${TOKEN}" > /dev/null 2>&1 || true
  fi
  rm -f /tmp/test_sop.md /tmp/test_sop_invalid.md
}
trap cleanup EXIT

echo "=========================================="
info "SOP 决策树自动生成功能验收测试"
echo "=========================================="
echo ""

# ─── 测试用例 1：上传并审核 SOP 文档 ────────────────────────────────────────
info "测试用例 1: 上传 SOP 文档并审核"

# 1.1 创建测试 SOP 文档
cat > /tmp/test_sop.md << 'EOF'
# 虚拟机开机失败

## Redis OOM 导致开机失败

### 判断方法
- 页面判断方法：
  - 查看 Redis 内存使用率 > 90%
  - 查看 /var/log/redis/redis.log 有 OOM 记录
- acli 命令行：
  - acli redis info memory

### 解决方案
- 快速恢复方案：
  - 重启 Redis 服务
  - 释放大 key
- 彻底解决方案：
  - 扩容 Redis 内存
  - 配置内存淘汰策略

## 存储不可访问导致开机失败

### 判断方法
- 页面判断方法：
  - 查看存储池状态为"异常"
  - 查看 ASAN 日志有 I/O 错误

### 解决方案
- 快速恢复方案：
  - 重启存储服务
- 彻底解决方案：
  - 检查存储网络连接
  - 更换故障磁盘
EOF

# 1.2 上传文档
info "上传测试 SOP 文档..."
UPLOAD_RESPONSE=$(curl -s -X POST "${KB_URL}/api/admin/sop/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "file=@/tmp/test_sop.md" \
  -F "category_id=虚拟机-003")

DOCUMENT_ID=$(echo "$UPLOAD_RESPONSE" | jq -r '.document_id // empty')
if [ -z "$DOCUMENT_ID" ] || [ "$DOCUMENT_ID" = "null" ]; then
  fail "上传文档失败: $UPLOAD_RESPONSE"
fi
info "文档 ID: $DOCUMENT_ID"

# ─── 测试用例 2：审核并验证决策树生成 ────────────────────────────────────────
info "测试用例 2: 审核文档并验证决策树生成"

APPROVE_RESPONSE=$(curl -s -X POST "${KB_URL}/api/admin/sop/${DOCUMENT_ID}/approve" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": 1}')

echo "$APPROVE_RESPONSE" | jq .

# 验证响应字段
TREE_GENERATED=$(echo "$APPROVE_RESPONSE" | jq -r '.tree_generated')
TREE_LEAF_COUNT=$(echo "$APPROVE_RESPONSE" | jq -r '.tree_leaf_count')
TREE_STATUS=$(echo "$APPROVE_RESPONSE" | jq -r '.tree_validation_status')
STATUS=$(echo "$APPROVE_RESPONSE" | jq -r '.status')

if [ "$TREE_GENERATED" != "true" ]; then
  fail "tree_generated 应为 true，实际为 $TREE_GENERATED"
fi
pass "tree_generated = true"

if [ "$TREE_LEAF_COUNT" -lt 1 ]; then
  fail "tree_leaf_count 应 >= 1，实际为 $TREE_LEAF_COUNT"
fi
pass "tree_leaf_count = $TREE_LEAF_COUNT"

if [ "$TREE_STATUS" != "valid" ] && [ "$TREE_STATUS" != "warnings" ]; then
  fail "tree_validation_status 应为 valid 或 warnings，实际为 $TREE_STATUS"
fi
pass "tree_validation_status = $TREE_STATUS"

if [ "$STATUS" != "published" ]; then
  fail "status 应为 published，实际为 $STATUS"
fi
pass "status = published"

# ─── 测试用例 3：验证 sop_tree 表记录 ────────────────────────────────────────
info "测试用例 3: 验证 sop_tree 表记录"

TREE_RESPONSE=$(curl -s "${KB_URL}/api/sop/${DOCUMENT_ID}/tree" \
  -H "Authorization: Bearer ${TOKEN}")

if [ -z "$TREE_RESPONSE" ] || [ "$TREE_RESPONSE" = "null" ]; then
  fail "获取决策树失败"
fi

NODE_ID=$(echo "$TREE_RESPONSE" | jq -r '.node_id // empty')
if [ -z "$NODE_ID" ]; then
  fail "决策树缺少 node_id"
fi
pass "决策树根节点 ID: $NODE_ID"

CHILDREN_COUNT=$(echo "$TREE_RESPONSE" | jq '.children | length')
if [ "$CHILDREN_COUNT" -lt 1 ]; then
  fail "决策树应至少有 1 个子节点"
fi
pass "决策树子节点数: $CHILDREN_COUNT"

# ─── 测试用例 4：幂等性验证（重新发布）────────────────────────────────────────
info "测试用例 4: 幂等性验证（重新发布）"

APPROVE_RESPONSE2=$(curl -s -X POST "${KB_URL}/api/admin/sop/${DOCUMENT_ID}/approve" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": 1}')

TREE_GENERATED2=$(echo "$APPROVE_RESPONSE2" | jq -r '.tree_generated')
if [ "$TREE_GENERATED2" != "true" ]; then
  fail "重复审核 tree_generated 应仍为 true"
fi
pass "幂等性验证通过：重复审核返回已有树"

# ─── 测试用例 5：解析失败处理（缺解决方案）────────────────────────────────────────
info "测试用例 5: 解析失败处理"

cat > /tmp/test_sop_invalid.md << 'EOF'
# 测试场景

## 缺少解决方案的案例

### 判断方法
- 页面判断方法：检查状态
EOF

UPLOAD_RESPONSE3=$(curl -s -X POST "${KB_URL}/api/admin/sop/upload" \
  -H "Authorization: Bearer ${TOKEN}" \
  -F "file=@/tmp/test_sop_invalid.md")

DOCUMENT_ID3=$(echo "$UPLOAD_RESPONSE3" | jq -r '.document_id // empty')
if [ -z "$DOCUMENT_ID3" ]; then
  fail "上传无效测试文档失败"
fi

APPROVE_RESPONSE3=$(curl -s -X POST "${KB_URL}/api/admin/sop/${DOCUMENT_ID3}/approve" \
  -H "Authorization: Bearer ${TOKEN}" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": 1}')

TREE_GENERATED3=$(echo "$APPROVE_RESPONSE3" | jq -r '.tree_generated')
STATUS3=$(echo "$APPROVE_RESPONSE3" | jq -r '.status')

if [ "$TREE_GENERATED3" != "false" ]; then
  fail "无效文档 tree_generated 应为 false"
fi
pass "解析失败时 tree_generated = false"

if [ "$STATUS3" != "published" ]; then
  fail "即使解析失败，文档仍应发布"
fi
pass "解析失败时文档仍发布"

echo ""
echo "=========================================="
pass "所有测试用例通过！"
echo "=========================================="
