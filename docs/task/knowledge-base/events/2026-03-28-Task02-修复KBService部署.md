---
status: active
category: task
audience: developer
last_updated: 2026-03-28
owner: team
related: 02
---

# Task 02：修复 KB Service 部署（P0）

```
你是一名负责 hci-troubleshoot-platform kb-service 的 agent。

【仓库】
git clone https://github.com/tomturing/hci-troubleshoot-platform.git
cd hci-troubleshoot-platform

【背景】
知识库服务（kb-service）已有完整代码实现（BM25 + pgvector + RRF 混合检索），
但目前未在 dev 环境正常运行。conversation-service 调用 kb-service 时无法获得
知识检索结果，是整个 RAG 功能失效的直接原因。

需要确认并修复 KB Service 在 Docker Compose 环境中的启动和运行。

【任务目标】
1. 排查并修复 KB Service 无法启动的原因
2. 确认 pgvector 扩展正确安装并初始化
3. 确认 kb-service 数据库迁移执行完整
4. 确认 conversation-service 能成功调用 kb-service 的检索接口
5. 端到端验证：发一条包含"虚拟机开机失败"的检索请求能返回（空）结果而非 5xx 错误

【涉及服务 / 文件范围】
允许修改：
  - deploy/docker/docker-compose.yml（仅 kb-service 相关配置）
  - backend/kb-service/（服务代码、配置）
  - database/（如有需要补充的 schema 修复）
只读参考：
  - backend/conversation-service/app/services/kb_client.py（调用方，不修改）
  - backend/shared/（共享代码，不修改）

【详细实现步骤】

Step 1：环境状态检查
```bash
# 检查当前服务运行状态
docker compose -f deploy/docker/docker-compose.yml ps

# 检查 kb-service 日志
docker compose -f deploy/docker/docker-compose.yml logs kb-service --tail 50

# 检查 PostgreSQL pgvector 扩展
docker compose -f deploy/docker/docker-compose.yml exec postgres \
  psql -U hci_user -d hci_db -c "SELECT extname, extversion FROM pg_extension WHERE extname='vector';"
```

Step 2：修复已发现的问题

常见问题清单（按可能性排序）：
a. docker-compose.yml 中 kb-service 缺少 depends_on: postgres
b. pgvector 未在 postgres 镜像中安装（需要 pgvector/pgvector:pg15 镜像）
c. 数据库迁移未执行（knowledge_chunks 表不存在）
d. 环境变量 KB_SERVICE_URL / DATABASE_URL 配置错误
e. 端口映射冲突（8004 被占用）

逐一检查并修复。

Step 3：确认迁移完整性
```bash
# 在 kb-service 容器内运行迁移
docker compose -f deploy/docker/docker-compose.yml exec kb-service \
  uv run alembic upgrade head

# 验证 knowledge_chunks 表存在
docker compose -f deploy/docker/docker-compose.yml exec postgres \
  psql -U hci_user -d hci_db -c "\dt knowledge*"
```

Step 4：接口验证
```bash
# 健康检查
curl http://localhost:8004/health
# 预期：{"status": "healthy"}

# 检索接口（空结果也是正常）
curl -X POST http://localhost:8004/api/v1/search \
  -H "Content-Type: application/json" \
  -d '{"query": "虚拟机开机失败", "top_k": 5}'
# 预期：{"chunks": [], "total": 0} 或有内容，不应是 5xx 错误

# conversation-service 通过 kb_client 联调
# 在 conversation-service 日志中确认 kb_client 调用成功
```

Step 5：记录修复内容

在 PR 描述中说明修复了哪些配置问题，以便后续 Task 03 的入库工作衔接。

【约束】
- 不修改 conversation-service 代码（只修复 kb-service 和 docker-compose）
- 不修改 kb-service 的检索逻辑（只修复部署问题）
- 不修改现有数据库 schema（只执行已有迁移）

【验收标准】
- [ ] docker compose ps 显示 kb-service 状态为 running（healthy）
- [ ] curl http://localhost:8004/health 返回 200
- [ ] curl POST http://localhost:8004/api/v1/search 返回 200（空结果或有内容）
- [ ] conversation-service 日志中 kb_client 调用无 ConnectionError
- [ ] make lint 无新增错误
```

---