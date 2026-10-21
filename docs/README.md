---
status: active
category: meta
audience: all
last_updated: 2026-10-21
owner: team
update_trigger: 每个工作循环完成后（新功能上线 / 阶段里程碑达成）必须更新第一屏
---

# HCI 智能排障平台 — 冷启动入口

> **写给所有读者：无论你是 AI Agent、新成员还是离开数月后回来的自己——  
> 读完第一屏（约 30 秒），你可以独立判断"系统现在是什么状态"。**

---

## 第一屏：系统现状（30 秒）

### 系统是什么

**HCI 排障助手按已确认分类选择知识，现场证据决定自动诊断结论。**

```text
用户问题 → 确认分类
  ├─ 有 SOP 且未放弃：继续 SOP 排查
  └─ 无 SOP / 已明确回退：同分类 KBD
       ├─ 强信号与后续验证 → CDD 证据门禁 → 满足条件才确诊
       └─ 无可执行信号或证据不足 → 受控语义验证 / 未验证案例推荐
```

**KBD、信号、语义推荐、审核发布统一从 [知识库主题入口](solution/knowledge-base/README.md) 阅读。** 没有可用知识或证据时，不自动生成知识库外命令或虚构已确认根因。

### 当前阶段

> 当前工作区：无信号案例发布与标题推荐已完成本地实现和定向测试；文档已按主题收敛。尚未提交、部署或进行真实检索准确率评测。

<details>
<summary>最近 5 个里程碑（完成日期倒序，原文逐字保留在归档快照）</summary>

| 里程碑 | 状态 | 完成日期 |
|--------|------|---------|
| CI 主干后端门禁按影响范围收敛（纯前端/文档 push 跳过整层后端门禁） | ✅ 已实施 | 2026-09-16 |
| QKV 输出后处理（同一 Signal 内投影转换 / 特征提取 / 断言） | ✅ 文档与最小确定性闭环完成 | 2026-08-25 |
| hci-sim KBD 27123 纵向样板与生产化差距审查 | 🟡 样板通过，平台生产交付 BLOCKED | 2026-08-11 |
| KBD 六阶段任务管理 CLI 参数模型重构 | 🟡 代码与自动测试完成，真实环境验收待完成 | 2026-08-09 |
| CI 发布链路按影响范围收敛 | ✅ 已实施 | 2026-08-07 |

> **完整历史只看两处，本屏不再累积流水**：
> 发布事实源 = [CHANGELOG.md](../CHANGELOG.md)（release-please 自动生成）；
> 第一屏原记录表与 hci-sim 事件表 = [归档快照](archive/docs-slimming-2026-10/README冷启动入口-历史快照.md)（只读）。
</details>

**当前关注点（2026-09-23）**：区分知识推荐与现场确诊；保留 SOP 优先、强信号验证和分类完整候选，不用标题相似度覆盖反证。当前规范从[知识库主题入口](solution/knowledge-base/README.md)查询，实施及待验收项见[知识库任务](task/knowledge-base/知识库任务.md)。

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
| [solution/agent/design/agent设计.md](solution/agent/design/agent设计.md) | AI 助手架构、Pod 池调度、AI协议设计 |
| [solution/hci-sim/README.md](solution/hci-sim/README.md) | hci-sim A～E 目标架构、严格依赖和当前 proposed 状态 |
| [hci_sim/README.md](../hci_sim/README.md) | hci-sim 现行全量源码设计说明（代码/数据库/部署/CI/验收入口） |
| [solution/knowledge-base/README.md](solution/knowledge-base/README.md) | KBD/SOP、信号契约、CDD、语义推荐、审核操作和生产的统一入口 |
| [solution/knowledge-base/关键信号架构设计.md](solution/knowledge-base/关键信号架构设计.md) | 信号职责、运行契约、验收矩阵与专项文档导航 |
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
| [user-guide/](user-guide/) | 终端用户使用手册（在线诊断 / 离线诊断） |

### 验证与测试

| 文档 | 说明 |
|------|------|
| [verify/测试指南.md](verify/测试指南.md) | 单测/集成/E2E 测试策略与执行方法 |
| [verify/测试指南.md](verify/测试指南.md#三hci-sim-ae-分层验证) | hci-sim A～E 验证导航与历史证据边界 |
| [verify/pitfalls/_index.md](verify/pitfalls/_index.md) | 验证类避坑路由索引（AI Agent 必读） |

### 当前任务

| 文档 | 说明 |
|------|------|
| [task/架构任务.md](task/架构任务.md) | 系统架构层任务 |
| [task/数据库任务.md](task/数据库任务.md) | 数据库任务（含迁移） |
| [task/case/工单任务.md](task/case/工单任务.md) | 工单模块任务 |
| [task/conversation/对话任务.md](task/conversation/对话任务.md) | 对话模块任务 |
| [task/agent/agent任务.md](task/agent/agent任务.md) | AI 助手层任务 |
| [task/hci-sim/README.md](task/hci-sim/README.md) | hci-sim 当前实施入口；B～E 依次受前置门禁阻断 |
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

### 特殊文档类（非工作循环）

| 文档类 | 说明 | 入口 |
|--------|------|------|
| 终端普通用户使用手册 | 面向非开发者的操作结果视角文档 | [user-guide/](user-guide/) |
| 接口契约 | 机器可读的 OpenAPI / Protobuf 契约源文件 | [contracts/](contracts/) |
| 历史归档 | 已完成、冻结、不再演进的历史文档（只读） | [archive/README.md](archive/README.md) |

## 入口变更历史

| 日期 | 变更 |
|---|---|
| 2026-10-21 | 第一屏瘦身：里程碑表由 44 行缩为最近 5 条并明确「完整历史看 CHANGELOG + 归档快照」，hci-sim 事件表移出（其链接改由 solution/hci-sim/README.md 承载）。 |
| 2026-09-23 | 校正 SOP/KBD 与参考推荐的现行边界；历史里程碑折叠，增加知识库统一入口。 |
