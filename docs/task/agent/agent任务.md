---
status: active
category: task
audience: developer
last_updated: 2026-07-31
owner: team
update_trigger: Agent 层功能新增/重构/问题修复任务
---

# 任务：Agent 层

> 对应方案文档：[Agent 设计](../../solution/agent/02-架构设计/agent设计.md)

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-08-04 | v2.8 | 收敛 KBD27736 图片来源门禁：保留四个诊断字段图片白名单、自由上下文隔离和 source ref 实际输入集合检查；移除正文 evidence 与截图 OCR 的跨来源逐字强绑定，恢复后续运行语义门禁的真实拒绝原因。 | [KBD关键信号图片来源门禁收敛修复任务](events/2026-08-04-KBD关键信号图片来源门禁收敛修复任务.md) |
| 2026-08-04 | v2.7 | 修复 KBD27736 截图 Evidence 输入边界泄漏：根因/方案图片、上下文及未知章节 fail closed；初版实现额外加入逐字 evidence 强校验，后由 v2.8 收敛。 | [KBD关键信号输入边界泄漏修复任务](events/2026-08-04-KBD关键信号输入边界泄漏修复任务.md) |
| 2026-07-31 | v2.6 | 启动正式专家复核并在 Agent KBD 运行审计写入最小 Replay manifest：精确版本、计划、环境/参数哈希、逐 Signal evaluation 和 Terminal Bridge artifact 指针；不复制现场输出且不宣称完整 Replay。 | [KBD 最小回放证据契约与正式专家复核启动任务](events/2026-07-31-KBD最小回放证据契约与正式专家复核启动任务.md) |
| 2026-07-31 | v2.5 | 完成可独立产生的 KBD 数据闭环：reason_code、专家删除原因、精确 runtime revision、CDD 编译/逐 Signal outcome、Capability Gap、使用效果/失败模式与评估导出；SSO、Replay、真实客户执行和 Champion/Challenger 仍待后续阶段。 | [KBD 专家监督与运行效果数据闭环任务](events/2026-07-31-KBD专家监督与运行效果数据闭环任务.md) |
| 2026-07-31 | v2.4 | qfk_system 的 aCLI `--container` 与 Bridge container 执行边界分离；Matcher 和产出变量共用 TextExtract，替代 df 特判；真实 HCI/Agent replay 待实施。 | [QFK 系统执行域与统一文本取值任务](./events/2026-07-31-QFK系统执行域与统一文本取值任务.md) |
| 2026-07-31 | v2.3 | Expert 发布增加当前 Tool Contract 独立盖章，Agent 保留真实 Handler/DAG 编译；QKV END 统一、task/alert/dialog 锚点优先级确定化，threshold 正确解析 df Use%。真实客户环境 replay 仍待实施。 | [KBD 发布消费一致性任务](./events/2026-07-31-KBD发布消费一致性与专家审核易用性任务.md) |
| 2026-07-30 | v2.2 | KBD 轻治理纵向切片已实施：复用现有审核页、最小 Proposal/Expert Revision、published maintenance working、Vision/Signal 专家编辑、静态 Validation、统一写门禁和 Capability Runtime Discovery。当前仍无可信 Admin SSO、真实 Agent replay、reason_code/评估导出；122 Proposal 尚未开始正式专家复核。 | [KBD专家复核与全生命周期闭环实施任务](./events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |
| 2026-07-29 | v2.1 | 原重治理方案归档；随后已按用户确认收敛为轻治理、自动化优先，不采用独立工作台、双审、多表 Release 或 Capability Registry | [KBD专家复核与全生命周期闭环实施任务](./events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |
| 2026-07-29 | v2.0 | 完成 222/222 Vision Evidence 数据库工程审计与 Agent 推断硬隔离；Signal Prompt 升级 v1.3/revision 8，补齐 rejected candidates、零信号 needs_review、保存时运行语义门禁、安全日志 basename 和诊断/处置 Contract 归一；新增 126 条分层验证报告 | [KBD截图证据与可执行诊断契约实施任务](./events/2026-07-28-KBD截图证据与可执行诊断契约方案.md) |
| 2026-07-29 | v1.9 | 原 39 条扩展为 126 条真实语料；126/126 来源、222 张图片与 52 条零图完整性通过；修复抓取失败标记、数据库 DSN 方言和 Vision Prompt 漂移；当时将 4 条工程 Contract fixture 简称 Gold，已由 v2.1 纠正，当前仍为 0/126 Expert Gold | [KBD截图证据与可执行诊断契约实施任务](./events/2026-07-28-KBD截图证据与可执行诊断契约方案.md) |
| 2026-07-28 | v1.7 | 完成 KBD 截图证据、信号抽取与案例自动验证的现状审计，确认关键信号为必要原子层，并规划 Evidence/Compiler/Replay/Case Verification Contract 分阶段实施 | [KBD截图证据与可执行诊断契约实施任务](./events/2026-07-28-KBD截图证据与可执行诊断契约方案.md) |
| 2026-07-27 | v1.6 | 完成 KBD 三信号执行闭环：显式 QKV acquisition、QFK 边缘筛选、工具事件协议与持久化、保存/运行双门禁、KBD 27123 revision 17 | [KBD27123三信号执行闭环任务](../events/2026-07-27-KBD27123三信号执行闭环任务.md) |
| 2026-07-25 | v1.5 | **检索 query 提炼优化（PR #616）**：`InvestigationAgent._build_retrieval_query` 过滤 S0 控制符（`①`/`继续`等），支持提取首条真实用户主诉症状 | [2026-07-25-KBD向量搜索失效根因分析与修复](../../verify/events/2026-07-25-KBD向量搜索失效根因分析与修复.md) |
| 2026-06-21 | v1.4 | Skill 调用失效修复（PR #475）：实施分层改进方案 — P0（preferred_next_steps 嵌入 sop_advance/get_sop_node 返回体）+ P1（软推荐门禁层 skill_call/tool_call）+ P2（S0/S1 系统提示词变量采集规范） | [skill调用失效根因分析与改进方案](../../solution/agent/02-架构设计/skill调用失效根因分析与改进方案.md) |
| 2026-06-20 | v1.3 | Skill 调用失效根因分析（工单 Q2026062036731 实例）：确认 `hci-alert-parsing`/`hci-disk-vendor-lifetime` 未触发根因为变量门禁盲区，输出分层改进方案（preferred_next_steps 嵌入 + 软推荐门禁层 + 提示词规范） | [skill调用失效根因分析与改进方案](../../solution/agent/02-架构设计/skill调用失效根因分析与改进方案.md) |
| 2026-06-08 | v1.2 | 排障 Agent 可靠性改造（PR #416）：阶段零~二完整落地，阶段三/四主体完成（T1-2 前端 exec_id 回传、T3-3 CoT 强制外显、T4-4 CI 回归门禁待后续 PR 整改），详见 [Agent 可靠性改造任务清单](./Agent可靠性改造任务清单.md) | [Agent 可靠性改造任务清单](./Agent可靠性改造任务清单.md) |
| 2026-05-31 | v1.1 | 助手类型命名统一（PR #369）：scheduler-service config.py 助手 display_name 改为 HTP/OPS/PAI Agent（移除 GLM-5 后缀），与 Helm configmap.yaml 同步 | — |
| 2026-04-05 | v1.0 | 初版 | [2026-04-02-S0意图识别与分类基线重构方案](../../solution/agent/events/2026-04-02-S0意图识别与分类基线重构方案.md) |

---

## 当前任务清单

| 状态 | 任务 | 创建日期 | 关联方案 |
|------|------|---------|---------|
| ✅ 完成（代码级；KBD27736 重抽仍须专家触发） | T-AGT-KBD-SIGNAL-IMAGE-REF-BOUNDARY：诊断截图白名单、截图上下文隔离与 source ref 实际输入集合检查；不要求正文 evidence 与图片 OCR 跨来源逐字相等 | 2026-08-04 | [KBD关键信号图片来源门禁收敛修复方案](../../solution/agent/events/2026-08-04-KBD关键信号图片来源门禁收敛修复方案.md) |
| ✅ 完成（代码级；真实 HCI 回归待做） | T-AGT-QFK-EXEC：aCLI 系统执行域、Bridge 边界和统一 TextExtract | 2026-07-31 | [QFK 系统执行域与统一文本取值方案](../../solution/agent/events/2026-07-31-QFK系统执行域与统一文本取值方案.md) |
| ✅ 完成（代码级；不代表可信身份或真实客户 replay） | T-AGT-KBD-DATA-CLOSURE：专家原因/删除原因、精确版本运行审计、Capability Gap、运行指标与评估导出 | 2026-07-31 | [KBD 专家监督与运行效果数据闭环方案](../../solution/agent/events/2026-07-31-KBD专家监督与运行效果数据闭环方案.md) |
| ✅ 完成（代码级；不代表完整回放） | T-AGT-KBD-REPLAY-MANIFEST：运行审计中的不可变版本、哈希与 Terminal Bridge artifact 最小引用；正式专家复核入口沿用现有轻治理审核页 | 2026-07-31 | [KBD 最小回放证据契约与正式专家复核启动方案](../../solution/agent/events/2026-07-31-KBD最小回放证据契约与正式专家复核启动方案.md) |
| ✅ 完成（代码级） | T-AGT-KBD-PUBLISH：发布盖章 freshness、END 标准变量、替代 QKV 优先级和 df 阈值解析 | 2026-07-31 | [KBD 发布消费一致性方案](../../solution/agent/events/2026-07-31-KBD发布消费一致性与专家审核易用性方案.md) |
| 进行中（静态审核与维护发布闭环已完成；可信身份、真实 replay、评估数据闭环待实施；0/126 专家 Gold） | [KBD 专家复核、版本治理与生产消费闭环](./events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) | 2026-07-29 | [完整方案](../../solution/agent/events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |
| 进行中（126/126 来源完成；4 条工程 Contract fixture 仅 Decision Replay；0/126 专家 Gold） | [KBD 截图证据与可执行诊断契约](./events/2026-07-28-KBD截图证据与可执行诊断契约方案.md) | 2026-07-28 | [系统级方案](../../solution/agent/events/2026-07-28-KBD截图证据与可执行诊断契约方案.md) |
| ✅ 已完成 | KBD 27123 三信号执行闭环与 39 MB 大输出边缘筛选 | 2026-07-27 | [KBD27123三信号执行闭环方案](../../solution/events/2026-07-27-KBD27123三信号执行闭环方案.md) |
| 进行中 | [Agent 可靠性改造（4 阶段）](./Agent可靠性改造任务清单.md) | 2026-06-08 | [Agent 可靠性三方案对比分析](../../solution/agent/02-架构设计/Agent可靠性三方案对比分析.md) |
| ✅ 已完成 | Skill 调用失效修复（PR #475）：变量门禁盲区 + preferred_next_steps 引导 + 系统提示词规范 | 2026-06-20 | [skill调用失效根因分析与改进方案](../../solution/agent/02-架构设计/skill调用失效根因分析与改进方案.md) |
