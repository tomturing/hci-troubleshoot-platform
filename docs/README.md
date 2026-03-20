# HCI 智能排障平台 — 文档导航

> **阅读说明**：
> - 新成员上手 → 从[本地开发指南](guides/本地开发指南.md)开始
> - 准备发布 → 看[发布手册](guides/发布手册.md)
> - 了解架构决策 → 看 [adr/](adr/README.md)
> - 查阅技术规格 → 看 [reference/](reference/) 或 [architecture/](architecture/)
>
> **文档管理规范**：所有文档的分类原则、命名规则、生命周期见 [文档管理规范.md](文档管理规范.md)。

---

## 目录结构

```
docs/
├── 文档管理规范.md          ← 文档管理规则（必读）
├── README.md               ← 本文件（导航总图）
│
├── requirements/           ← 需求说明
├── architecture/           ← 系统架构与设计
├── adr/                    ← 架构决策记录
├── guides/                 ← 操作指南（How-to）
├── reference/              ← 参考资料（精确查阅）
└── archive/                ← 历史归档（只进不出）
```

---

## 需求说明

| 文档 | 说明 |
|------|------|
| [needs/需求说明.md](requirements/需求说明.md) | 产品需求规格、用户故事、MVP 范围 |

---

## 系统架构（architecture/）

| 文档 | 说明 |
|------|------|
| [系统架构.md](architecture/系统架构.md) | 整体架构分层、微服务拓扑、交互关系 |
| [数据库设计.md](architecture/数据库设计.md) | 数据模型、ER 图、表结构、迁移策略 |
| [接口设计.md](architecture/接口设计.md) | REST API 规范、WebSocket 协议、错误码表 |
| [可观测性设计.md](architecture/可观测性设计.md) | OTel 链路追踪、Loki 日志、Grafana 看板设计 |
| [AI助手层设计.md](architecture/AI助手层设计.md) | OpenClaw 架构、Session隔离、知识共享方案 |
| [知识库RAG设计.md](architecture/知识库RAG设计.md) | RAG 数据管道、检索引擎、知识生命周期 |
| [客户端设计.md](architecture/客户端设计.md) | 前端对话 UI 与管理控制台设计 |

---

## 架构决策记录（adr/）

> 记录每一个重要架构选型的背景、选项与结论。详见 [adr/README.md](adr/README.md)。

| 编号 | 决策 |
|------|------|
| [ADR-001](adr/001-选择K3s作为生产编排.md) | 选择 K3s 而非标准 K8s |
| [ADR-002](adr/002-GitOps双仓模型.md) | GitOps 双仓模型设计 |
| [ADR-003](adr/003-RAG对接架构决策.md) | AI 层与 RAG 层对接方式三阶段演进 |
| [ADR-004](adr/004-发布工作流方案选型.md) | 发布工作流方案选型（方案21 vs 方案22） |
| [ADR-005](adr/005-Helm-Chart资源归属拆分.md) | Helm Chart 资源归属拆分（infra/data/obs 三层，消除 ArgoCD 资源竞争） |

---

## 操作指南（guides/）

| 文档 | 说明 | 适用场景 |
|------|------|---------|
| [本地开发指南.md](guides/本地开发指南.md) | 本地环境搭建、开发流程、调试技巧 | 新成员上手 |
| [测试指南.md](guides/测试指南.md) | 单测/集成/E2E 测试策略与执行方法 | 开发提测 |
| [生产运维指南.md](guides/生产运维指南.md) | 生产环境配置、巡检、扩容指南 | 值班运维 |
| [**发布手册.md**](guides/发布手册.md) | **完整发布生命周期：流程+ArgoCD接入+检查清单+回滚SOP** | **每次发布** |
| [项目交付标准化.md](guides/项目交付标准化.md) | 业界最佳实践对比、差距分析、整改计划 | 规范建设 |

---

## 参考资料（reference/）

| 文档 | 说明 |
|------|------|
| [K8s部署规格.md](reference/K8s部署规格.md) | 四层 Helm Chart 参数、K3s+ArgoCD GitOps 部署说明、资源规格 |
| [评分机制.md](reference/评分机制.md) | AI 回复质量评分体系与评价系统设计 |
| [SSH终端交互.md](reference/SSH终端交互.md) | 侧边栏 SSH 登录与终端交互功能规格 |
| [脚本与配置管理规范.md](reference/脚本与配置管理规范.md) | scripts/ 分类体系、脚本头部规范、deploy/ 配置分层说明、密钥管理规则 |

---

## 归档（archive/）

历史文档存档，详见 [archive/README.md](archive/README.md)。
