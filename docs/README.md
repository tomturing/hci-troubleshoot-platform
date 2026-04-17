---
status: active
category: meta
audience: all
last_updated: 2026-04-17
owner: team
update_trigger: 每个工作循环完成后（新功能上线 / 阶段里程碑达成）必须更新第一屏
---

# HCI 智能排障平台 — 冷启动入口

> **写给所有读者：无论你是 AI Agent、新成员还是离开数月后回来的自己——  
> 读完第一屏（约 30 秒），你可以独立判断"系统现在是什么状态"。**

---

## 第一屏：系统现状（30 秒）

### 系统是什么

**HCI 排障助手 = 双轨知识注入 + 三级 Fallback + 六阶段诊断状态机**

```
用户描述故障
   ↓
[双轨知识检索]
   ├── SOP 轨道：症状匹配 SOP 手册 → 注入「SOP排障流程」→ AI 按步骤执行
   └── KB 轨道：语义检索历史案例  → 注入「历史案例参考」→ AI 提取假设

[三级 Fallback]
   SOP 命中 > 案例命中 > 机制推理（标注【机制推理】，不拒绝回答）

[六阶段诊断]
   S0 意图识别 → S1 故障定位 → S2 假设生成 → S3 验证执行 → S4 根因确认 → S6 验证闭环
```

### 当前阶段

> ⚠️ **此处需在每个工作循环完成后更新（owner: team）**

| 里程碑 | 状态 | 完成日期 |
|--------|------|---------|
| P0 系统基线修复（5段式 Prompt + 测试覆盖） | ✅ 完成 | 2026-03-23 |
| P1 知识库重建（知识原子结构） | 🔄 进行中 | — |
| P2 诊断状态机代码落地 | ✅ 完成 | 2026-04-07 |
| P3 ReAct 引擎与工具接入 | ⚠️ 执行器已实现，文档待补 | — |
| P4 工具扩展与数据管道 | 🔲 待启动 | — |
| dashscope 多模型直连（PR #158） | ✅ 完成 | 2026-04-16 |
| GitOps App of Apps 分层架构（PR #159） | ✅ 完成 | 2026-04-16 |
| nginx 动态 DNS 解析修复（PR #160） | ✅ 完成 | 2026-04-17 |

**当前关注点**：P1 知识库重建（[task/knowledge-base/知识库任务.md](task/knowledge-base/知识库任务.md)）；dashscope 多模型已启用（qwen3.5-plus / qwen3-max / glm-4.7 / kimi-k2.5）

### 冷启动阅读路径

按顺序读以下文件，即可独立开始贡献：

1. [solution/架构设计.md](solution/架构设计.md) — 了解整体架构（10 分钟）
2. [deploy/部署指南.md](deploy/部署指南.md) — 本地 K3s 部署与生产环境部署（15 分钟）
3. [task/](task/) — 看当前进行中的任务（5 分钟）
4. [文档管理规范.md](文档管理规范.md) — 了解如何维护文档（5 分钟）

---

## 第二屏：按需查阅

### 系统设计

| 主干文档 | 说明 |
|---------|------|
| [solution/架构设计.md](solution/架构设计.md) | 整体架构分层、微服务拓扑、交互关系 |
| [solution/数据库设计.md](solution/数据库设计.md) | 数据模型、表结构、迁移策略 |
| [solution/接口设计.md](solution/接口设计.md) | REST API 规范、WebSocket 协议、错误码 |
| [solution/可观测性设计.md](solution/可观测性设计.md) | OTel 链路追踪、Loki 日志、Grafana 看板 |

| 分支文档 | 说明 | 对应架构组件 |
|---------|------|----------|
| [solution/ai-assistant/AI助手设计.md](solution/ai-assistant/AI助手设计.md) | AI 助手架构、Pod 池调度、AI协议设计 |
| [solution/knowledge-base/知识库设计.md](solution/knowledge-base/知识库设计.md) | RAG 摄入 + 检索流水线、KBD + SOP 两轨 |
| [solution/custom-ui/客户端设计.md](solution/custom-ui/客户端设计.md) | WebSocket 生命周期、UI 状态机、aClient 采集 |
| [solution/case/工单设计.md](solution/case/工单设计.md) | 工单生命周期、Case 状态机、评分触发 |
| [solution/conversation/对话设计.md](solution/conversation/对话设计.md) | 消息处理、P4 ReAct 引擎、3-Tier Prompt 组装 |

历史决策事件见 [solution/events/](solution/events/)（知识工程方案选型、RAG 对接架构决策等）

### 部署操作

| 文档 | 说明 |
|------|------|
| [deploy/部署设计.md](deploy/部署设计.md) | 部署架构全量：K3s 拓扑图 + GitOps + Helm Chart 结构 |
| [deploy/部署指南.md](deploy/部署指南.md) | 本地 K3s + 生产环境完整部署操作手册 |
| [deploy/发布指南.md](deploy/发布指南.md) | 发布流程 + ArgoCD 接入 + 回滚 SOP |
| [deploy/部署管理规范.md](deploy/部署管理规范.md) | 脚本分类体系、配置分层、密钥管理规则 |
| [deploy/pitfalls/_index.md](deploy/pitfalls/_index.md) | 部署类避坑路由索引（AI Agent 必读） |

### 验证与测试

| 文档 | 说明 |
|------|------|
| [verify/测试指南.md](verify/测试指南.md) | 单测/集成/E2E 测试策略与执行方法 |
| [verify/pitfalls/_index.md](verify/pitfalls/_index.md) | 验证类避坑路由索引（AI Agent 必读） |

### 当前任务

| 文档 | 说明 |
|------|------|
| [task/架构任务.md](task/架构任务.md) | 系统架构层任务 |
| [task/数据库任务.md](task/数据库任务.md) | 数据库任务（含迁移） |
| [task/case/工单任务.md](task/case/工单任务.md) | 工单模块任务 |
| [task/conversation/对话任务.md](task/conversation/对话任务.md) | 对话模块任务 |
| [task/ai-assistant/AI助手任务.md](task/ai-assistant/AI助手任务.md) | AI 助手层任务 |
| [task/knowledge-base/知识库任务.md](task/knowledge-base/知识库任务.md) | 知识库 RAG 任务（当前重点） |
| [task/custom-ui/客户端任务.md](task/custom-ui/客户端任务.md) | 客户端任务 |
| [task/events/](task/events/) | 历史任务事件记录 |

### 需求文档

| 文档 | 说明 |
|------|------|
| [requirement/需求说明.md](requirement/需求说明.md) | 完整产品需求规格、用户故事、MVP 范围 |
| [requirement/events/](requirement/events/) | 历史需求事件 |

---

## 文档管理

文档更新规则详见 [文档管理规范.md](文档管理规范.md)。

历史归档见 [archive/README.md](archive/README.md)。
