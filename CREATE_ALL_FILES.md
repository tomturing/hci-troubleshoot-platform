# HCI智能排障平台 - 完整代码生成指南

由于代码文件数量众多（预计100+文件），我将采用分批创建的方式。

## 文件创建清单

### 阶段1: 基础设施层 (Shared模块) ✓
- [x] backend/shared/database/postgres.py
- [x] backend/shared/database/redis.py  
- [x] backend/shared/models/base.py
- [x] backend/shared/utils/logger.py (已存在)
- [x] backend/shared/utils/trace_id.py (已存在)
- [ ] backend/shared/models/schemas.py

### 阶段2: Case Service
- [ ] backend/case-service/app/models/case.py
- [ ] backend/case-service/app/repositories/case_repo.py
- [ ] backend/case-service/app/services/case_service.py
- [ ] backend/case-service/app/routes/cases.py
- [ ] backend/case-service/app/config.py
- [ ] backend/case-service/app/main.py
- [ ] backend/case-service/requirements.txt
- [ ] backend/case-service/Dockerfile

### 阶段3: API Gateway
- [ ] backend/api-gateway/app/middleware/trace_id.py
- [ ] backend/api-gateway/app/services/session.py
- [ ] backend/api-gateway/app/routes/websocket.py
- [ ] backend/api-gateway/app/routes/cases.py
- [ ] backend/api-gateway/app/routes/health.py
- [ ] backend/api-gateway/app/config.py
- [ ] backend/api-gateway/app/main.py
- [ ] backend/api-gateway/requirements.txt
- [ ] backend/api-gateway/Dockerfile

### 阶段4: Conversation Service
- [ ] backend/conversation-service/app/models/conversation.py
- [ ] backend/conversation-service/app/models/message.py
- [ ] backend/conversation-service/app/repositories/conversation_repo.py
- [ ] backend/conversation-service/app/repositories/message_repo.py
- [ ] backend/conversation-service/app/services/openclaw_client.py
- [ ] backend/conversation-service/app/services/conversation_service.py
- [ ] backend/conversation-service/app/routes/conversations.py
- [ ] backend/conversation-service/app/config.py
- [ ] backend/conversation-service/app/main.py
- [ ] backend/conversation-service/requirements.txt
- [ ] backend/conversation-service/Dockerfile

### 阶段5: Scheduler Service
- [ ] backend/scheduler-service/app/models/pod.py
- [ ] backend/scheduler-service/app/repositories/pod_repo.py
- [ ] backend/scheduler-service/app/services/k8s_client.py
- [ ] backend/scheduler-service/app/services/scheduler.py
- [ ] backend/scheduler-service/app/routes/pods.py
- [ ] backend/scheduler-service/app/config.py
- [ ] backend/scheduler-service/app/main.py
- [ ] backend/scheduler-service/requirements.txt
- [ ] backend/scheduler-service/Dockerfile

### 阶段6: 前端代码
- [ ] frontend/src/main.ts
- [ ] frontend/src/App.vue
- [ ] frontend/src/router/index.ts
- [ ] frontend/src/stores/case.ts
- [ ] frontend/src/stores/websocket.ts
- [ ] frontend/src/views/Home.vue
- [ ] frontend/src/views/CaseList.vue
- [ ] frontend/src/views/Chat.vue
- [ ] frontend/src/components/CaseCard.vue
- [ ] frontend/src/components/MessageList.vue
- [ ] frontend/src/components/CommandDisplay.vue
- [ ] frontend/src/services/api.ts
- [ ] frontend/src/services/websocket.ts
- [ ] frontend/src/types/index.ts
- [ ] frontend/package.json
- [ ] frontend/tsconfig.json
- [ ] frontend/vite.config.ts

### 阶段7: 数据库
- [x] database/init_schema.sql (已存在)
- [ ] database/migrations/001_initial.sql
- [ ] database/seeds/test_data.sql

### 阶段8: 部署配置
- [ ] deploy/docker/docker-compose.yml
- [ ] deploy/k8s/*.yaml (9个文件)
- [ ] deploy/scripts/*.sh (3个文件)

### 阶段9: 配置文件
- [ ] .env.example
- [ ] .gitignore
- [ ] README.md
- [ ] Makefile

## 代码生成策略

由于文件数量多，我会:
1. 先生成所有Python后端代码的核心逻辑
2. 再生成前端Vue/TypeScript代码
3. 最后生成配置和部署文件
4. 将所有文件打包供下载

