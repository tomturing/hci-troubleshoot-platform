# HCI 智能排障平台

> 版本 **v2.1.1** · 2026-04-24

HCI 环境 AI 故障诊断平台。微服务架构 + S0-S6 六阶段诊断状态机 + 双轨知识检索（SOP + RAG）。

### v2.1.1 更新说明（2026-04-24）

- **SSH UX 修复**：修复 Copilot Review 提出的 15 个代码质量问题
  - SshFormSection 使用本地副本模式避免 props 直接修改
  - CaseCreateDialog 进度条步骤顺序调整为 SSH认证→创建工单→采集环境
  - alert/task JSON 解析兼容包装对象格式 {alerts:[...]}
  - console.log 替换为 devLog（生产环境自动禁用）
  - 弹框打开时根据 bridgeStatus 重置 viewState
  - environmentApi.upsert 移除冗余字段（case_id/env_type 已在 URL path 中）

---

## 系统架构

```
用户层（Web Client）
    ↓ WSS/HTTPS（Traefik sticky session）
网关层 API Gateway :8000
    OTel Span 生成 · W3C Trace 传播 · WebSocket 管理 · 限流
    ↓
服务层：
  ├─ Case Service         :8001   工单生命周期（6 态状态机：created/confirmed/resolved/closed/in_progress/cancelled）
  ├─ Conversation Service :8002   S0-S6 诊断状态机 · P4 ReAct 引擎 · 3-Tier Prompt 组装 · SSE 流式
  ├─ Scheduler Service    :8003   AI Pod 池调度 · Redis 状态持久化 · 断线重连恢复
  └─ KB Service           :8004   BM25+向量混合检索 · SOP 外键路由 · pgvector（开发中）
    ↓
AI 层（Pod Pool）：
  OpenClaw :18789  → Z.AI GLM（AssistantRegistry 配置化注册，可扩展多助手）
    ↓
数据层：
  PostgreSQL 15（全表 trace_id · pgvector · Alembic 迁移）
  Redis 7（Pod 分配状态 · AOF 持久化）

可观测性：
  Tempo ← OTLP ← 各服务 OTel SDK
  Loki  ← Promtail ← Container stdout（含 TTFT 首 Token 延迟）
  Grafana → Trace↔Log 双向钻取 · AlertManager 告警（4 组 10 条）
```

### 诊断状态机（v6.3）

```
case.status（业务合同层）：  created → confirmed → resolved → closed
                                                ↘ in_progress（人工接管）
                          任意 → cancelled（/close · 超时 · 管理员）

conversation.diagnostic_stage（AI 推理层）：
  S0 意图识别 → S1 故障定位 → S2 假设生成 → S3 验证执行 → S4 根因确认 → S5 方案输出 → S6 验证闭环
                                                                                         ↓
                                                                            A=resolved · B=回退S1 · C=in_progress
```

两层状态**正交独立**，仅在 5 个同步点联动。详见 [docs/solution/架构设计.md §9](docs/solution/架构设计.md)。

---

## 快速开始

### 环境要求

| 项目 | 版本 |
|------|------|
| Python | 3.12+，包管理用 `uv`（必须） |
| 前端包管理 | `pnpm`（必须） |
| 容器 | Docker & Docker Compose |
| 数据库 | PostgreSQL 15 · Redis 7 |
| 生产集群 | K3s v1.28+（推荐 WSL Ubuntu 24.04）|

### 方式一：Docker Compose 本地开发

```bash
# 1. 配置环境变量
cp .env.example .env
# 填写 ZAI_API_KEY 和 OPENCLAW_GATEWAY_TOKEN

# 2. 启动业务栈（含 OpenClaw）
docker compose -f deploy/docker/docker-compose.yml up -d --build

# 3. 启动可观测性栈
docker compose -f deploy/observability/docker-compose-obs.yml up -d

# 4. 访问服务
#   Customer UI : http://localhost:3001
#   Admin UI    : http://localhost:3002
#   Grafana     : http://localhost:3000  (admin/admin)

# 5. 端到端验证
bash scripts/tools/docker-e2e-test.sh
```

### 方式二：K3s + ArgoCD GitOps（生产推荐）

项目采用**双仓模型**：本仓库负责代码与 CI，`hci-platform-env` 仓库存储 Helm values。  
CI 构建镜像推至 `ghcr.io`，向环境仓库提交 dev 晋级 PR，ArgoCD 自动同步。

Helm Chart 分四层，**按顺序部署**：

| Chart | 内容 | ArgoCD App |
|-------|------|------------|
| `hci-platform-infra` | StorageClass · ClusterRole（集群级，仅首次）| `local/hci-platform-infra-dev.yaml` |
| `hci-platform-data` | PostgreSQL · Redis（prune:false 保护 PVC）| `local/hci-platform-data-{dev,staging,prod}.yaml` |
| `hci-platform-obs` | Loki · Tempo · Grafana · Prometheus | `local/hci-platform-obs-{dev,staging,prod}.yaml` |
| `hci-platform` | 业务微服务 + 前端 | `local/hci-platform-{dev,staging,prod}.yaml` |

```bash
# 首次部署（按顺序）
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-infra-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-data-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-obs-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/local/hci-platform-dev.yaml

# 验证
bash scripts/ops/k3s-verify.sh
```

> **应急通道**：`bash scripts/ops/k3s-release.sh` 可绕过 GitOps 快速修复，使用后需通过 ArgoCD Sync 对齐。

---

## 文档索引

完整文档入口：[docs/README.md](docs/README.md)

| 分类 | 文档 |
|------|------|
| **架构** | [docs/solution/架构设计.md](docs/solution/架构设计.md) · [数据库设计.md](docs/solution/数据库设计.md) · [接口设计.md](docs/solution/接口设计.md) · [可观测性设计.md](docs/solution/可观测性设计.md) |
| **服务设计** | [工单设计.md](docs/solution/case/工单设计.md) · [对话设计.md](docs/solution/conversation/对话设计.md) · [AI助手设计.md](docs/solution/ai-assistant/AI助手设计.md) · [知识库设计.md](docs/solution/knowledge-base/知识库设计.md) |
| **前端设计** | [客户端设计.md](docs/solution/custom-ui/客户端设计.md) · [管理台设计.md](docs/solution/admin-ui/管理台设计.md) |
| **部署** | [部署指南.md](docs/deploy/部署指南.md) · [部署设计.md](docs/deploy/部署设计.md) · [发布指南.md](docs/deploy/发布指南.md) · [部署管理规范.md](docs/deploy/部署管理规范.md) |
| **测试** | [测试指南.md](docs/verify/测试指南.md) |
| **需求** | [需求说明.md](docs/requirement/需求说明.md) |
| **避坑指南** | [部署类 pitfalls](docs/deploy/pitfalls/_index.md) · [验证类 pitfalls](docs/verify/pitfalls/_index.md) |
| **规范** | [文档管理规范.md](docs/文档管理规范.md) |

---

## 项目状态

### ✅ 已完成

**核心功能**
- 全部微服务基础框架：API Gateway · Case · Conversation · Scheduler
- v6.3 双状态机：`case.status`（6 态）+ `conversation.diagnostic_stage`（S0-S6）
- S0 意图识别：198 分类列表注入 · category_id 提取 · S1 三轨路由
- S6 验证闭环：pending_resolution · 三选项（A/B/C）· 断线重连恢复
- P4 ReAct 引擎：`pending_confirm` 高危操作拦截 · `react_state` 断点快照
- 数据库 Schema + Alembic 版本迁移（migration v6.3 全量落地）
- 前端双应用：Customer 对话式 UI + Admin 管理控制台（Vue 3 + TypeScript）
- Docker Compose 本地全链路协同（SSE 打字机 E2E 验证通过）
- K3s Helm 生产部署（Traefik Ingress · PDB · 反亲和 · TLS）

**可观测性**
- OpenTelemetry 全链路追踪（Trace 瀑布图 + Loki 日志关联）
- TTFT 首 Token 延迟结构化日志 + `/pool-metrics` 实时指标
- AlertManager 告警规则（4 组 10 条：服务宕机 / 高延迟 / DB 连接 / Pending 积压）

**工程化**
- GitHub Actions CI：lint · 单测（覆盖率 ≥ 60%）· 集成 · Helm lint · 安全扫描 · 文档治理
- 镜像推至 `ghcr.io`，Trivy HIGH/CRITICAL 阻断
- GitOps 双仓模型：dev 自动晋级 PR，staging/prod 手动审批
- pre-commit hooks：ruff · prettier · detect-secrets
- release-please CHANGELOG 自动化（Conventional Commits）

### ⏳ 进行中

- KB Service：RAG Pipeline 代码落地（`feature/knowledge-rag` 分支）
- `data-pipeline/kbd` → kb-service API → `kbd_entry` 入库流程联调
- Scheduler Redis 集成测试（fakeredis）

---

## 目录结构

```
hci-troubleshoot-platform/
├── backend/                   # 后端微服务
│   ├── api-gateway/           # API 网关 :8000（含 SSH 终端、终端操作录制）
│   ├── case-service/          # 工单服务 :8001
│   ├── conversation-service/  # 对话服务 :8002
│   ├── scheduler-service/     # 调度服务 :8003
│   ├── kb-service/            # 知识库服务 :8004（开发中）
│   └── shared/                # 共享模型、数据库、工具
├── frontend/                  # 前端双应用（Vue 3）
│   ├── customer/              # Customer UI :3001
│   ├── admin/                 # Admin UI :3002
│   └── shared/                # 共享组件
├── deploy/                    # 部署配置
│   ├── docker/                # Docker Compose
│   ├── helm/                  # 四层 Helm Chart
│   ├── gitops/                # ArgoCD Application 定义
│   ├── observability/         # Loki·Tempo·Grafana 配置
│   ├── claw-configs/          # OpenClaw 助手配置
│   └── env/                   # 环境变量模板
├── database/                  # 数据库迁移脚本与种子数据
├── adapters/                  # CLI-to-OpenAI 适配器（NanoClaw·NanoBot）
├── terminal_bridge/           # SSH 终端代理（Go）
├── scripts/                   # 运维与工具脚本
│   ├── ci/                    # CI 质量门禁脚本
│   ├── dev/                   # 本地开发辅助
│   ├── ops/                   # K3s 运维操作
│   ├── tools/                 # 测试工具
│   ├── kbd/                   # 知识库数据导入
│   └── evaluation/            # 意图识别评估脚本（intent_eval.py · test_sse.py · write_ai_response.py · write_intent_to_excel.py）
├── tests/                     # 集成测试
└── docs/                      # 文档（见 docs/README.md）
    ├── requirement/           # 需求文档
    ├── solution/              # 架构与服务设计
    ├── task/                  # 当前任务跟踪
    ├── deploy/                # 部署文档 + pitfalls
    ├── verify/                # 测试文档 + pitfalls
    └── archive/               # 历史归档
```

---

## 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI · Python 3.12 · SQLAlchemy 2.0 · asyncpg · Pydantic v2 |
| 前端 | Vue 3 · TypeScript · Vite · Element Plus · pnpm |
| 数据 | PostgreSQL 15 · pgvector · Redis 7 |
| AI | OpenClaw Gateway :18789 · Z.AI GLM |
| 基础设施 | Docker Compose（开发）/ K3s + Helm + ArgoCD（生产）|
| 镜像仓库 | ghcr.io（GitHub Container Registry）|
| 可观测性 | OpenTelemetry · Tempo · Loki · Promtail · Grafana |
| CI/CD | GitHub Actions · release-please |

---

## 作者

**tom**（需求设计）| **Claude** + **Codex** + **Gemini** + **OpenCode**（代码实现）
