# Ops-Agent集成测试指南

## 测试清单

### 1. 基础功能测试

#### 1.1 OA服务启动测试
- [ ] 服务可以正常启动
- [ ] `/health` 端点返回200
- [ ] `/health/live` 端点返回200
- [ ] `/health/ready` 端点返回200
- [ ] `/metrics` 端点可访问

#### 1.2 OpenAI兼容API测试
- [ ] `/v1/chat/completions` 端点响应正常
- [ ] 流式输出格式正确
- [ ] 可以处理简单的用户消息

### 2. HCI-TP集成测试

#### 2.1 助手注册测试
- [ ] OA助手在 `AIAssistantRegistry` 中正确注册
- [ ] 助手列表包含 `ops-agent`
- [ ] 可以获取到OA助手客户端

#### 2.2 健康检查测试
- [ ] OA健康检查可以正常调用
- [ ] 健康检查超时处理正常

#### 2.3 对话测试
- [ ] 可以创建使用OA助手的工单
- [ ] 可以发送消息给OA
- [ ] 可以接收OA的响应
- [ ] 流式输出正常
- [ ] 消息正确保存到数据库

### 3. 部署测试

#### 3.1 Docker Compose测试
- [ ] 可以通过 `docker-compose -f docker-compose.yml -f docker-compose.opsagent.yml up -d` 启动
- [ ] OA服务容器正常启动
- [ ] 容器健康检查通过
- [ ] 服务间网络连通正常

#### 3.2 Helm部署测试
- [ ] Helm模板渲染正确
- [ ] Service正确创建
- [ ] ConfigMap正确创建
- [ ] Deployment正确创建
- [ ] Pod正常启动并通过健康检查
- [ ] conversation-service可以连接到ops-agent-service

### 4. 回滚测试
- [ ] 设置 `OPS_AGENT_ENABLED=false` 后OA助手不可用
- [ ] 禁用OA后不影响其他助手功能

## 本地测试步骤

### 步骤1：启动OA服务

```bash
cd ops-agent

# 安装依赖（如果需要）
uv sync

# 启动服务
uv run uvicorn ops_agent.server.main:app --host 0.0.0.0 --port 8006 --reload
```

### 步骤2：测试OA服务

```bash
cd ops-agent

# 运行测试脚本
./scripts/test-server.sh
```

或者手动测试：

```bash
# 健康检查
curl http://localhost:8006/health

# 测试API
curl -X POST http://localhost:8006/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "ops-agent",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": true
    }'
```

### 步骤3：启动完整开发环境

```bash
cd hci-troubleshoot-platform/deploy/docker

# 设置环境变量
export OPS_AGENT_ENABLED=true
export OPS_AGENT_BASE_URL=http://localhost:8006

# 启动所有服务
docker-compose -f docker-compose.yml -f docker-compose.opsagent.yml up -d

# 查看日志
docker-compose -f docker-compose.yml -f docker-compose.opsagent.yml logs -f
```

### 步骤4：测试HCI-TP集成

1. 访问前端UI (http://localhost:3001)
2. 创建新工单
3. 在助手选择中选择 "ops-agent"
4. 发送测试消息
5. 验证OA响应正常

## 常见问题排查

### OA服务无法启动
- 检查端口8006是否被占用
- 检查配置文件路径是否正确
- 查看服务日志

### HCI-TP无法连接OA
- 检查 `OPS_AGENT_ENABLED` 是否为 `true`
- 检查 `OPS_AGENT_BASE_URL` 是否正确
- 检查网络连通性
- 查看conversation-service日志

### API请求超时
- 检查OA服务是否正常运行
- 检查网络延迟
- 可以调整超时配置

## 验收标准

### 功能验收
- [ ] 所有基础功能测试通过
- [ ] 所有集成测试通过
- [ ] 用户可以正常使用OA助手进行对话

### 性能验收
- [ ] 健康检查响应时间 < 1秒
- [ ] API首字延迟 < 5秒（正常情况下）
- [ ] 流式输出流畅无卡顿

### 质量验收
- [ ] 无明显错误日志
- [ ] 所有服务健康检查通过
- [ ] 可以正常回滚
