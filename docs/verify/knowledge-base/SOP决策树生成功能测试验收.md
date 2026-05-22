---
status: active
category: verify
audience: developer
last_updated: 2026-05-22
version: v1.0
owner: team
---

# SOP 决策树自动生成功能 — 测试验收文档

> **功能概述**：在 SOP 文档审核通过时，自动解析 Markdown 内容生成结构化决策树（`sop_tree` 表），供 PAI-agent 按需获取。

---

## 一、验收标准

| 编号 | 标准 | 验证方式 |
|------|------|---------|
| AC-1 | 审核通过 SOP 文档时，自动生成 `sop_tree` 记录 | API 响应 + 数据库查询 |
| AC-2 | 决策树包含正确的叶节点数量和校验状态 | API 响应字段验证 |
| AC-3 | 解析失败（叶节点缺字段）时，`tree_generated=false`，文档仍可发布 | API 响应 + 日志 |
| AC-4 | 重新发布时覆盖旧树（幂等） | 重复调用 API |
| AC-5 | 已发布文档再次调用 approve 返回现有树信息 | API 响应 |

---

## 二、自动化测试脚本

### 2.1 前置条件

```bash
# 1. 确保服务运行
make dev-up

# 2. 获取 INTERNAL_API_TOKEN（从 .env 或 Helm Secret）
export INTERNAL_API_TOKEN="your-token-here"

# 3. 设置环境变量
export KB_URL="http://localhost:8004"
```

### 2.2 测试脚本：`test_sop_tree_generation.sh`

创建文件 `scripts/verify/test_sop_tree_generation.sh`：

```bash
#!/bin/bash
# SOP 决策树自动生成功能验收测试
# 用法：./test_sop_tree_generation.sh

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

# ─── 清理 ────────────────────────────────────────────────────────────────────
info "清理测试数据"
curl -s -X DELETE "${KB_URL}/api/kb/documents/${DOCUMENT_ID}" \
  -H "Authorization: Bearer ${TOKEN}" > /dev/null || true
curl -s -X DELETE "${KB_URL}/api/kb/documents/${DOCUMENT_ID3}" \
  -H "Authorization: Bearer ${TOKEN}" > /dev/null || true
rm -f /tmp/test_sop.md /tmp/test_sop_invalid.md

echo ""
echo "=========================================="
pass "所有测试用例通过！"
echo "=========================================="
```

### 2.3 运行测试脚本

```bash
# 创建脚本目录
mkdir -p scripts/verify

# 保存脚本
chmod +x scripts/verify/test_sop_tree_generation.sh

# 运行测试
./scripts/verify/test_sop_tree_generation.sh
```

---

## 三、人工测试页面操作

### 3.1 管理后台测试流程

**前置条件**：已登录管理后台，拥有 SOP 审核权限。

#### 步骤 1：上传 SOP 文档

1. 进入 **知识库管理** → **SOP 文档**
2. 点击 **上传文档** 按钮
3. 选择 `.docx` 或 `.md` 格式的 SOP 文档
4. 填写分类 ID（如：`虚拟机-003`）
5. 点击 **确认上传**

**预期结果**：
- 文档状态显示为 `draft`
- 显示文档 ID 和分块数量

#### 步骤 2：审核发布文档

1. 在文档列表中找到刚上传的文档
2. 点击 **审核** 按钮
3. 确认文档内容无误
4. 点击 **发布** 按钮

**预期结果**：
- 文档状态变为 `published`
- 响应中显示：
  - `tree_generated: true`
  - `tree_leaf_count: X`（X >= 1）
  - `tree_validation_status: valid` 或 `warnings`

#### 步骤 3：验证决策树生成

1. 点击已发布文档的 **详情** 按钮
2. 查看是否显示 **决策树** 标签页
3. 点击标签页，验证树结构展示

**预期结果**：
- 决策树以可视化方式展示
- 每个叶节点包含判断方法和解决方案
- 节点 ID 格式为 `n-1-2-3`

### 3.2 API 测试（curl）

#### 测试 1：获取决策树

```bash
curl -X GET "http://localhost:8004/api/sop/{document_id}/tree" \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN"
```

**预期响应**：
```json
{
  "node_id": "n-1",
  "name": "虚拟机开机失败",
  "level": 1,
  "children": [
    {
      "node_id": "n-1-1",
      "name": "Redis OOM 导致开机失败",
      "diagnosis": { ... },
      "solution": { ... }
    }
  ]
}
```

#### 测试 2：审核响应字段

```bash
curl -X POST "http://localhost:8004/api/admin/sop/{document_id}/approve" \
  -H "Authorization: Bearer $INTERNAL_API_TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"reviewer_id": 1}'
```

**预期响应**：
```json
{
  "success": true,
  "document_id": 1,
  "status": "published",
  "chunks_embedded": 2,
  "tree_generated": true,
  "tree_leaf_count": 2,
  "tree_validation_status": "valid",
  "published_at": "2026-05-22T10:00:00Z"
}
```

---

## 四、测试数据

### 4.1 有效 SOP 文档示例

```markdown
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
- 彻底解决方案：
  - 扩容 Redis 内存
```

### 4.2 无效 SOP 文档示例（缺少解决方案）

```markdown
# 测试场景

## 不完整案例

### 判断方法
- 页面判断方法：检查状态

<!-- 缺少解决方案 → 解析失败，tree_generated=false -->
```

---

## 五、验收检查清单

| 检查项 | 通过标准 | 实际结果 | 状态 |
|--------|---------|---------|------|
| 上传 SOP 文档 | 返回 document_id | | ☐ |
| 审核发布文档 | status=published | | ☐ |
| tree_generated 字段 | true（有效文档）| | ☐ |
| tree_leaf_count 字段 | >= 1 | | ☐ |
| tree_validation_status 字段 | valid/warnings | | ☐ |
| 获取决策树 API | 返回完整树 JSON | | ☐ |
| 幂等性验证 | 重复审核返回已有树 | | ☐ |
| 解析失败处理 | tree_generated=false | | ☐ |

---

## 六、相关文件

| 文件路径 | 说明 |
|---------|------|
| `backend/kb-service/app/routes/admin.py` | 审核接口实现 |
| `backend/kb-service/app/services/sop_parser.py` | 决策树解析器 |
| `backend/kb-service/app/models/sop_tree.py` | 数据模型 |
| `docs/solution/knowledge-base/SOP决策树生产消费流程.md` | 设计文档 |
