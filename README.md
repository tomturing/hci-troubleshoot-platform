# HCI 智能排障平台

基于微服务架构的智能运维排障系统，集成AI技术辅助HCI环境故障诊断和解决。

## 📋 项目概述

HCI智能排障平台采用云端Server为核心、多客户端接入的架构，利用AI技术（OpenClaw + Zhipu AI）为运维人员提供智能化的故障排查和解决方案。

### 核心特性

- ✅ **微服务架构**: API Gateway + 3个核心微服务  
- ✅ **实时通信**: WebSocket双向通信
- ✅ **智能调度**: OpenClaw Pod智能调度（热备池+按需创建）
- ✅ **全链路追踪**: OpenTelemetry 标准分布式链路追踪（Trace 瀑布图 + Log 关联钻取）
- ✅ **结构化日志**: JSON格式日志 + Loki 中央聚合
- ✅ **容器化部署**: Docker + Kubernetes支持

## 🏗 系统架构

```
用户层 (Web Client)
    ↓ WSS/HTTPS
网关层 (API Gateway :8000) - OTel 自动 Span 生成、W3C Trace Context 传播
    ↓
服务层:
  ├─ Case Service (:8001 工单管理)
  ├─ Conversation Service (:8002 对话管理)
  └─ Scheduler Service (:8003 Pod调度)
    ↓
AI层:
  └─ OpenClaw Gateway (:18789) - 容器化部署，OpenAI 兼容 /v1/chat/completions
      └─ 上游模型 (Z.AI GLM)
    ↓
数据层 (PostgreSQL + Redis)

可观测性层:
  Tempo (Trace) ← OTLP ← 各微服务 OTel SDK
  Loki (Logs)   ← Promtail ← Docker stdout
  Grafana (可视化面板) → Trace↔Log 双向钻取（自动 Provisioning 数据源）
```

详细架构: [docs/01_架构设计.md](docs/01_架构设计.md)

## 🚀 快速开始

### 推荐开发环境
- Windows WSL Ubuntu 24.04 + Docker-Desktop
- 使用uv管理python环境
- 使用pnpm管理前端环境

### 运行环境
- Python 3.12+ / Docker & Docker Compose
- PostgreSQL 15 / Redis 7

### 启动服务

```bash
# 1. 配置环境变量
cp .env.example .env
# 编辑 .env，填写 ZAI_API_KEY（或 Z_AI_API_KEY）以及 OPENCLAW_GATEWAY_TOKEN

# 2. 创建共享网络（首次）
docker network create hci-troubleshoot-platform_default 2>/dev/null || true

# 3. 启动业务栈（含 OpenClaw）
docker compose -f deploy/docker/docker-compose.yml up -d --build

# 4. 启动可观测性栈（Loki + Tempo + Promtail + Grafana）
docker compose -f deploy/observability/docker-compose-obs.yml up -d

# 5. 访问
#  - API Gateway: http://localhost:8000
#  - API文档: http://localhost:8000/docs
#  - Customer UI: http://localhost:3001 (客户端对话界面)
#  - Admin UI: http://localhost:3002 (管理控制台)
#  - Grafana 监控面板: http://localhost:3000 (admin/admin)

# 6. 运行端到端验证
bash test_manual.sh
```

## 📚 文档

- [00.需求说明](docs/00_需求说明.md)
- [01.架构设计](docs/01_架构设计.md)
- [02.数据库设计](docs/02_数据库设计.md)
- [03.接口设计](docs/03_接口设计.md)
- [04.可观测性设计](docs/04_可观测性设计.md)
- [05.K8s设计](docs/05_K8s设计.md)
- [06.开发指南](docs/06_开发指南.md)
- [07.测试指南](docs/07_测试指南.md)
- [08.文件清单](docs/08_文件清单.md)
- [09.项目进展](docs/09_项目进展.md)

## 📊 MVP状态 (全栈可用)

### ✅ 已完成
- 完整架构设计文档
- **所有微服务（API网关、工单、会话、调度）基础框架与业务功能实盘走通**
- 数据库Schema与Pydantic实体校验
- 基于 `docker-compose.yml` 的本地环境全链路协同互通（已完成 SSE 问答打字机 E2E 验证）
- **OpenTelemetry 全链路分布式追踪** — Trace 瀑布图 + Loki 日志关联
- **Grafana 可观测性中台** — Loki + Promtail + Tempo + Grafana 一键部署 + 数据源自动 Provisioning
- **OpenClaw 容器化接入** — 以 Docker 容器运行 OpenClaw Gateway（端口 18789），通过 ZAI_API_KEY 对接真实 z.ai 模型
- **trace_id 统一为 OTel 标准** — 全链路使用 W3C traceparent 自动传播
- **前端双应用** — Customer 对话式UI（:3001） + Admin 管理控制台（:3002），Docker 集成部署

### ⏳ 待补充
- 生产级 K8s 部署配置清单和集群发布
- 知识库融合与 Prompt 调优

详见最新的进展报告: [最新进展状态记录](docs/09_项目进展.md)

## 🛠 技术栈

**后端**: FastAPI + PostgreSQL + Redis + Docker + K8s  
**前端**: Vue 3 + TypeScript + Vite  
**AI**: OpenClaw Gateway (容器化, 端口 18789) + Z.AI GLM  
**可观测性**: OpenTelemetry + Grafana Tempo + Loki + Promtail

## 👥 作者

**tom** (需求设计) | **Claude** + **Codex** + **Gemini** + **OpenCode** (代码实现)
