---
status: active
category: meta
audience: all
last_updated: 2026-08-05
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
| admin 分类详情页 UI 修复：标题对齐/Markdown XSS/a11y（PR #200） | ✅ 完成 | 2026-04-22 |
| agent-service/eval-service 服务拆分与测试覆盖（PR #309, #310） | ✅ 完成 | 2026-05-21 |
| admin SOP 文档详情弹窗决策树高保真与自适应渲染优化 (PR #353, #355) | ✅ 完成 | 2026-05-29 |
| KBD/QFK 安全非 JSON 行列提取与完整输出变量 | ✅ 完成 | 2026-07-27 |
| KBD 多条历史 QFK 管道的分步修复与统一保存 | ✅ 完成 | 2026-07-27 |
| KBD 自动执行门禁与 HTP 人工升级确认修复 | ✅ 完成 | 2026-07-27 |
| Terminal Bridge 真实 UI 入口信任边界、Alloy 与端到端可观测性 P0 | ✅ 真实案例重验与全链路关联完成 | 2026-07-30 |
| 运行时代码完整性防护（禁止源码热补丁覆盖镜像） | ✅ 完成 | 2026-07-27 |
| KBD QKV/QFK 三信号执行闭环与 39 MB 大输出边缘筛选 | ✅ 代码与自动验证完成，待 PR 部署后现场复测 | 2026-07-27 |
| KBD 关键信号结果用户化与 `ps -p PID -o cmd=` 提取契约 | ✅ 代码与 59 项自动验证完成，待修正 KBD revision 后现场复测 | 2026-07-28 |
| KBD 截图证据与可执行诊断契约 | 🟡 126/126 来源完整；122 条自动 Proposal；4 条工程 Contract fixture 仅完成 Handler Build/Decision Replay；0/126 专家 Gold | 2026-07-29 |
| KBD 专家复核、不可变版本与 Capability 闭环 | 🟡 静态审核、Vision/Signal 编辑、published maintenance working、统一写门禁和 Agent Runtime Discovery 已完成；可信 SSO、真实 replay、评估导出待实施；0/126 Expert Gold | 2026-07-30 |
| KBD 专家 Signal 编辑与执行契约一致性 | ✅ 代码级完成：角色单一事实来源、编辑 Contract 自动同步、草稿/发布门禁分层；KBD30880 UI 人工验收待部署后执行，不计 Expert Gold | 2026-07-30 |
| KBD Expert 发布与 Agent 消费一致性 | 🟡 代码级完成：独立发布盖章、Agent freshness、旧角色冲突规范化、替代证据、END 与 df Use% 修复；部署后 KBD30880/40061 真实 replay 待完成，0/126 Expert Gold | 2026-07-31 |
| QFK 系统执行域与统一文本取值 | 🟡 代码级完成：aCLI `--container` 与 Bridge container 边界分离；Matcher/产出变量复用安全 TextExtract；真实 HCI、Admin 构建和 KBD40061 replay 待验 | 2026-07-31 |
| KBD 专家监督与运行效果数据闭环 | 🟡 已完成可独立产生的数据：原因码/删除原因、精确 Agent runtime revision、CDD 编译/逐 Signal outcome、Capability Gap、运行指标和评估导出；可信 SSO、Replay、真实客户执行、0/126 Expert Gold、Challenger 仍未完成 | 2026-07-31 |
| KBD 最小回放证据契约与正式专家复核启动 | 🟡 运行审计已保存不可变版本、哈希和 Terminal Bridge artifact 查找键；正式专家复核可开始积累，但 manifest 明确不可重放，0/126 Expert Gold | 2026-07-31 |
| KBD Pipeline Stage 6 运行时契约预检 | ✅ 代码级完成：标准源码入口自动加载同 checkout 的 `backend/shared`，包含审计的完整流水线在任何生产 Stage 前预检；KBD40061 完整环境重跑待人工确认，不计 Expert Gold | 2026-07-31 |
| KBD 截图 Evidence v3 展示与单图专家确认 | 🟡 代码级完成：正文卡片展示 OCR、Observed Facts 与语义确认状态；`reviewed_image_seqs` 只接受严格非负整数，未确认图片 Evidence 不被补写；hci-dev UI 人工验收待新镜像部署 | 2026-08-03 |
| KBD Candidate 三态门禁与批量自查 | 🔄 专家修复/删除边界已完成；第四批 KBD32300/33510/33882/34094/34164 revisions 142～146 真实同批复验通过；后续批次继续按每批 5 篇推进 | 2026-08-04 |
| KBD 关键信号输入边界隔离 | ✅ 代码级完成：仅诊断章节截图的原子观察可进入 Prompt；根因/方案/未知章节图片与截图上下文 fail closed，Candidate 仅能引用本轮实际输入的图片区域，不要求正文 evidence 与图片 OCR 跨来源逐字相等；KBD27736 重抽仍须专家触发 | 2026-08-04 |
| KBD 人工复核标签三态纠偏 | ✅ 代码与自动验证完成：普通 Signal 无标签；待复核 Signal 按专家是否保存显示黄/绿；详情 API 从现有 Revision 派生，不新增数据库状态 | 2026-08-04 |
| KBD 关键信号统一过滤、取值与输出 | ✅ 代码与自动验证完成：same_record 包含/排除独立关系、完整行/文本行列/JSON、可选 AI、Match/Produce、qfk_log 有界保存与四类完整链路矩阵；真实 HCI replay 待部署后执行 | 2026-08-05 |
| KBD 关键信号两步处理交互统一 | ✅ 代码与自动验证完成：匹配模式和每个产出变量统一为“处理单元 → 第一步取值 → 第二步判断/产出”；取值/判定关键字独立，交互与样式由共享组件保证一致 | 2026-08-05 |
| hci-real/hci-sim 双轨与 100+ Agent 并发回归 | 🟡 A/B Runtime 与 C–E 控制面代码级基础已实现；C1 已对 dev 126 条 KBD 完成只读 active snapshot/Tool Contract 基线（2 条待 Artifact 绑定、4 条 Tool stale、120 条未发布）；C2 已补 Artifact Gate、payload digest 和对象存储参考契约，真实 Artifact/生产 CAS/Bridge E2E、20-repeat、真实校准与 100+ 并发仍未验证 | 2026-08-06 |

**当前关注点**：P1 知识库重建（[task/knowledge-base/知识库任务.md](task/knowledge-base/知识库任务.md)）继续推进；hci-sim A/B 已通过 PR CI，C–E 的控制面代码级基础见[实施验证报告](verify/events/2026-08-06-hci-sim阶段C-E控制面代码级实施验证报告.md)，C1 的权威 KBD 读取和 126 条基线见[专项报告](verify/events/2026-08-06-hci-sim阶段C1权威KBD解析与全量能力验证报告.md)，C2 的 Artifact Gate/对象完整性参考实现见[专项报告](verify/events/2026-08-06-hci-sim阶段C2获批Artifact与不可变BundleRegistry验证报告.md)。下一步须获得真实 Artifact 与对象存储授权，接入生产 PostgreSQL CAS/outbox；不得把 `ready_for_artifact_binding` 或 C2 内存测试写成已编译、真实 SSH、差分或 100+ 并发通过。[KBD 专家信号修复与删除可用性](task/knowledge-base/events/2026-08-04-KBD专家信号修复与删除可用性任务.md)已完成代码与第四批真实重抽，后续仍按 [Candidate 三态门禁批量任务](task/knowledge-base/events/2026-08-04-KBD关键信号Candidate三态门禁与批量自查任务.md)每批 5 篇推进。真实执行、可信 Expert Gold 与 100+ 并发仍按既有事实边界推进。

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
| [solution/agent/02-架构设计/agent设计.md](solution/agent/02-架构设计/agent设计.md) | AI 助手架构、Pod 池调度、AI协议设计 |
| [solution/agent/README.md](solution/agent/README.md#hci-sim-ae-设计导航) | hci-sim A～E 目标架构、严格依赖和当前 proposed 状态 |
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
| [task/agent/events/2026-08-05-hci-sim阶段A目录收敛与基础门禁任务.md](task/agent/events/2026-08-05-hci-sim阶段A目录收敛与基础门禁任务.md) | hci-sim 当前实施入口；B～E 依次受前置门禁阻断 |
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
