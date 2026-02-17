# HCI 智能排障平台 - 项目文件清单

## 文档版本: 1.0 | 日期: 2026-02-15

本文档列出了项目的完整文件结构和每个文件的用途。

---

## 目录结构

```
hci-troubleshoot-platform/
├── README.md                                   # 项目说明
├── .gitignore                                  # Git 忽略文件
├── .env.example                                # 环境变量模板
├── docker-compose.yml                          # Docker Compose 配置
├── docker-compose.prod.yml                     # 生产环境 Docker Compose
│
├── docs/                                       # 文档目录
│   ├── 01_architecture_design.md               # 架构设计文档 ✅
│   ├── 02_database_design.md                   # 数据库设计文档 ✅
│   ├── 03_api_design.md                        # API 设计文档 ✅
│   ├── 04_development_guide.md                 # 开发指南 ✅
│   └── 05_deployment_guide.md                  # 部署指南 (待创建)
│
├── database/                                   # 数据库脚本
│   ├── init_schema.sql                         # 初始化 Schema ✅
│   ├── migrations/                             # 数据库迁移
│   │   └── README.md
│   └── seeds/                                  # 测试数据
│       └── test_data.sql
│
├── backend/                                    # 后端服务
│   ├── shared/                                 # 共享代码
│   │   ├── __init__.py
│   │   ├── models/                             # 数据模型
│   │   │   ├── __init__.py
│   │   │   ├── schemas.py                      # Pydantic 模型 ✅
│   │   │   └── database.py                     # SQLAlchemy 模型
│   │   ├── utils/                              # 工具函数
│   │   │   ├── __init__.py
│   │   │   ├── trace_id.py                     # TraceID 工具 ✅
│   │   │   ├── logger.py                       # 结构化日志 ✅
│   │   │   └── response.py                     # 响应工具
│   │   └── database/                           # 数据库连接
│   │       ├── __init__.py
│   │       ├── connection.py                   # 数据库连接
│   │       └── session.py                      # 会话管理
│   │
│   ├── api-gateway/                            # API 网关
│   │   ├── app/
│   │   │   ├── __init__.py
│   │   │   ├── main.py                         # 主入口
│   │   │   ├── config.py                       # 配置
│   │   │   ├── routers/                        # 路由
│   │   │   │   ├── __init__.py
│   │   │   │   ├── case.py                     # 工单路由
│   │   │   │   ├── health.py                   # 健康检查
│   │   │   │   └── websocket.py                # WebSocket 路由
│   │   │   ├── middleware/                     # 中间件
│   │   │   │   ├── __init__.py
│   │   │   │   ├── trace_id.py                 # TraceID 中间件
│   │   │   │   └── error_handler.py            # 错误处理
│   │   │   ├── websocket/                      # WebSocket 处理
│   │   │   │   ├── __init__.py
│   │   │   │   ├── manager.py                  # 连接管理
│   │   │   │   └── handler.py                  # 消息处理
│   │   │   └── services/                       # 服务调用
│   │   │       ├── __init__.py
│   │   │       ├── case_service.py             # Case Service 客户端
│   │   │       └── conversation_service.py     # Conversation Service 客户端
│   │   ├── tests/                              # 测试
│   │   │   ├── __init__.py
│   │   │   ├── test_main.py
│   │   │   └── test_websocket.py
│   │   ├── Dockerfile                          # Docker 配置
│   │   ├── requirements.txt                    # Python 依赖
│   │   └── README.md
│   │
│   ├── case-service/                           # 工单服务
│   │   ├── app/
│   │   │   ├── __init__.py
│   │   │   ├── main.py                         # 主入口
│   │   │   ├── config.py                       # 配置
│   │   │   ├── routers/                        # 路由
│   │   │   │   ├── __init__.py
│   │   │   │   └── case.py                     # 工单 CRUD
│   │   │   ├── services/                       # 业务逻辑
│   │   │   │   ├── __init__.py
│   │   │   │   └── case_service.py             # 工单服务
│   │   │   └── repositories/                   # 数据访问
│   │   │       ├── __init__.py
│   │   │       └── case_repository.py          # 工单数据访问
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── README.md
│   │
│   ├── conversation-service/                   # 对话服务
│   │   ├── app/
│   │   │   ├── __init__.py
│   │   │   ├── main.py                         # 主入口
│   │   │   ├── config.py                       # 配置
│   │   │   ├── routers/                        # 路由
│   │   │   │   ├── __init__.py
│   │   │   │   └── conversation.py             # 对话路由
│   │   │   ├── services/                       # 业务逻辑
│   │   │   │   ├── __init__.py
│   │   │   │   ├── conversation_service.py     # 对话服务
│   │   │   │   └── openclaw_client.py          # OpenClaw 客户端
│   │   │   └── repositories/                   # 数据访问
│   │   │       ├── __init__.py
│   │   │       ├── conversation_repository.py  # 对话数据访问
│   │   │       └── message_repository.py       # 消息数据访问
│   │   ├── tests/
│   │   ├── Dockerfile
│   │   ├── requirements.txt
│   │   └── README.md
│   │
│   └── scheduler-service/                      # 调度服务
│       ├── app/
│       │   ├── __init__.py
│       │   ├── main.py                         # 主入口
│       │   ├── config.py                       # 配置
│       │   ├── routers/                        # 路由
│       │   │   ├── __init__.py
│       │   │   └── scheduler.py                # 调度路由
│       │   ├── services/                       # 业务逻辑
│       │   │   ├── __init__.py
│       │   │   ├── scheduler_service.py        # 调度服务
│       │   │   └── k8s_client.py               # Kubernetes 客户端
│       │   └── models/                         # 模型
│       │       ├── __init__.py
│       │       └── pod.py                      # Pod 模型
│       ├── tests/
│       ├── Dockerfile
│       ├── requirements.txt
│       └── README.md
│
├── frontend/                                   # 前端应用
│   ├── src/
│   │   ├── components/                         # 组件
│   │   │   ├── CaseList.vue                    # 工单列表
│   │   │   ├── CaseCard.vue                    # 工单卡片
│   │   │   ├── ChatWindow.vue                  # 聊天窗口
│   │   │   ├── MessageItem.vue                 # 消息项
│   │   │   ├── CommandDisplay.vue              # 命令展示
│   │   │   └── LoadingSpinner.vue              # 加载动画
│   │   ├── views/                              # 页面
│   │   │   ├── Home.vue                        # 首页
│   │   │   ├── CaseList.vue                    # 工单列表页
│   │   │   ├── CaseDetail.vue                  # 工单详情页
│   │   │   └── Chat.vue                        # 聊天页
│   │   ├── stores/                             # 状态管理 (Pinia)
│   │   │   ├── case.ts                         # 工单状态
│   │   │   ├── chat.ts                         # 聊天状态
│   │   │   ├── user.ts                         # 用户状态
│   │   │   └── websocket.ts                    # WebSocket 状态
│   │   ├── api/                                # API 调用
│   │   │   ├── client.ts                       # HTTP 客户端
│   │   │   ├── case.ts                         # 工单 API
│   │   │   ├── message.ts                      # 消息 API
│   │   │   └── websocket.ts                    # WebSocket 客户端
│   │   ├── types/                              # 类型定义
│   │   │   ├── case.ts                         # 工单类型
│   │   │   ├── message.ts                      # 消息类型
│   │   │   ├── user.ts                         # 用户类型
│   │   │   └── api.ts                          # API 类型
│   │   ├── router/                             # 路由
│   │   │   └── index.ts                        # 路由配置
│   │   ├── utils/                              # 工具函数
│   │   │   ├── format.ts                       # 格式化工具
│   │   │   └── client-id.ts                    # ClientID 生成
│   │   ├── styles/                             # 样式
│   │   │   └── main.css                        # 全局样式
│   │   ├── App.vue                             # 根组件
│   │   └── main.ts                             # 入口文件
│   ├── public/
│   │   └── favicon.ico
│   ├── package.json                            # Node 依赖
│   ├── tsconfig.json                           # TypeScript 配置
│   ├── vite.config.ts                          # Vite 配置
│   ├── Dockerfile                              # 生产环境 Dockerfile
│   ├── Dockerfile.dev                          # 开发环境 Dockerfile
│   └── README.md
│
├── deploy/                                     # 部署配置
│   ├── docker/                                 # Docker 配置
│   │   └── docker-compose.prod.yml             # 生产环境 Compose
│   ├── k8s/                                    # Kubernetes 配置
│   │   ├── namespace.yaml                      # 命名空间
│   │   ├── configmap.yaml                      # 配置映射
│   │   ├── secret.yaml                         # 密钥
│   │   ├── postgres.yaml                       # PostgreSQL 部署
│   │   ├── redis.yaml                          # Redis 部署
│   │   ├── api-gateway.yaml                    # API Gateway 部署
│   │   ├── case-service.yaml                   # Case Service 部署
│   │   ├── conversation-service.yaml           # Conversation Service 部署
│   │   ├── scheduler-service.yaml              # Scheduler Service 部署
│   │   ├── openclaw.yaml                       # OpenClaw 部署
│   │   └── ingress.yaml                        # Ingress 配置
│   └── scripts/                                # 部署脚本
│       ├── deploy.sh                           # 部署脚本
│       ├── rollback.sh                         # 回滚脚本
│       └── backup.sh                           # 备份脚本
│
└── tests/                                      # 测试
    ├── integration/                            # 集成测试
    │   ├── test_case_workflow.py               # 工单流程测试
    │   ├── test_conversation_flow.py           # 对话流程测试
    │   └── test_websocket.py                   # WebSocket 测试
    ├── e2e/                                    # 端到端测试
    │   ├── test_user_journey.spec.ts           # 用户旅程测试
    │   └── playwright.config.ts                # Playwright 配置
    └── README.md
```

---

## 已创建文件状态

### 文档 (docs/)
- ✅ 01_architecture_design.md - 架构设计文档
- ✅ 02_database_design.md - 数据库设计文档
- ✅ 03_api_design.md - API 设计文档
- ✅ 04_development_guide.md - 开发指南

### 数据库 (database/)
- ✅ init_schema.sql - 数据库初始化脚本

### 后端共享代码 (backend/shared/)
- ✅ models/schemas.py - Pydantic 数据模型
- ✅ utils/trace_id.py - TraceID 工具
- ✅ utils/logger.py - 结构化日志工具

### 配置文件
- ✅ README.md - 项目说明
- ✅ .gitignore - Git 忽略文件
- ✅ .env.example - 环境变量模板
- ✅ docker-compose.yml - Docker Compose 配置

---

## 待创建文件

由于项目文件数量较多(预计100+个文件),以下是关键的待创建文件:

### 后端服务 (高优先级)

#### API Gateway
1. `backend/api-gateway/app/main.py` - 主入口
2. `backend/api-gateway/app/config.py` - 配置
3. `backend/api-gateway/app/routers/websocket.py` - WebSocket 路由
4. `backend/api-gateway/app/middleware/trace_id.py` - TraceID 中间件
5. `backend/api-gateway/Dockerfile` - Docker 配置
6. `backend/api-gateway/requirements.txt` - Python 依赖

#### Case Service
1. `backend/case-service/app/main.py` - 主入口
2. `backend/case-service/app/services/case_service.py` - 工单服务
3. `backend/case-service/app/repositories/case_repository.py` - 数据访问
4. `backend/case-service/Dockerfile` - Docker 配置
5. `backend/case-service/requirements.txt` - Python 依赖

#### Conversation Service
1. `backend/conversation-service/app/main.py` - 主入口
2. `backend/conversation-service/app/services/openclaw_client.py` - OpenClaw 客户端
3. `backend/conversation-service/Dockerfile` - Docker 配置
4. `backend/conversation-service/requirements.txt` - Python 依赖

#### Scheduler Service
1. `backend/scheduler-service/app/main.py` - 主入口
2. `backend/scheduler-service/app/services/k8s_client.py` - K8s 客户端
3. `backend/scheduler-service/Dockerfile` - Docker 配置
4. `backend/scheduler-service/requirements.txt` - Python 依赖

### 前端应用 (高优先级)

1. `frontend/src/main.ts` - 入口文件
2. `frontend/src/App.vue` - 根组件
3. `frontend/src/views/Chat.vue` - 聊天页面
4. `frontend/src/api/websocket.ts` - WebSocket 客户端
5. `frontend/package.json` - Node 依赖
6. `frontend/vite.config.ts` - Vite 配置

### Kubernetes 配置 (中优先级)

1. `deploy/k8s/namespace.yaml` - 命名空间
2. `deploy/k8s/configmap.yaml` - 配置映射
3. `deploy/k8s/api-gateway.yaml` - API Gateway 部署
4. `deploy/k8s/openclaw.yaml` - OpenClaw 部署

---

## 文件创建建议

建议按以下顺序创建文件:

### 阶段 1: 基础设施 (1-2天)
1. 共享代码 (database, utils)
2. Docker 配置文件
3. K8s 基础配置

### 阶段 2: 后端核心服务 (3-5天)
1. Case Service (最简单)
2. API Gateway (含 WebSocket)
3. Conversation Service
4. Scheduler Service

### 阶段 3: 前端应用 (2-3天)
1. 基础组件和路由
2. API 集成
3. WebSocket 集成
4. UI 优化

### 阶段 4: 测试和文档 (1-2天)
1. 单元测试
2. 集成测试
3. 部署文档

---

## 代码生成建议

由于文件数量较多,建议使用以下方式快速生成:

1. **使用代码生成工具**: 可以使用 cookiecutter 等工具生成项目骨架
2. **复制模板**: 将一个服务作为模板,复制并修改
3. **分批创建**: 按照上述阶段分批创建和测试

---

## 下一步行动

1. **立即可做**:
   - 执行 `docker-compose up -d` 启动数据库
   - 运行 `database/init_schema.sql` 初始化数据库
   - 开始创建第一个微服务 (Case Service)

2. **本周目标**:
   - 完成所有后端服务的基础代码
   - 实现基本的 API 功能
   - 完成 WebSocket 通信

3. **下周目标**:
   - 完成前端基础界面
   - 集成前后端
   - 端到端测试

---

*文档版本: 1.0 | 日期: 2026-02-15*
