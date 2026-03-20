# HCI 智能排障平台

基于微服务架构的 AI 驱动运维排障系统，辅助 HCI 环境故障诊断。

## 📋 项目概述

HCI 智能排障平台采用云端 Server 为核心、多客户端接入的架构，利用 AI 技术（OpenClaw + Zhipu AI）为运维人员提供智能化的故障排查和解决方案。

### 核心特性

- ✅ **微服务架构**: API Gateway + Case / Conversation / Scheduler / KB 四大微服务
- ✅ **实时通信**: WebSocket 双向通信（Traefik sticky session 保障多副本粘性路由）
- ✅ **多助手架构 v2.0**: AI Assistant Pod Pool 多类型 + AssistantRegistry 配置化注册
- ✅ **智能调度**: Pod 热备池 + 按需创建 + Redis 状态持久化（服务重启自动恢复）
- ✅ **全链路追踪**: OpenTelemetry 标准分布式链路追踪（Trace 瀑布图 + Log 关联钻取）
- ✅ **结构化日志**: JSON stdout → Promtail → Loki，含 TTFT 首 Token 延迟指标
- ✅ **前端双应用**: Customer 对话式 UI + Admin 管理控制台（Vue 3 + TypeScript）
- ✅ **GitOps 交付**: ghcr.io 镜像 + ArgoCD + 双仓模型，dev 自动晋级，staging/prod 手动审批
- ✅ **四层 Helm Chart**: hci-platform（业务）/ infra（集群资源）/ data（PG+Redis）/ obs（可观测性）
- ✅ **工程化**: pre-commit hooks + Trivy 安全扫描 + Alembic 迁移 + 覆盖率 ≥ 60% + AlertManager 告警

## 🏗 系统架构 (v2.0)

```
用户层 (Web Client)
    ↓ WSS/HTTPS (Traefik sticky session)
网关层 (API Gateway :8000)
    - OTel 自动 Span 生成、W3C Trace Context 传播
    - WebSocket 连接管理、限流、助手选择代理
    ↓
服务层:
  ├─ Case Service         (:8001 工单全生命周期)
  ├─ Conversation Service (:8002 对话管理, SSE 流式, TTFT 记录)
  ├─ Scheduler Service    (:8003 多类型 Pod 池调度, Redis 状态持久化)
  └─ KB Service           (:8004 RAG 检索, SOP 匹配, pgvector)  [开发中]
    ↓
AI 层 (AI Assistant Pod Pool):
  └─ OpenClaw (:18789) — 容器化，OpenAI 兼容 /v1/chat/completions → Z.AI GLM
  └─ 可扩展至 NaboBot / PicoClaw / 自研助手（AssistantRegistry 配置化注册）
    ↓
数据层:
  ├─ PostgreSQL 15 (工单/对话/消息, 全表 trace_id, Alembic 版本迁移)
  └─ Redis 7    (Session / Pod 分配状态, AOF 持久化)

可观测性层:
  Tempo (Trace) ← OTLP ← 各微服务 OTel SDK
  Loki (Logs)   ← Promtail ← Container stdout
  Grafana       → Trace↔Log 双向钻取，AlertManager 告警规则
```

详细架构: [docs/architecture/系统架构.md](docs/architecture/系统架构.md)

## 🚀 快速开始

### 运行环境

- Python 3.12+ / Docker & Docker Compose
- PostgreSQL 15 / Redis 7
- K3s v1.28+（生产部署）

### 推荐开发工具

- Windows WSL Ubuntu 24.04 + Docker Desktop
- Python 环境管理: `uv`（必须）
- 前端包管理: `pnpm`（必须）

### 方式一：Docker Compose 本地开发

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填写 ZAI_API_KEY 以及 OPENCLAW_GATEWAY_TOKEN

# 2. 启动业务栈（含 OpenClaw）
docker compose -f deploy/docker/docker-compose.yml up -d --build

# 3. 启动可观测性栈
docker compose -f deploy/observability/docker-compose-obs.yml up -d

# 4. 访问
#  - Customer UI: http://localhost:3001
#  - Admin UI:    http://localhost:3002
#  - Grafana:     http://localhost:3000 (admin/admin)

# 5. 运行端到端验证
bash scripts/docker-e2e-test.sh
```

### 方式二：K3s + ArgoCD GitOps（生产推荐）

项目采用**双仓模型**：代码仓库（本仓库）负责代码和 CI，环境仓库（`hci-platform-env`）存储 Helm values。  
CI 在 push 到 main 时自动构建镜像推至 `ghcr.io`，并向环境仓库提交 dev 晋级 PR；ArgoCD 监听环境仓库完成自动同步。

Helm Chart 分四层，**按顺序部署**：

| Chart | 内容 | ArgoCD App |
|-------|------|------------|
| `hci-platform-infra` | StorageClass + ClusterRole（集群级，仅首次） | `hci-platform-infra.yaml` |
| `hci-platform-data` | PostgreSQL + Redis（三套环境独立，prune:false 保护 PVC） | `hci-platform-data-{dev,staging,prod}.yaml` |
| `hci-platform-obs` | Loki + Tempo + Grafana + Prometheus（全局一套，prune:false） | `hci-platform-obs.yaml` |
| `hci-platform` | 业务微服务 + 前端（三套环境） | `hci-platform-{dev,staging,prod}.yaml` |

```bash
# 首次部署（按顺序）
kubectl apply -f deploy/gitops/argo-apps/hci-platform-infra.yaml
kubectl apply -f deploy/gitops/argo-apps/hci-platform-data-dev.yaml
kubectl apply -f deploy/gitops/argo-apps/hci-platform-obs.yaml
kubectl apply -f deploy/gitops/argo-apps/hci-platform-dev.yaml

# 验证
bash scripts/k3s-verify.sh
```

> **应急通道**：`bash scripts/k3s-release.sh` 可绕过 GitOps 快速修复，但会产生配置漂移，
> 使用后需通过 ArgoCD Sync 对齐。

## 📚 文档

| 分类 | 文档 |
|------|------|
| 需求 | [需求说明](docs/requirements/) |
| 架构 | [系统架构](docs/architecture/系统架构.md) · [数据库设计](docs/architecture/数据库设计.md) · [接口设计](docs/architecture/接口设计.md) · [可观测性](docs/architecture/可观测性设计.md) · [AI助手层](docs/architecture/AI助手层设计.md) · [知识库RAG](docs/architecture/知识库RAG设计.md) · [客户端](docs/architecture/客户端设计.md) |
| 决策 | [ADR 索引](docs/adr/README.md)（ADR-001 ~ ADR-005） |
| 指南 | [开发指南](docs/guides/) · [项目交付标准化](docs/guides/项目交付标准化.md) |
| 参考 | [发布检查清单与回滚 SOP](docs/reference/) |

## 📊 项目状态

### ✅ 已完成

**业务功能**
- 所有微服务（API 网关、工单、会话、调度）基础框架与业务功能联调通过
- 数据库 Schema + Pydantic 实体校验 + Alembic 版本迁移
- Docker Compose 本地全链路协同互通（SSE 问答打字机 E2E 验证）
- 前端双应用（Customer 对话式 UI + Admin 管理控制台）
- v2.0 多助手架构（AIAssistantRegistry + PodPoolManager）
- K3s Helm 生产部署落地（Traefik Ingress + PDB + 反亲和 + TLS）

**可观测性**
- OpenTelemetry 全链路分布式追踪 — Trace 瀑布图 + Loki 日志关联
- Grafana 可观测性全栈（Loki + Promtail + Tempo）
- TTFT 首 Token 延迟结构化日志 + /pool-metrics 实时指标端点
- AlertManager 告警规则（4组10条，覆盖服务宕机/高延迟/DB连接/Pending积压）

**工程化交付（Sprint 1/2/3，2026-03）**
- GitHub Actions CI：lint + 单元测试（覆盖率 ≥ 60%）+ 集成测试 + Helm lint + 安全测试 + 文档治理
- 镜像构建推送至 `ghcr.io` + Trivy HIGH/CRITICAL 安全扫描阻断
- GitOps 双仓模型：dev 自动晋级 PR，staging/prod 手动审批
- 四层 Helm Chart 资源归属拆分（见 [ADR-005](docs/adr/005-Helm-Chart资源归属拆分.md)）
- pre-commit hooks（ruff + prettier + detect-secrets）
- CHANGELOG 自动化（release-please，Conventional Commits 驱动）
- 分支保护：5 个 Required checks + PR 代码审核

### ⏳ 待完成

- 知识库融合与 Prompt 调优（KB Service + RAG Pipeline，`feature/knowledge-rag` 分支）
- Scheduler Redis 集成测试（fakeredis）
- 负载测试基线（P95 延迟 / 并发连接数）

## 🛠 技术栈

| 层 | 技术 |
|----|------|
| 后端 | FastAPI + Python 3.12 + SQLAlchemy + asyncpg |
| 前端 | Vue 3 + TypeScript + Vite + Element Plus |
| 数据 | PostgreSQL 15 + pgvector + Redis 7 |
| AI | OpenClaw Gateway（:18789）+ Z.AI GLM |
| 基础设施 | Docker Compose（开发）/ K3s + Helm + ArgoCD（生产）|
| 镜像仓库 | ghcr.io（GitHub Container Registry）|
| 可观测性 | OpenTelemetry + Tempo + Loki + Promtail + Grafana |
| CI/CD | GitHub Actions + release-please |

## 👥 作者

**tom**（需求设计）| **Claude** + **Codex** + **Gemini** + **OpenCode**（代码实现）
