# 本地端到端测试指南

## 快速开始（Docker Compose）

### 前置条件
- Docker和Docker Compose已安装
- 两个项目都在本地

### 步骤1：准备环境

```bash
# 1. 确保两个项目都在正确的分支
cd ops-agent
git checkout feature/openai-compatible-api

cd ../hci-troubleshoot-platform
git checkout feature/ops-agent-integration

# 2. 准备环境变量
cd deploy/docker
cp .env.opsagent.example .env

# 编辑.env，根据需要配置
# 注意：如果不需要完整的Agent功能，可以先不配置OPENROUTER_API_KEY
```

### 步骤2：启动服务

```bash
# 启动所有服务
docker-compose -f docker-compose.yml -f docker-compose.opsagent.yml up -d

# 查看服务状态
docker-compose -f docker-compose.yml -f docker-compose.opsagent.yml ps

# 查看日志
docker-compose -f docker-compose.yml -f docker-compose.opsagent.yml logs -f
```

### 步骤3：验证服务

```bash
# 等待服务启动完成后，测试OA服务
curl http://localhost:8006/health

# 测试conversation-service
curl http://localhost:8002/health
```

### 步骤4：前端测试

1. 打开浏览器访问 http://localhost:3001
2. 登录（如果需要）
3. 创建新工单
4. 在助手选择中，应该能看到 "ops-agent" 选项
5. 选择 "ops-agent"
6. 发送测试消息
7. 验证能收到响应

### 步骤5：API测试（可选）

如果只想测试API，可以：

```bash
# 测试OA的OpenAI兼容API
curl -X POST http://localhost:8006/v1/chat/completions \
    -H "Content-Type: application/json" \
    -d '{
        "model": "ops-agent",
        "messages": [{"role": "user", "content": "Hello"}],
        "stream": true
    }'
```

---

## 独立测试OA服务

如果只想测试OA服务，不需要启动完整的HCI-TP：

```bash
cd ops-agent

# 1. 安装依赖
uv sync

# 2. 准备配置文件
cp ops_config.yaml.example ops_config.yaml

# 3. 启动服务
uv run uvicorn ops_agent.server.main:app --host 0.0.0.0 --port 8006 --reload

# 4. 在另一个终端测试
cd ops-agent
./scripts/test-server.sh
```

---

## 创建PR走CI流程

当本地测试通过后，可以创建PR：

### 1. ops-agent PR

```bash
cd ops-agent
git checkout feature/openai-compatible-api

# 确保代码已提交
git status

# 创建PR（通过GitHub网页界面）
# 或者使用gh CLI
gh pr create --title "feat: 添加OpenAI-compatible API服务器" --body "
## 概述
添加OpenAI-compatible API服务器，支持与HCI-Troubleshoot-Platform集成。

## 变更
- 添加ops_agent/server/目录
- 实现FastAPI服务器
- 添加Dockerfile.ops-server
- 添加测试脚本

## 关联
- HCI-TP PR: [链接]
" --base main
```

### 2. HCI-TP PR

```bash
cd hci-troubleshoot-platform
git checkout feature/ops-agent-integration

# 创建PR
gh pr create --title "feat: 添加Ops-Agent助手集成" --body "
## 概述
集成Ops-Agent作为可选AI助手。

## 变更
- 添加OpsAgentAssistant类
- 更新配置
- 添加部署配置
- 添加测试文档

## 关联
- ops-agent PR: [链接]
" --base main
```

### 3. 等待CI通过

CI会自动运行：
- 文档门禁检查
- Lint检查
- 前端构建检查
- 等等...

---

## k3s部署测试（可选）

如果有本地k3s环境：

```bash
# 1. 确保k3s已启动
kubectl get nodes

# 2. 部署OA服务（需要先构建镜像）
cd ops-agent
docker build -f Dockerfile.ops-server -t ops-agent:latest .

# 3. 导入镜像到k3s
k3d image import ops-agent:latest --cluster your-cluster

# 4. 部署Helm chart
cd ../hci-troubleshoot-platform
helm upgrade --install hci-platform ./deploy/helm/hci-platform \
    --set conversationService.opsAgentEnabled=true
```

---

## 常见问题

### 端口冲突
如果8006端口被占用，可以修改：
- docker-compose.opsagent.yml中的端口映射
- 或者停止占用端口的进程

### OA服务无法启动
- 检查配置文件路径
- 查看容器日志：`docker logs ops-agent-service`

### HCI-TP无法连接OA
- 检查OPS_AGENT_ENABLED是否为true
- 检查OPS_AGENT_BASE_URL是否正确
- 检查网络连通性

---

## 测试检查清单

- [ ] 所有服务正常启动
- [ ] OA服务健康检查通过
- [ ] conversation-service健康检查通过
- [ ] 前端可以访问
- [ ] 可以看到ops-agent助手选项
- [ ] 可以选择ops-agent助手
- [ ] 可以发送消息
- [ ] 可以收到响应（即使是简单响应）
- [ ] 消息正确保存
