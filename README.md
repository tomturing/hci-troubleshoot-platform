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
网关层 (API Gateway) - OTel 自动 Span 生成、W3C Trace Context 传播
    ↓
服务层:
  ├─ Case Service (工单管理)
  ├─ Conversation Service (对话管理)
  └─ Scheduler Service (Pod调度)
    ↓
AI层 (OpenClaw Pods) - Zhipu AI集成
    ↓
数据层 (PostgreSQL + Redis)

可观测性层:
  Tempo (Trace) ← OTLP ← 各微服务 OTel SDK
  Loki (Logs)   ← Promtail ← Docker stdout
  Grafana (可视化面板) → Trace↔Log 双向钻取
```

详细架构: [docs/01_架构设计.md](docs/01_架构设计.md)

## 🚀 快速开始

### 推荐开发环境
- windows wsl ubuntu 24.04 + docker-desktop
- 使用uv管理python环境
- 使用pnpm管理前端环境

### 运行环境
- Python 3.12+ / Docker & Docker Compose
- PostgreSQL 15 / Redis 7

### 启动服务

```bash
# 1. 配置环境变量
cp .env.example .env

# 2. 启动所有服务
make dev-up

# 3. 访问
#  - API Gateway: http://localhost:8000
#  - API文档: http://localhost:8000/docs
#  - Grafana 监控面板: http://localhost:3000
```

## 📚 文档

- [00.需求说明](docs/00_需求说明.md)
- [01.架构设计](docs/01_架构设计.md)
- [02.数据库设计](docs/02_数据库设计.md)
- [03.接口设计](docs/03_接口设计.md)
- [04.可观测性设计](docs/04_可观测性设计.md)
- [05.开发指南](docs/05_开发指南.md)
- [06.测试指南](docs/06_测试指南.md)
- [07.文件清单](docs/07_文件清单.md)
- [08.项目进展](docs/08_项目进展.md)

## 📊 MVP状态 (后端全量可用)

### ✅ 已完成
- 完整架构设计文档
- **所有微服务（API网关、工单、会话、调度）基础框架与业务功能实盘走通**
- 数据库Schema与Pydantic实体校验
- 基于 `docker-compose.yml` 的本地环境全链路协同互通（已完成 SSE 问答打字机 E2E 验证）
- **OpenTelemetry 全链路分布式追踪** — Trace 瀑布图 + Loki 日志关联
- **Grafana 可观测性中台** — Loki + Promtail + Tempo + Grafana 一键部署

### ⏳ 待补充
- 前端 Vue 3 控制台对接
- 生产级 K8s 部署配置清单和集群发布
- 真实的大模型密钥和业务知识库融合

详见最新的进展报告: [最新进展状态记录](docs/08_项目进展.md)

## 🛠 技术栈

**后端**: FastAPI + PostgreSQL + Redis + Docker + K8s  
**前端**: Vue 3 + TypeScript + Vite  
**AI**: OpenClaw + Zhipu AI (GLM-4)  
**可观测性**: OpenTelemetry + Grafana Tempo + Loki + Promtail

## 👥 作者

**tom** (需求设计) | **Claude** (代码实现)
