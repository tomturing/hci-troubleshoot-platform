# HCI 智能排障平台

基于微服务架构的智能运维排障系统，集成 AI 技术辅助 HCI 环境故障诊断和解决。

## 📋 项目概述

HCI 智能排障平台采用云端 Server 为核心、多客户端接入的架构，利用 AI 技术（OpenClaw + Zhipu AI）为运维人员提供智能化的故障排查和解决方案。

### 核心特性

- ✅ **微服务架构**: API Gateway + Case / Conversation / Scheduler 四大微服务
- ✅ **实时通信**: WebSocket 双向通信（Traefik sticky session 保障多副本粘性路由）
- ✅ **多助手架构 v2.0**: AI Assistant Pod Pool 多类型 + AssistantRegistry 配置化注册
- ✅ **智能调度**: Pod 热备池 + 按需创建 + Redis 状态持久化（服务重启自动恢复）
- ✅ **全链路追踪**: OpenTelemetry 标准分布式链路追踪（Trace 瀑布图 + Log 关联钻取）
- ✅ **结构化日志**: JSON stdout → Promtail → Loki，含 TTFT 首 Token 延迟指标
- ✅ **前端双应用**: Customer 对话式 UI + Admin 管理控制台（Vue 3 + TypeScript）
- ✅ **K3s 生产部署**: Helm Chart 22 资源 + 三层配置分层 + PDB / 反亲和 / TLS
- ✅ **Phase 1+2 重构优化**: 10 项 Bug 修复与架构加固（DI 模式、数据一致性、安全性）

## 🏗 系统架构 (v2.0)

```
用户层 (Web Client)
    ↓ WSS/HTTPS (Traefik sticky session)
网关层 (API Gateway :8000)
    - OTel 自动 Span 生成、W3C Trace Context 传播
    - WebSocket 连接管理、限流、助手选择代理
    ↓
服务层:
  ├─ Case Service (:8001 工单全生命周期)
  ├─ Conversation Service (:8002 对话管理, SSE 流式, TTFT 记录)
  └─ Scheduler Service (:8003 多类型 Pod 池调度, Redis 状态持久化)
    ↓
AI 层 (AI Assistant Pod Pool):
  └─ OpenClaw Deployment (:18789) — 容器化，OpenAI 兼容 /v1/chat/completions
      └─ 上游模型 (Z.AI GLM)
  └─ 可扩展至 NaboBot / PicoClaw / 自研助手（AssistantRegistry 配置化注册）
    ↓
数据层:
  ├─ PostgreSQL 15 (工单/对话/消息, 全表 trace_id, StatefulSet + PVC)
  └─ Redis 7 (Session / Pod 分配状态, StatefulSet + PVC + AOF 持久化)

可观测性层:
  Tempo (Trace) ← OTLP ← 各微服务 OTel SDK
  Loki (Logs)   ← Promtail ← Container stdout
  Grafana (可视化) → Trace↔Log 双向钻取，ai_ttft / pool-metrics 可查询
```

详细架构: [docs/01_架构设计.md](docs/01_架构设计.md)

## 🚀 快速开始

### 运行环境
- Python 3.12+ / Docker & Docker Compose
- PostgreSQL 15 / Redis 7
- K3s v1.34+（生产/集成部署）

### 推荐开发工具
- Windows WSL Ubuntu 24.04 + Docker Desktop
- Python 环境管理: `uv`
- 前端包管理: `pnpm`

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

### 方式二：K3s Helm 生产部署（推荐）

```bash
# 1. 构建所有 Docker 镜像
bash scripts/k3s-build.sh

# 2. 准备生产 override 配置（首次部署必须）
cp deploy/helm/hci-platform/values-prod.override.example.yaml /srv/hci/config/values-prod.override.yaml
# 编辑 override：替换域名、镜像 tag、数据库密码、token 等

# 3. 部署到 K3s（生产推荐）
bash scripts/k3s-deploy-prod.sh deploy

# 4. 验证部署
bash scripts/k3s-verify.sh

# 5. 卸载
bash scripts/k3s-deploy-prod.sh uninstall
```

## 📚 文档

- [00. 需求说明](docs/00_需求说明.md)
- [01. 架构设计](docs/01_架构设计.md)
- [02. 数据库设计](docs/02_数据库设计.md)
- [03. 接口设计](docs/03_接口设计.md)
- [04. 可观测性设计](docs/04_可观测性设计.md)
- [05. K8s 设计](docs/05_K8s设计.md)
- [06. 开发指南](docs/06_开发指南.md)
- [07. 测试指南](docs/07_测试指南.md)
- [08. 文件清单](docs/08_文件清单.md)
- [09. 项目进展](docs/09_项目进展.md)
- [10. 重构优化](docs/10_重构优化.md)
- [11. 生产环境](docs/11_生产环境.md)

## 📊 MVP 状态（全栈可用）

### ✅ 已完成
- 完整架构设计文档（v2.0 多助手架构）
- **所有微服务（API 网关、工单、会话、调度）基础框架与业务功能联调通过**
- 数据库 Schema 与 Pydantic 实体校验
- Docker Compose 本地全链路协同互通（SSE 问答打字机 E2E 验证）
- **OpenTelemetry 全链路分布式追踪** — Trace 瀑布图 + Loki 日志关联
- **Grafana 可观测性中台** — Loki + Promtail + Tempo + Grafana 一键部署
- **OpenClaw 容器化接入** — `hci-openclaw` Docker 镜像，通过 ZAI_API_KEY 对接 z.ai 模型
- **trace_id 统一为 OTel 标准** — 全链路使用 W3C traceparent 自动传播
- **前端双应用** — Customer 对话式 UI + Admin 管理控制台
- **v2.0 多助手架构** — AIAssistantRegistry + PodPoolManager + 前端助手选择器
- **K3s Helm 生产部署落地** — 22 资源（9 业务 + 4 可观测性），Traefik Ingress 统一入口
- **Phase 1+2 重构优化（10 项）**
  - 数据一致性：修复 message_count 双重递增 + 双重 commit Bug
  - 安全性：CORS 配置修复（`allow_origins=["*"]` + credentials 不兼容）
  - 可靠性：工单关闭后自动释放 Pod；HTTPException 不再被 except Exception 吞掉
  - 架构加固：Scheduler 分配状态迁移至 Redis；消除全局变量 DI；移除 sys.path.insert hack
  - 启动健壮性：PodPool.initialize() 扫描 K8s 存量 Pod；后台任务异常自动记录
- **安全与运维加固（2026-03-03）**
  - Ingress WebSocket 粘性会话配置（Traefik sticky cookie）
  - Ingress TLS 模板 + cert-manager 示例
  - Token 从 ConfigMap 剥离，统一由 Secret 注入
  - 可观测性镜像固定版本（Loki 3.3.2 / Promtail 3.3.2 / Grafana 11.4.0）
  - PostgreSQL 定时备份 CronJob（pg_dump → hostPath，30 天自动清理）
  - 生产弱密码校验（使用真实域名部署时自动 fail）
  - SSE 错误事件结构化化（`event: error` 形式，前端展示友好提示）
  - K8s Pod 创建指数退避重试（最多 3 次，应对 API Server 抖动）
  - Scheduler `/pool-metrics` 端点（实时 idle/active/pool_size 指标，可接 Grafana）
  - AI 调用 TTFT（首 Token 延迟）结构化日志（Loki 可查询）

### ⏳ 待完成（Phase 3/4）
- 知识库融合与 Prompt 调优（KB Service + RAG Pipeline，`feature/knowledge-rag` 分支）
- Scheduler Redis 集成测试（fakeredis）
- Pod 调度端到端测试
- GitHub Actions CI（lint + test + build）
- 负载测试基线（P95 延迟 / 并发连接数）

详见进展报告: [09_项目进展.md](docs/09_项目进展.md)

## 🛠 技术栈

**后端**: FastAPI + PostgreSQL 15 + Redis 7 + Python 3.12  
**前端**: Vue 3 + TypeScript + Vite + Element Plus  
**AI**: OpenClaw Gateway（容器化，端口 18789）+ Z.AI GLM  
**基础设施**: Docker / K3s + Helm / Traefik Ingress  
**可观测性**: OpenTelemetry + Grafana Tempo + Loki + Promtail

## 👥 作者

**tom**（需求设计）| **Claude** + **Codex** + **Gemini** + **OpenCode**（代码实现）
