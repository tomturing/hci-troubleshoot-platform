# HCI 智能排障平台

> 版本 **v2.2.1** · 2026-05-24

HCI 环境 AI 故障诊断平台。微服务架构 + Ports & Adapters Agent 路由 + Triage/Investigation/Remediation 分层诊断 + SOP/KBD 双轨知识检索。

### v2.2.1 更新说明（2026-05-24）

- **Agent Service 方案 B 重构**：新增 `domain/` + `adapters/agents/` + `adapters/clients/` 分层结构
- **HTP Agent 分层**：TriageAgent 负责 S0 分诊，InvestigationAgent 负责 S1-S4 调查，RemediationAgent 负责 S5 修复确认
- **BaseAgent 抽象**：新增 think/act/run 最小控制循环，作为 HTP Agent 内部继承契约
- **CDD 案例差异诊断**：基于 `KBClient.search_cases_with_steps()` 返回带步骤案例，按最大消除策略执行调查
- **Conversation Service 简化**：移除本地 knowledge_retriever/prompt_builder，转为对话状态机与 agent-service 流式客户端

---

## 系统架构

```
用户层（Web Client）
    ↓ WSS/HTTPS
网关层 API Gateway :8000
    OTel Span · W3C Trace · WebSocket · 限流
    ↓
服务层：
  ├─ Case Service         :8001   工单生命周期（6 态状态机）
  ├─ Conversation Service :8002   对话管理 · SSE 流式 · 上下文组装
  ├─ Agent Service        :8003   AI 推理引擎（AgentRouter）
  ├─ KB Service           :8004   BM25+向量混合检索 · SOP 路由
  ├─ Scheduler Service    :8005   助手注册表（已迁移至 dashscope）
  └─ Eval Service         :8006   AI 评估与质量统计
    ↓
AI 推理层（Ports & Adapters）：
  AgentRouter
    ├─ HTP Agent（Triage → Investigation → Remediation）
    ├─ OPS Agent → ops-agent ACP REST
    └─ PAI Agent → pydantic-ai Agent
    ↓
数据层：
  PostgreSQL 15（18 表 · trace_id · pgvector）
  Redis 7（Pod 状态 · Session）

可观测性：
  Tempo ← OTLP ← OTel SDK
  Loki  ← Promtail ← stdout
  Grafana → Trace↔Log 双向钻取
```

### 诊断状态机

```
case.status（业务合同层）：
  created → confirmed → resolved → closed
                         ↘ in_progress（人工接管）
  任意 → cancelled

conversation.diagnostic_stage（AI 推理层）：
  S0 意图识别 → S1 故障定位 → S2 假设生成 → S3 验证执行
    → S4 根因确认 → S5 方案输出 → S6 验证闭环
                                      ↓
                          A=resolved · B=回退S1 · C=in_progress
```

两层状态**正交独立**，仅在 5 个同步点联动。详见 [docs/solution/架构设计.md](docs/solution/架构设计.md)。

---

## 快速开始

### 环境要求

| 项目 | 版本 |
|------|------|
| Python | 3.12+，包管理用 `uv`（必须） |
| 前端包管理 | `pnpm`（必须） |
| 容器 | Docker & Docker Compose |
| 数据库 | PostgreSQL 15 · Redis 7 |
| 生产集群 | K3s v1.28+ |

### 方式一：Docker Compose 本地开发

```bash
# 1. 配置环境变量
cp .env.example .env
# 填写 LLM_API_KEY

# 2. 启动业务栈
docker compose -f deploy/docker/docker-compose.yml up -d --build

# 3. 启动可观测性栈
docker compose -f deploy/observability/docker-compose-obs.yml up -d

# 4. 访问服务
#   Customer UI : http://localhost:3001
#   Admin UI    : http://localhost:3002
#   Grafana     : http://localhost:3000

# 5. 端到端验证
bash scripts/tools/docker-e2e-test.sh
```

### 方式二：K3s + ArgoCD GitOps（生产推荐）

项目采用**双仓模型**：本仓库负责代码与 CI，`hci-platform-env` 仓库存储 Helm values。

**四层 Helm Chart（按顺序部署）**：

| Chart | 内容 |
|-------|------|
| `hci-platform-infra` | StorageClass · ClusterRole |
| `hci-platform-data` | PostgreSQL · Redis |
| `hci-platform-obs` | Loki · Tempo · Grafana |
| `hci-platform` | 业务微服务 + 前端 |

```bash
# 首次部署（按顺序）
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-infra-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-data-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-obs-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-dev.yaml

# 验证
bash scripts/ops/k3s-verify.sh
```

---

## 文档索引

完整文档入口：[docs/README.md](docs/README.md)

| 分类 | 文档 |
|------|------|
| **架构** | [架构设计.md](docs/solution/架构设计.md) · [数据库设计.md](docs/solution/数据库设计.md) · [接口设计.md](docs/solution/接口设计.md) · [可观测性设计.md](docs/solution/可观测性设计.md) |
| **服务设计** | [工单设计.md](docs/solution/case/工单设计.md) · [对话设计.md](docs/solution/conversation/对话设计.md) · [agent设计.md](docs/solution/agent/agent设计.md) · [知识库设计.md](docs/solution/knowledge-base/知识库设计.md) |
| **前端设计** | [客户端设计.md](docs/solution/custom-ui/客户端设计.md) · [管理台设计.md](docs/solution/admin-ui/管理台设计.md) |
| **部署** | [部署指南.md](docs/deploy/部署指南.md) · [发布指南.md](docs/deploy/发布指南.md) |
| **测试** | [测试指南.md](docs/verify/测试指南.md) |
| **避坑指南** | [部署类 pitfalls](docs/deploy/pitfalls/_index.md) · [验证类 pitfalls](docs/verify/pitfalls/_index.md) |

---

## 项目状态

### ✅ 已完成

**核心功能**
- 7 个微服务：API Gateway · Case · Conversation · Agent · KB · Scheduler · Eval
- Ports & Adapters Agent 架构：HTP Agent · OPS Agent · PAI Agent
- 双状态机：`case.status`（6 态）+ `conversation.diagnostic_stage`（S0-S6）
- S0 意图识别：198 分类列表 · category_id 提取 · 三轨路由
- S6 验证闭环：pending_resolution · 三选项（A/B/C）
- dashscope 网关集成：GLM-5 统一调用
- 前端双应用：Customer UI + Admin UI（Vue 3 + TypeScript）
- Docker Compose 本地全链路
- K3s Helm 生产部署

**可观测性**
- OpenTelemetry 全链路追踪
- TTFT 首 Token 延迟日志
- Grafana Dashboard

**工程化**
- GitHub Actions CI：lint · 单测 · 安全扫描
- GitOps 双仓模型
- release-please CHANGELOG 自动化

---

## 目录结构

```
hci-troubleshoot-platform/
├── backend/                   # 后端微服务
│   ├── api-gateway/           # API 网关 :8000
│   ├── case-service/          # 工单服务 :8001
│   ├── conversation-service/  # 对话服务 :8002
│   ├── agent-service/         # AI 推理引擎 :8003
│   ├── kb-service/            # 知识库服务 :8004
│   ├── scheduler-service/     # 调度服务 :8005
│   ├── eval-service/          # 评估服务 :8006
│   └── shared/                # 共享模块
├── frontend/                  # 前端（pnpm monorepo）
│   ├── customer/              # Customer UI :3001
│   ├── admin/                 # Admin UI :3002
│   └── shared/                # 共享组件
├── deploy/                    # 部署配置
│   ├── docker/                # Docker Compose
│   ├── helm/                  # Helm Chart
│   ├── gitops/                # ArgoCD
│   └── observability/         # 可观测性
├── database/                  # 数据库 Schema
├── terminal_bridge/           # SSH 终端代理（Go）
├── scripts/                   # 运维脚本
├── tests/                     # 集成测试
└── docs/                      # 文档
```

详细目录结构见 [docs/solution/目录结构.md](docs/solution/目录结构.md)。

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI · Python 3.12 · SQLAlchemy 2.0 · asyncpg |
| 前端 | Vue 3 · TypeScript · Vite · Element Plus · pnpm |
| 数据 | PostgreSQL 15 · pgvector · Redis 7 |
| AI | dashscope 网关（GLM-5）· ops-agent · pydantic-ai |
| 基础设施 | Docker Compose / K3s + Helm + ArgoCD |
| 可观测性 | OpenTelemetry · Tempo · Loki · Grafana |
| CI/CD | GitHub Actions · release-please |

---

## 作者

**tom**（需求设计）| **Claude**（代码实现）
