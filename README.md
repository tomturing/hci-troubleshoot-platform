# HCI 智能排障平台

基于微服务架构的智能运维排障系统，集成AI技术辅助HCI环境故障诊断和解决。

## 📋 项目概述

HCI智能排障平台采用云端Server为核心、多客户端接入的架构，利用AI技术（OpenClaw + Zhipu AI）为运维人员提供智能化的故障排查和解决方案。

### 核心特性

- ✅ **微服务架构**: API Gateway + 3个核心微服务  
- ✅ **实时通信**: WebSocket双向通信
- ✅ **智能调度**: OpenClaw Pod智能调度（热备池+按需创建）
- ✅ **全链路追踪**: TraceID贯穿整个调用链路
- ✅ **结构化日志**: JSON格式日志
- ✅ **容器化部署**: Docker + Kubernetes支持

## 🏗 系统架构

```
用户层 (Web Client)
    ↓ WSS/HTTPS
网关层 (API Gateway) - TraceID生成、Session管理
    ↓
服务层:
  ├─ Case Service (工单管理)
  ├─ Conversation Service (对话管理)
  └─ Scheduler Service (Pod调度)
    ↓
AI层 (OpenClaw Pods) - Zhipu AI集成
    ↓
数据层 (PostgreSQL + Redis)
```

详细架构: [docs/01_architecture_design.md](docs/01_architecture_design.md)

## 🚀 快速开始

### 前置要求
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
```

## 📚 文档

- [架构设计](docs/01_architecture_design.md)
- [数据库设计](docs/02_database_design.md)
- [API接口设计](docs/03_api_design.md)
- [开发指南](docs/04_development_guide.md)
- [完整交付指南](COMPLETE_GUIDE.md)

## 📊 MVP状态

### ✅ 已完成 (~40个文件, ~3000行代码)
- 完整架构设计文档
- 所有微服务基础框架
- 数据库Schema
- Docker本地开发环境

### ⏳ 待补充
- 部分业务逻辑细节
- 前端Vue组件
- K8s部署配置

详见: [COMPLETE_GUIDE.md](COMPLETE_GUIDE.md)

## 🛠 技术栈

**后端**: FastAPI + PostgreSQL + Redis + Docker + K8s  
**前端**: Vue 3 + TypeScript + Vite  
**AI**: OpenClaw + Zhipu AI (GLM-4)

## 👥 作者

**tom** (需求设计) | **Claude** (代码实现)
