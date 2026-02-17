# 快速测试指南

## 🚀 5分钟快速测试

### 前置准备
```bash
# 安装依赖
uv pip install pytest pytest-asyncio pytest-cov httpx

# 启动数据库
cd deploy/docker
docker-compose up -d postgres redis
```

### 1. 运行单元测试（无需服务运行）

```bash
cd /home/claude/hci-troubleshoot-platform

# 测试TraceID工具
pytest tests/unit/test_trace_id.py -v

# 预期输出：
# ✓ test_generate_trace_id_format
# ✓ test_generate_trace_id_unique
# ✓ test_validate_valid_trace_id
# ... 全部通过

# 测试Case Service业务逻辑
pytest tests/unit/test_case_service.py -v
```

### 2. 启动Case Service进行集成测试

**终端1 - 启动服务**:
```bash
cd backend/case-service

# 设置环境变量
export DATABASE_URL="postgresql+asyncpg://hci_user:hci_password@localhost:5432/hci_troubleshoot"

# 安装依赖
uv pip install -r requirements.txt

# 启动服务
uvicorn app.main:app --reload --port 8001
```

**终端2 - 运行测试**:
```bash
# 等待服务启动（3秒）
sleep 3

# 运行集成测试
pytest tests/integration/test_case_service_integration.py -v

# 或手动测试
curl -X POST http://localhost:8001/api/cases \
  -H "Content-Type: application/json" \
  -d '{
    "client_id": "quick-test",
    "title": "快速测试工单",
    "description": "5分钟测试"
  }' | jq
```

### 3. 验证结果

```bash
# 查看数据库
psql -h localhost -U hci_user -d hci_troubleshoot -c \
  "SELECT case_id, client_id, status, title FROM cases ORDER BY created_at DESC LIMIT 5;"

# 预期看到刚创建的测试工单
```

## 📊 测试覆盖率

```bash
# 生成覆盖率报告
pytest tests/unit/ --cov=backend/shared --cov=backend/case-service --cov-report=html

# 查看报告
open htmlcov/index.html
```

## 🎯 关键测试场景

### 场景1: 完整工单流程
```bash
# 1. 创建工单
CASE_ID=$(curl -s -X POST http://localhost:8001/api/cases \
  -H "Content-Type: application/json" \
  -d '{"client_id": "test-001", "title": "Test"}' | jq -r '.case_id')

# 2. 查询工单
curl -s http://localhost:8001/api/cases/$CASE_ID | jq

# 3. 确认工单
curl -s -X PUT http://localhost:8001/api/cases/$CASE_ID/confirm | jq

# 4. 关闭工单
curl -s -X PUT http://localhost:8001/api/cases/$CASE_ID/close | jq

echo "✓ 完整流程测试通过！"
```

### 场景2: TraceID追踪
```bash
# 发送带TraceID的请求
curl -s -X POST http://localhost:8001/api/cases \
  -H "Content-Type: application/json" \
  -H "X-Trace-ID: test-trace-12345" \
  -d '{"client_id": "test-002", "title": "TraceID Test"}' | jq

# 在数据库中验证
psql -h localhost -U hci_user -d hci_troubleshoot -c \
  "SELECT case_id, trace_id FROM cases WHERE trace_id = 'test-trace-12345';"

# 在日志中查找
docker logs hci-case-service | grep "test-trace-12345" | jq
```

## ✅ 测试检查清单

运行以下命令，确认所有项都通过：

```bash
cd /home/claude/hci-troubleshoot-platform

echo "1. TraceID单元测试..."
pytest tests/unit/test_trace_id.py -v && echo "✓ 通过" || echo "✗ 失败"

echo "2. Case Service单元测试..."
pytest tests/unit/test_case_service.py -v && echo "✓ 通过" || echo "✗ 失败"

echo "3. Case Service健康检查..."
curl -s http://localhost:8001/health | jq && echo "✓ 通过" || echo "✗ 失败"

echo "4. 创建工单测试..."
curl -s -X POST http://localhost:8001/api/cases \
  -H "Content-Type: application/json" \
  -d '{"client_id": "checklist", "title": "Check"}' | jq \
  && echo "✓ 通过" || echo "✗ 失败"

echo ""
echo "=== 测试完成 ==="
```

## 🐛 测试失败排查

### 数据库连接失败
```bash
# 检查PostgreSQL运行状态
docker ps | grep postgres

# 检查连接
psql -h localhost -U hci_user -d hci_troubleshoot -c "SELECT 1"

# 重启
docker-compose restart postgres
```

### 端口占用
```bash
# 查看8001端口
lsof -i :8001

# 杀掉进程
kill -9 <PID>
```

### 清理测试数据
```bash
psql -h localhost -U hci_user -d hci_troubleshoot << EOF
DELETE FROM cases WHERE client_id LIKE 'test-%';
DELETE FROM cases WHERE client_id LIKE 'quick-test%';
EOF
```

## 📝 完整测试文档

详细测试指南请查看: `docs/05_testing_guide.md`
