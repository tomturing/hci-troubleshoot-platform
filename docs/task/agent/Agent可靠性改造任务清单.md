---
status: active
category: task
audience: developer
last_updated: 2026-08-03
related_prs:
  - PR #474: invoke() 重试 + tool_calls 清理 + skill 可观测 + 报告模板简化 + solution 格式合并
owner: team
---

# Agent 可靠性改造任务清单

> **关联方案** → [Agent可靠性三方案对比分析](../../solution/agent/Agent可靠性三方案对比分析.md)
>
> **执行策略**："方案 C 为魂，方案 B 为骨，方案 A 为理"融合落地。
> 方案 C 的领域模型是目标终态，方案 B 的渐进路线是执行手册，方案 A 的第一性原理是决策防偏指南。

## 变更历史

| 日期 | 版本 | 变更内容 |
|------|------|---------|
| 2026-08-03 | v3.49 | **QFK 只读命令编译预览**：Agent 新增内部预览端点，复用运行中 `BackendSignal` 与 `HandlerRegistry` 编译完整 aCLI 模板；保留运行时变量、明确 host 只用于 SSH 路由，预览不进入执行器。 | PR #663 |
| 2026-08-03 | v3.48 | **QFK producer 执行链修复**：显式区分 produce/match，lsof 成功输出可进入 PID 变量池；qfk_system 收敛为唯一 argv 命令模型。 | [KBD27123产出变量执行链与QFK命令参数模型收敛任务](../events/2026-08-03-KBD27123产出变量执行链与QFK命令参数模型收敛.md) |
| 2026-07-31 | v3.47 | **KBD Matcher/行选择模式与 Extract 契约收敛**：修复 LLM 将 `rows.include_mode=any/all` 误写为 `match.mode` 的 Prompt 歧义；backend Matcher 强制复用 `produces` 的声明式 Extract，`match.mode` 收口为 `or/and/not`，JSON 路径收口至 `extract.type=json`。新增迁移 017 和 Prompt/花括号契约回归测试；不添加 `any→or` / `all→and` 旧兼容，非法候选只保留审计后重新抽取。 | [KBD Matcher 模式与 Extract 契约对齐](../../solution/events/2026-07-31-KBD-Matcher-模式与Extract契约对齐.md) |
| 2026-07-31 | v3.46 | **KBD Prompt JSON 花括号转义修复**：015 热加载 Prompt 的新增 JSON 示例未按 Python `str.format()` 转义，导致 `key`/`mode`/`name` 被误判为非法运行时占位符并使重抽 HTTP 500；016 将已部署数据修正为 `{{`/`}}` 字面量并将 Prompt 提升为 v1.5，新增 seed 与数据迁移的 StrictPromptLoader 回归校验。 | [KBD 重抽取与任务详情截图语义收口](../../solution/knowledge-base/events/2026-07-31-KBD重抽取与任务详情截图语义收口.md) |
| 2026-07-31 | v3.45 | **Prompt 与重抽版本一致性**：`kbd_extract_signals_v2` 热加载 Prompt 只教授声明式 Extract 和 `or/and/not`；KBD 重抽写入新 Proposal revision，草稿旧 Expert 指针不再冒充当前稿；任务详情弹窗按可见任务字段确定性归类为任务截图。 | [KBD 重抽取与任务详情截图语义收口](../../solution/knowledge-base/events/2026-07-31-KBD重抽取与任务详情截图语义收口.md) |
| 2026-07-31 | v3.44 | **QFK 声明式取值统一**：Matcher 与 produces 强制共用新版 `ValueExtract`；文本行可按关键字或行号选择，列可按表头或列号提取；删除旧单列 TextExtract、QFK `produces.path`、无 extract 全文判定和 `json_path` Matcher，变量写入保持原子。 | [QFK 声明式取值与匹配模式统一方案](../../solution/agent/events/2026-07-31-QFK取值先行与全模式安全管道统一方案.md) |
| 2026-07-31 | v3.43 | **KBD 最小 Replay artifact 契约**：在既有精确 revision 运行审计中追加 `replay_manifest`，记录不可变 KBD checksum、计划、环境/参数哈希、逐 Signal evaluation 与 Terminal Bridge artifact 查找键；不复制 stdout/stderr，明确 `replayable=false`，尚未形成 Evidence/Execution Replay。 | [KBD 最小回放证据契约与正式专家复核启动方案](../../solution/agent/events/2026-07-31-KBD最小回放证据契约与正式专家复核启动方案.md) |
| 2026-07-31 | v3.42 | **KBD 专家监督与运行效果数据闭环**：专家 revision 保存受控原因码和删除说明；Agent 按检索时加载的精确 Dynamic Resource revision 审计 CDD 编译、逐 Signal outcome 和失败模式，审计失败不阻断诊断。运行指标、Capability Gap 与评估导出只承载实际已知事实，不冒充客户 replay 或 Expert Gold。 | [KBD 专家监督与运行效果数据闭环方案](../../solution/agent/events/2026-07-31-KBD专家监督与运行效果数据闭环方案.md) |
| 2026-07-31 | v3.41 | **KBD Expert 发布/Agent 消费一致性**：发布盖章与 LLM 生成指纹分离；Agent 优先校验当前盖章并继续 Handler/DAG 编译；END 标准化、task 锚点优先级与 df Use% 阈值改为确定性语义。qfk_system host/容器边界本轮未改。 | [KBD 发布消费一致性方案](../../solution/agent/events/2026-07-31-KBD发布消费一致性与专家审核易用性方案.md) |
| 2026-07-30 | v3.40 | **KBD 执行 Contract 单向投影**：专家与 LLM 写路径以 `signals[].id + role` 生成 evidence policy；Agent 编译器消费持久化 Contract，不在现场反向改写知识。对绕过新写路径的历史不一致数据保持 Contract 兼容边界。 | [KBD 专家信号编辑与执行契约一致性方案](../../solution/agent/events/2026-07-30-KBD专家信号编辑与执行契约一致性方案.md) |
| 2026-07-30 | v3.39 | **KBD Capability 运行时发现**：Agent 新增内部只读探测端点，按当前进程真实状态报告 QKV/QFK Validator、HandlerRegistry、Terminal Bridge Executor 和 usable；Gateway 与 shared Descriptor 合并，Agent 不可达时保持 unknown。该状态用于平台诊断，不再冒充专家可处理的逐 KBD 告警。 | [KBD 轻治理闭环方案](../../solution/agent/events/2026-07-29-KBD专家复核与全生命周期闭环方案.md) |
| 2026-07-30 | v3.38 | **PR #644：qfk_log 统一契约与 KBD Pipeline 收敛**：将日志信号审计领域逻辑迁入 `data-pipeline/kbd/log_signal_audit.py`，通过唯一 `kbd.run` 入口统一关键信号抽取和只读审计；KBD DAG 扩展为 FETCH→IMPORT→VISION→CLASSIFY→EXTRACT_SIGNALS→AUDIT_LOG_SIGNALS；qfk_log、qkv_dialog、aCLI/运行时能力边界与专家复核清单同步固化。 | [qfk_log统一日志采集解析与判定设计](../../solution/agent/02-架构设计/qfk_log统一日志采集解析与判定设计.md) |
| 2026-07-29 | v3.37 | **HCI/aCLI 实机契约审计与 PR #641 CI 收敛**：完成 HCI 6.11.1_R1 + aCLI 1.0.0 的只读知识采集；修复 Signal Schema 合法 fixture 的 `file/path` 旧写法并增加完整路径反例；形成日志、blackbox、配置、数据、补丁、容器、设备 manifest、自观测污染与能力漂移基线。运行语义改造在用户确认后分 P0-P4 实施。关联：[HCI底层目录日志容器与aCLI知识基线](../../solution/agent/02-架构设计/HCI底层目录日志容器与aCLI知识基线.md) |
| 2026-07-29 | v3.36 | **Terminal Bridge 真实入口 P0 修复**：删除普通 Markdown `CommandBlock → ssh_input` 自动执行旁路；S0 在 LLM 前拒绝显式命令执行请求并阻断无工具证据的伪造输出；Alloy 固化资源、探针、指标抓取与流水线告警。状态：自动化回归已启动，真实 S1/ReAct 正向与 S0/Markdown 负向验收通过前不得宣称完整可观测，也不得进入 hci-sim。 |
| 2026-07-28 | v3.35 | **KBD 关键信号结果用户化与 ps 输出契约修正**：主报告改为检查说明/状态/结果/结构化产出，技术 ID 与原始输出留在审计层；golden chain 使用 `ps -p PID -o cmd=` 产出 CMD，并固定旧 PID include 会过滤 cmd 输出的反例。关联：[KBD关键信号结果展示与ps输出提取方案](../../solution/events/2026-07-28-KBD关键信号结果展示与ps输出提取方案.md) |
| 2026-07-28 | v3.34 | **S0 分类稳定身份与原子推进**：修复工单 `Q2026072855923` 中 VM 名被当成分类、点击③却进入存储-020 的复合错误；Agent 候选与 active 分类交集，UI 回传 category code，Conversation 保留原 optionId 并原子提交 category/S1。关联：[S0 分类稳定身份协议与候选治理方案](../../solution/events/2026-07-28-S0分类稳定身份协议与候选治理方案.md) |
| 2026-07-28 | v3.33 | **KBD 大输出聚合副本与 exec-result 纵深防御**：修复旧 terminal_bridge 的 40 MB `output` 绕过 stdout/stderr 筛选并导致 Gateway OOM；UI 统一重建兼容 output，Gateway 增加 JSON 解析前 2 MiB 门禁，Conversation 增加 256 KiB 契约并修复 yield Session 被提前关闭。关联工单 `Q2026072785259`；关联：[KBD27123三信号执行闭环方案](../../solution/events/2026-07-27-KBD27123三信号执行闭环方案.md)。 |
| 2026-07-27 | v3.32 | **KBD 三信号执行闭环**：QKV 必须显式现场 acquisition；QFK 大输出字面量筛选前移到 terminal_bridge；统一工具卡片 args/result/status、exec_id 持久化和流中断终态；KBD 27123 修正为定向三步变量链。关联：[KBD27123三信号执行闭环方案](../../solution/events/2026-07-27-KBD27123三信号执行闭环方案.md)。 |
| 2026-07-27 | v3.31 | **QFK 非 JSON 完整输出行列提取**：新增受控 text extract、stdout/stderr 完整缓存读取、稳定错误码和 Fail Closed；KB 增加 grep/awk/cut 确定性转换；requires 从占位符推导；管理端提供简化审核 UI。关联：[QFK非JSON结果行列提取方案](../../solution/events/2026-07-27-QFK非JSON结果行列提取方案.md)。 |
| 2026-07-27 | v3.30 | **QFK 产出变量、宿主机执行与超时链路（PR #622）**：① QFK `match` 与 `orchestrate.produces` 强制二选一，产出结果写入变量池；② `qfk_system.container=host` 直接在宿主机执行；③ timeout 从 Agent 透传至 terminal bridge，并在独立 SSH session 超时后关闭会话。 |
| 2026-07-26 | v3.29 | **v2 信号契约分层解包与容错解析（PR #620）**：① `kbd_model.py` 的 `kbd_from_dict()` 增加 dict 信封解包容错，支持兼容 API 标准 list 与 DB 原始 dict 形态；② 配合 kb-service 检索接口剥离存储信封，透出规范 `List[Signal]` 数组；③ 新增架构选型文档《关键信号数据结构选型分析与分层治理方案》 |
| 2026-07-25 | v3.28 | **KBD 向量检索正确性修复（PR #617）**：① 移除 KBD 向量检索中的 hash/BGE 伪向量兜底，embedding 生成失败时诚实降级到词法检索；② 增加 embedding 结果校验、模型与内容 hash 溯源字段、最小相似度阈值和模型一致性过滤；③ 发布与查询统一使用 jieba/HCI 分词，删除按时间兜底返回无关结果的逻辑；④ 增加 `backend/kb-service/app/cli/rebuild_kbd_search_index.py` 索引重建 CLI；⑤ 规范 `dynamic_resource_usage_audit` 状态语义；⑥ 贯通 `conversation_id` / `case_id` 到 KBD 检索链路 |
| 2026-07-25 | v3.27 | **InvestigationAgent 检索 query 提炼优化（PR #616）**：`InvestigationAgent._build_retrieval_query` 过滤 S0 阶段点击控制符（`①`/`继续`等），支持提取首条真实用户主诉症状 | [2026-07-25-KBD向量搜索失效根因分析与修复](../../verify/events/2026-07-25-KBD向量搜索失效根因分析与修复.md) |
| 2026-07-24 | v3.26 | database/seeds/02_system_prompts.sql 中 kbd_extract_signals_v2 关键信号抽取提示词字段名对齐 v2 扁平契约（command 替代 sub_command，instruction 替代 description）（PR #613） |
| 2026-07-23 | v3.25 | database/seeds/02_system_prompts.sql 中 kbd_extract_signals_v2 关键信号抽取提示词补充规则11（说明/关键字字段边界：禁止把检查说明塞进 resource_keyword 与 match.pattern）与 sig_004 正例（PR #609） |
| 2026-07-21 | v3.24 | **修复 terminal_bridge blog() Go 值传递导致 type 字段为空（PR #591）**：PR #587 合并后 stdout 输出仍显示 `"type":""`。根因是 Go 值传递：`blog()` 创建 logEntry → 值传递调用 `publish(e)` → `publish()` 内部修改 `e.Type = "bridge_log"` 只影响副本 → `blog()` 的 `json.Marshal(e)` 输出原始 e（Type 仍为空）。修复：在 `blog()` 的 Marshal 前补充 `e.Type = "bridge_log"` 赋值。此问题揭示 Go 结构体传参的隐晦陷阱：副本修改不影响原值，必须在值传递前完成所有字段赋值、或改用指针传递。关联工单：Q2026072171592 |
| 2026-07-21 | v3.23 | **terminal_bridge exec 命令日志回采完整实现（PR #589）**：① `execCommandIsolated()` 添加完整 bridge_log：`exec.start`（命令开始）、`exec.error`（错误详情+error_type分类）、`exec.done`（退出码+耗时+输出预览+success）；② `ssh_exec_process` 添加 `exec.request` 日志透传 trace_id；③ Migration 007：`bridge_execution_logs` 表新增 `exec_id`/`command`/`exit_code`/`duration_ms`/`stdout_len`/`stderr_len`/`output_preview`/`success`/`error_type` 字段+索引，支持完整的命令执行历史追踪和性能分析。关联工单：Q2026072160299，关联方案：[2026-07-21-terminal-bridge日志缺失根因分析与完整解决方案](../../solution/events/2026-07-21-terminal-bridge日志缺失根因分析与完整解决方案.md) |
| 2026-07-21 | v3.22 | **KBD 诊断引擎透传 case_id 到 QFK 执行链路（PR #585，补齐 PR #583 遗漏）**：PR #583 修复了底层链路但遗漏调用方 `kbd_differential.py`，导致 `qfk_exec` 调用时未传入 `case_id`，terminal_bridge 无法路由到正确的 SSH 会话。本次修复：① `KBDDiagnostic.__init__()` 新增 `case_id` 参数；② `_execute_acquirer()` 调用 `qfk_exec` 时透传 `case_id`；③ `investigation_agent.py` 构造 `KBDDiagnostic` 时传入 `case_id`。关联工单：Q2026072034962，关联 KBD：27123 |
| 2026-07-20 | v3.21 | **统一修复空 case_id 透传导致的回采/exec 失败并增强诊断日志（PR #583）**：① `conversation-service` 的 `/agent-exec` 将 `case_id` 由必填改为可选，缺失时经会话关联解析真实 `case_id`（复用 `get_conversation_service`），一处收敛同时覆盖 LLM 路径与 qfk 路径的空 case_id 路由缺口，并补 `agent_exec_case_id_resolved`/`agent_exec_case_id_unresolved` 日志；② `acli/executor.py` 推送命令前新增 `agent_exec_push` 结构化日志（含 `case_id`）；③ `qfk/engine.py` 的 `qfk_exec` 透传 `case_id`，终端失败日志新增 `triage` 字段区分「调用方未透传」与「已携带仍 session_missing（SSH 会话未建立/已断开）」；④ `terminal_bridge/main.go` 在 exec 回退与 `exec.session_missing` 分支补充 `sub_case_id`/`has_fallback_target` 字段，区分「从未连接」与「连了但路由不到」 |
| 2026-07-20 | v3.20 | **terminal_bridge 可观测性与日志回采重设计（OBS-TERMINAL-BRIDGE-001）**：① `terminal_bridge/main.go` 重写为结构化日志中枢 `LogHub`（有界环形缓冲 + 多订阅者回采 + 异常重传 + 本地落盘），标准库 `log` 经 `bridgeLogWriter` 零改造重定向为结构化日志；`InMessage`/`OutMessage` 透传 `trace_id`/`custom_ui`，按连接 `Origin` 自动归属 `custom_ui`；② 端到端 `trace_id` 透传：`acli/executor.py`（回退 exec_id）→ `agent_exec.py` SSE `traceId` → 前端 `chat.ts`/`terminal.ts`；③ 日志回采落库：前端 `stores/chat.ts` 消费 `bridge_log` 批量 POST `/api/bridge-logs`（失败重传），后端新增 `bridge_logs.py` 实现**真实 Session 鉴权**并批量写入 `bridge_execution_logs`；④ `qfk/engine.py` 新增**终端失败哨兵**——命令未在 HCI 主机执行（会话缺失/桥未运行/超时）直接判失败不进入关键字判定，从根上消除 `match_mode="not"`/`expected=False` 信号的假阳性（修复工单 Q2026071923606）；⑤ `database/desired_schema.sql` 新增 `bridge_execution_logs` 表 + 迁移 `005_bridge_execution_logs.sql`，`数据库设计.md` 同步 §2.9。关联事件文档：[2026-07-20-terminal-bridge可观测性与日志回采重设计](../../solution/events/2026-07-20-terminal-bridge可观测性与日志回采重设计.md) |
| 2026-07-20 | v3.19 | **修复前端「先报告后诊断」渲染顺序（工单 Q2026071923606）**：现象是 UI 先展示诊断报告（含 KBD 链接）、再展示诊断步骤卡片，与「先诊断匹配前端/后端信号、再提示 KBD 链接」的预期相反。根因经全链路排查确认在前端而非后端——`agent-service` 的 `diagnose()` 顺序正确（`kbd_diag_step`/`kbd_diag_confirm` 诊断步骤 → `text_chunk` 报告 → S4），`agent.py`/`conversation-service` 均按序透传无重排；`frontend/customer/src/stores/chat.ts` 在流式开始即 `push` 空「AI 报告气泡」，流式期间诊断步骤（`tool_call`/`interactive_request`/`agent_exec_command`）被 `push` 到其后，报告文本最后才写入最前的气泡，视觉上「报告在顶、诊断在底」。修复：流式 `finally` 收尾时将报告气泡 `splice`+`push` 移到消息列表末尾，最终顺序变为「诊断步骤 → 工具执行 → 最终报告（含 KBD 链接）」；对无工具卡片的普通对话为 no-op 无副作用。关联事件文档：[2026-07-20-前端诊断报告与步骤渲染顺序修复](../../solution/events/2026-07-20-前端诊断报告与步骤渲染顺序修复.md) |
| 2026-07-18 | v3.18 | **KBD 诊断引擎安全与顺序修复（PR #574）**：① 抽取层剥离 `root_cause`/`solution`，杜绝把处置动作（如 `acli vm start`/`kill -9`）误抽成诊断信号；② `acli/classifier.py` 覆盖 `acli system` 子命令包裹的写操作（rm -rf/mkfs/kill/systemctl 等），消除安全倒挂；③ `kbd_differential.py` 新增 `_signal_requires_human` 写门禁（贪心主循环 + 确认分支均拦截）并修复确认分支跳过前端生产者 `qkv_task` 的设计缺口（改为按 KBD 内容顺序遍历全部 `signals`）；④ `kbd_model.py` 新增 `get_signal` 供执行层做写门禁判定。关联事件文档：[2026-07-18-KBD诊断引擎安全与顺序修复分析](../../solution/events/2026-07-18-KBD诊断引擎安全与顺序修复分析.md) |
| 2026-07-18 | v3.17 | **修复 QFK 诊断“BridgeRelayExecutor 未启动”假阴（PR #572）**：`main.py` lifespan 此前从未调用 `set_executor()` 注入模块级全局 `_executor`，而 QFK 引擎（`qfk.engine.qfk_exec`）复用该全局作为唯一执行后端，导致 `_executor` 恒为 None，所有 `qfk_system`/`qfk_vm` 等关键信号判定在入口短路返回“未启动”误报，并错误归因为终端桥未启动（实际 terminal_bridge/SSH 链路正常，手动 SSH 可验证）。修复：lifespan 中将 `CompositeToolExecutor` 已构建的 `BridgeRelayExecutor` 注册为全局实例（与 InvestigationAgent 共用，避免重复建连）；并修正 QFK 误报文案，明确指出是 agent-service 内部未注册而非终端桥故障，避免现场误重启 terminal_bridge | — |
| 2026-07-17 | v3.15 | **根治诊断报告“未用关键信号确认就下结论”（待合并 PR）**：① `kbd_differential.py` 新增关键信号确认阶段——当贪心消除主循环因候选数 ≤ early_stop_threshold(2) 未执行任何步骤时（单/少候选场景，如“虚拟机-003 开机失败”常仅 1 条匹配 KBD），强制补跑剩余候选的 backend 关键信号（qfk_*/acli_* 等）作为现场证据，杜绝直接把 KBD 文档 root_cause 复述成结论；② 报告生成 Prompt 强化：诊断依据必须引用实际采集到的关键信号输出，无证据须标注“（未经现场信号确认，建议执行：<命令>）”；③ 更新单候选测试并新增回归测试 `test_single_candidate_confirms_its_signals` | — |
| 2026-07-18 | v3.16 | **硬编码 Prompt 统一数据库化接入 prompt 管理（待合并 PR）**：将 htp 诊断路径中 5 处硬编码 LLM Prompt 注册进 `system_prompt` 表（prompt 管理可可视化/热更新/回滚），按阶段与顺序归位：① S1 `s1_react_output_constraint_v1`（React 通用输出约束）、`s1_react_structured_output_v1`（结构化输出强制要求，占位符 `schema_json`）；② S3 `s3_kbd_judge_v1`（KBD 差异判定 LLM 匹配，占位符 `tool_name/truncated_output/kbd_expectations`）；③ S4 `s4_kbd_report_v1`（KBD 诊断报告生成，占位符 `steps_count/steps_summary/kbds_count/kbds_summary`）、`s4_react_antihallucination_v1`（反幻觉自我检查）。`KBDDiagnostic`/`ReactEngine` 新增 `_load_prompt` 助手经 `StrictPromptLoader` 从 DB 加载（`db_session_factory` 为空时回退 `create_mock_session_factory` 基准模板，保证单测一致）；`investigation_agent.py` 向 `KBDDiagnostic` 注入 `db_session_factory`。`database/seeds/02_system_prompts.sql` 与 `shared/utils/prompt_loader.py` 的 mock 工厂同步新增 5 条模板 | — |
| 2026-07-16 | v3.14 | **工具命名规范统一：acquirer 点号→下划线（PR #566）**：`kbd_differential.py` 路由由 `startswith("qkv.")`/`split(".")` 改为 `startswith("qkv_")`/`split("_", 1)`；QKV 3 + QFK 8 共 11 个 acquirer 由点号统一为下划线（如 `qkv.alert`→`qkv_alert`、`qfk.hardware`→`qfk_hardware`），与 `ACQUIRER_CATALOG`、种子、系统提示词模板及单测/集成测试保持一致 | — |
| 2026-07-15 | v3.13 | **sop_document.signals_json 声明式 schema 补齐（PR #556）**：修复 PR #545 漏改 `database/desired_schema.sql` 的 `sop_document` 表定义（仅改了 `kbd_entry`）。`db-migrate.sh` 只应用 `desired_schema.sql`（声明式 SSOT）、不应用 `atlas-migrations/` 版本化迁移，导致数据库 `sop_document` 表缺 `signals_json` 列，ORM `select(SopDocument)` 查询 500（编辑保存/审核通过/发布/导入/抽取信号），而 GET 详情用原生 SQL 显式列名不受影响。本次补齐列+COMMENT+GIN 索引；新增避坑指南 D-013 | - |
| 2026-07-14 | v3.12 | **关键信号 Prompt 种子补齐 + Pipeline Stage 修复（PR #549）**：① `database/seeds/02_system_prompts.sql` 新增 `kbd_extract_signals_v1` 种子（与 atlas 迁移内容一致），修复 Prompt 管理页看不到「关键信号分级抽取」的问题；② data-pipeline `EXTRACT_SIGNALS` Stage 补齐异步 job+轮询（复用 `signal_job_manager`）、DAG 依赖 `CLASSIFY`、CLI `extract/5` 暴露，并修复 resume 路径缺失的 `get_completed_ids_for_stage` 导入 | — |
| 2026-07-14 | v3.11 | **关键信号字段级抽取后续清理（PR #547）**：① agent-service `signal/variable_pool` 注册键统一 `.upper()`、`render_template` 仅匹配大写 `{{VAR}}` 占位符（ADR-2 强制校验，小写/单括号视为非法被丢弃）；② `signal/template.py` 占位符全改为 `{{HOST}}`；③ 数据库 `desired_schema.sql` 彻底移除 `steps_json` 列与 GIN 索引、仅保留 `signals_json`（ADR-1）；④ `ToolManageView.vue` 支持从信号面板跳转预填检索 | — |
| 2026-06-17 | v3.10 | **ReAct 工具调用历史跨轮次持久化（PR #472）**：① `message_role` ENUM 增加 `tool_call`/`tool_result` 角色；② `message` 表新增 `tool_call_id` 字段；③ agent-service `_persist_tool_turn()` 每次工具执行后持久化；④ conversation-service `/tool-turn` 接口接收写入；⑤ `history_messages` 重建逻辑重写，还原 OpenAI messages 格式 + 滑动窗口压缩（最近 10 步完整保留） | [../../solution/agent/events/2026-06-17-ReAct工具调用历史跨轮次持久化.md](../../solution/agent/events/2026-06-17-ReAct工具调用历史跨轮次持久化.md) |
| 2026-06-08 | v1.0 | 初版：四阶段任务分解，含验收标准 |
| 2026-06-08 | v1.1 | 阶段零已全部合并；完成阶段一「工具事务化地基」开发并通过全量单元测试 |
| 2026-06-08 | v1.2 | 全面核查：阶段零~二全部完成；阶段三 T3-1~T3-2、T3-4~T3-5 已完成，T3-3 未实现；阶段四 T4-1~T4-3 已完成，T4-4 未实现；整体 20/22 |
| 2026-06-08 | v1.3 | 第一性原理深度审查：T1-2 实际未完成（前端 `chat.ts` 提交 interactive-response 时未回传 `exec_id`，`confirm:{exec_id}` 等待形同虚设），子项与阶段一总验收相应回退；其余任务 happy path 已落地，但仍存在审查记录在案的边缘缺陷（详见附录） |
| 2026-06-08 | v1.4 | **PR-1 工具事务执行链修复**：T0-3 修复 Composite 执行器降级 string 导致 exit_code_meaning 丢失；T1-1 ConfirmService 同步落 `authorization` 表（exec_id/actor/decision/tool_input_hash/expires_at）；T1-2 前端 `chat.ts handleConfirmResult` 改为命中 `/interactive-response` 并回传 exec_id+input_hash；T1-3 ConfirmService 缺失时 risk≥2 工具按 fail-closed 拒绝执行（旧实现 fail-open）；T1-4 `tool_result` 新增 `retry_count` 字段并由 ReactEngine 落库审计；附 6 个新增单测全部通过 |
| 2026-06-09 | v1.4 | 完成 T1-2/T3-3/T4-4 整改：补齐 `exec_id/input_hash/expires_at` 端到端透传、确认路由 fail-closed 校验与授权审计、禁止前端静默确认高风险 pending 工具、实现可展示推理摘要折叠展示与 CI 回归评测门禁 |
| 2026-06-09 | v1.5 | **PR-2 工具反馈完整性修复**：T0-1 未知工具/risk=3 block 路径统一走 ToolResultEnvelope；T0-2 kbd_differential 固定截断替换为 smart_truncate；T0-5 ToolCallValidator 扩展 enum/array/oneOf；T2-1 VM 阈值从 180s 改为 30s；T3-1 Composite 执行器 sop 路由标注 |
| 2026-06-09 | v1.6 | **PR-3 Prometheus 指标完善与 Grafana Dashboard**：T4-2 补充完整指标（agent_tool_timeout_total、agent_resolution_steps 等）；ReactEngine 接入超时和步数指标；新增 Grafana Agent 可靠性看板（agent-reliability.json） |
| 2026-06-09 | v1.7 | **P1 可靠性闭环关键缺口修复**：T0-1 动态 block ToolResultEnvelope；T1-1 Authorization 去重（confirm_service 停止写 authorization，conversation-service 为唯一写入方）；T4-2 补齐指标写入点（tool_execution_duration、reasoning_confidence、unsupported_claim、information_confidence） |
| 2026-06-09 | v1.8 | **P2 可靠性闭环补充改造**：T3-1 CompositeToolExecutor 真正执行 sop 工具；T2-2 工具结果写入 FactStore；T4-1 FaithfulFakeLLM 替换 random classification |
| 2026-06-09 | v1.9 | **P2 遗漏项补齐**：T2-3 FactStore PG-first；T3-5 验证闭环确认已实现；S0 env_context fallback 清理 |
| 2026-06-09 | v2.0 | **可靠性闭环加固优化**：全局 ReactEngine 注入 FactStore，覆盖 S5 修复工具事实写入；FactStore 收紧 PG 权威语义，PG 可用但未命中不再 fallback Redis，PG 写失败不再 Redis-only 成功；Replay Runner 改为离线 dispatcher 产出分类/工具路径，Fake LLM 不再直接读取 golden answer；CI 回归基线改为读取 PR base 分支 report |
| 2026-06-10 | v2.1 | **诊断阶段环境数据丢失修复**：① evidence_builder.py 增加 FactStore 历史事实检查，env_context 为空时不再直接判定缺失，而是先查询已存储事实；② conversation_service.py 环境上下文获取从仅 S0 扩展到 S0-S4 全阶段；③ desired_schema.sql 新增 `fact` 表（T4-3 事实持久化） |
| 2026-06-10 | v2.2 | **S0 意图识别解析逻辑优化**：triage_agent.py `_parse_intent_result` 改用宽松正则 + 动态字典交叉校验模式，支持管理员调优 Prompt 改变输出格式时仍能正确识别分类；新增 test_custom_prompt_output 单测验证解析稳健性 |
| 2026-06-10 | v2.5 | **命令执行 case_id 传递与超时优化**：① `BridgeRelayExecutor.execute` 和 `ReactEngine._execute_tool_call` 扩展方法签名，显式从上下文传递真正的 `case_id`，解决 terminal_bridge 会话池找不到会话的问题；② 后端 blpop 等待超时从 32s 缩短为 10s（HTTP client 12.0s）；③ 前端 `chat.ts waitForExecResult` 超时从 35s 缩短为 10s |
| 2026-06-10 | v2.4 | **澄清请求交互事件空流误判修复**：① `agent.py` `_event_stream` 引入 `_has_interactive_request` 和 `_has_escalation` 状态位；② 存在合法交互/升级事件时跳过空流错误判断；③ 修复澄清请求（`AgentInteractiveRequest`）和升级事件（`AgentEscalation`）被误判为推理错误并输出 `[Agent Error: AI 推理未返回任何内容]` 的问题 |
| 2026-06-10 | v2.3 | **终端 bridge 超时与诊断阶段显示修复**：① terminal_bridge execID 解析剔除连字符；② chat.ts 切换工单时还原诊断阶段；③ evidence_builder.py 信息质量检查 if 分支合并（SIM114） |
| 2026-06-11 | v2.6 | **前置查询工具类别迁移**：① `get_active_alerts`、`get_failed_tasks`、`get_vm_list`、`get_cluster_detail` 从 `scp` 类别迁移至 `acli` 类别，移除对 SCP REST API 的直接依赖；② `database/seeds/01_tool_definitions.sql` 中新增 `usage_template` 绑定 acli 命令；③ `executor.py` 中实现 `get_failed_tasks` 动态参数拼装，支持 keyword/code/vm_id/time/host/upid/limit 等过滤参数 |
| 2026-06-11 | v2.7 | **IP 参数校验放宽与工具结果截断修复（PR #442）**：① `react_engine.py` 参数前置校验放宽 `node_ip`/含 `ip` 参数，在未显式声明 `format: ipv4` 时同时兼容主机名/节点名（如 `SVR_aCloud_670`），消除主机名输入直接报错；② `react_engine.py` 在参数校验失败时补齐 `tool_result` failed 事件广播，终止前端控制台「正在等待输出...」悬挂；③ `terminal_bridge/main.go` 使用正则定位带 exitCode 的真实 Marker 并动态剥离命令回显，修复 `ssh.ECHO` 导致 `__EXEC_DONE_` 在 TTY 回显中被提前截断的 Bug，并注入完整的可观测调试日志；④ `executor.py` BLPOP_TIMEOUT 调整为 30s，对应 `frontend/customer/src/stores/chat.ts` `waitForExecResult` 超时同步为 30s |
| 2026-06-11 | v2.8 | **SSH终端代理双通道隔离执行与渲染优化（PR #443）**：① `terminal_bridge/main.go` 新增 `ssh_exec_process` 命令分支，使用独立 SSH Session 执行禁用 PTY 申请，通过物理分流 stdout/stderr 实时推送 `exec_stdout`/`exec_stderr` 帧；② `frontend/customer/src/api/terminal.ts` 支持 `buildAgentExecProcessMessage` 消息发送与物理流解析；③ `frontend/customer/src/stores/chat.ts` 设立流式缓冲区 `execBuffers`，在 `postExecResult` 时传入物理隔离的标准流；④ `frontend/customer/src/components/MessageBubble.vue` 物理隔离渲染 stdout 与 stderr 纯文本区域，杜绝 JSON 二次转义；⑤ `backend/conversation-service/app/routes/agent_exec.py` 扩展 `/exec-result` 支持 stdout/stderr 分离回传；⑥ `backend/agent-service/app/tools/acli/executor.py` 重构输出解析提取器，优先读取双通道物理隔离输出且向下兼容 |
| 2026-06-12 | v2.9 | **工具执行契约前置校验（PR-A）**：① `bash_exec` 增加必填 `container` 契约并在 ReAct 真实执行前执行 `ToolSemanticValidator`；② `executor.py` 修复双通道 `stderr` 被 `output` 未定义异常覆盖的问题；③ `acli_exec` 基于本地 aCLI catalog 快照拦截不支持的命令路径；④ 新增 `agent_tool_semantic_validation_total` 指标和结构化日志字段，校验失败不下发 terminal_bridge，而是反馈给 LLM 重新规划 |
| 2026-06-12 | v3.0 | **工具管理 UI 校验闭环（PR-B）**：① 工具管理编辑弹窗新增“校验工具定义”按钮，直接展示后端 `validation_issues`；② 保存前自动调用同一校验接口，`error` 阻断保存，`warning` 可保存但页面保留提示；③ 本地 JSON/数组解析错误也归一为同一套校验结果面板，避免只弹 toast 后丢失定位信息 |
| 2026-06-12 | v3.1 | **SOP 发布联动工具契约校验（PR-C）**：① `kb-service` 发布 SOP 时静态校验 `acli_methods` 与命令型前置检查；② 不支持 aCLI catalog 的命令、bash 命令缺容器边界统一写入 `ValidationIssue` warning；③ 同步脚本同时更新 agent-service/kb-service catalog；④ CI unit-tests 纳入 `backend/kb-service/tests/` |
| 2026-06-12 | v3.2 | **容器执行适配器（PR-D）**：① `bash_exec` 从固定 `docker exec` 升级为远端 wrapper 自动探测 docker/crictl/ctr；② 执行事件与 FactStore 保留 `container/original_command/built_command`；③ 不支持运行时 fail-closed，返回明确 stderr 与 `exit 127` |
| 2026-06-12 | v3.3 | **SOP Markdown 命令归一化与变量来源门禁（PR #450）**：① `bash_exec.container` 支持 `host`，host 表示物理机直接执行；② SOP Markdown 中 `acli`、`container_exec`、`host_exec` 与裸 bash 自动归一化为结构化 `tool_calls`；③ `get_sop_node` 外显节点/分支 `required_variables`；④ `sop_advance` 阻断缺失的 `user_input/user_confirm/env_*` 变量并提示先调用 `sop_request_variable` |
| 2026-06-12 | v3.4 | **SOP 变量来源运行时门禁补强**：① `SopToolExecutor` 对 SOP 模式下的真实诊断工具增加 before-tool-call 门禁；② 当前节点或候选子节点依赖 `user_input/user_confirm/env_*` 变量且未就绪时，不下发 `bash_exec/acli_exec`，返回 `sop_variable_gate_blocked`；③ 结果包含 `next_tool_call=sop_request_variable`，引导 LLM 先按变量来源获取值 |
| 2026-06-12 | v3.5 | **集成 Langfuse 可观测性**：① 引入 `langfuse.py` 实现大模型及工具执行调用全链路追踪；② 将 Langfuse 容器发布至 K3s；③ admin-ui 嵌入 Langfuse 分析面板 |
| 2026-06-13 | v3.6 | **告警与任务解析技能分类调整（PR #454）**：① `hci-alert-parsing` 和 `hci-task-parsing` 的 metadata.category 从 `monitoring` 调整为 `platform`（更准确的语义定位）；② 同步更新 `skills/告警解析.md` 和 `skills/任务解析.md` 源文件分类；③ `database/seeds/03_skill_definitions.sql` 同步更新种子数据 |
| 2026-06-13 | v3.7 | **平台内置/硬编码治理（PR #462）**：① `sop_request_variable` 改为通过 `DynamicSkillRunner` 按次读取 `skill_definition` active 记录，移除 Python 内置业务技能执行链；② 工具注册表改为短 TTL 热刷新，ReAct 工具列表、风险判定、执行前校验和 Composite 执行统一消费运行时 registry；③ `bash_exec.container` 从 `tool_definition.parameters_schema.properties.container.enum` 读取，`get_failed_tasks` 改为声明式 `usage_template`；④ conversation-service 环境变量注入收敛为显式 `env_info` / `env:<field>`，不再从告警日志硬编码猜测 `node_ip/disk_sn/request_id`；⑤ 幻觉检测仅使用当前工具 registry 快照，registry 缺失时跳过 phantom tool 检测并记录 warning |
| 2026-06-13 | v3.8 | **五大动态资源统一运行时（PR2）**：① 新增 `backend/shared/dynamic_resource/` 公共模块，统一 revision 发布、active 加载、TTL cache、基础校验、usage audit 和业务表适配；② 新增 `dynamic_resource_revision`、`dynamic_resource_active`、`dynamic_resource_usage_audit`、`prompt_slot`，并为 `sop_execution` 增加 `sop_revision`；③ KBD/SOP/Tool/Skill/Prompt 主发布或主运行路径接入 `resource_revision`，Agent/KBD/SOP/Prompt/Skill/Tool 使用写入统一审计；④ Skill 保存/启用/执行前校验 `allowed_tools`，Prompt loader 支持 slot 解析与占位符契约；⑤ 主动 pub/sub 刷新、完整 VariableProvider 拓扑、HTTP ToolRunner 和跨资源循环依赖全图校验留给后续增强 |
| 2026-06-13 | v3.9 | **SOP 变量运行时契约优化（PR3）**：① 变量池 `engine.py` 新增 `acquisition_args_template` 渲染，支持 dict/list/string 递归、`{var}` 与 `{object.field}` 占位符，缺变量时 fail-loud 报错；② 新增 `derived` 策略，使用白名单表达式支持布尔、字符串、数字、变量引用、函数和三元表达式（不使用 `eval`）；③ 工具/Skill 输出提取支持对象属性路径（如 `result.stdout`），通用命令执行结果默认绑定 `stdout`；④ 保留 `false/0` 等合法 falsy 值，不强制转换为字符串；⑤ 新增单测覆盖参数模板、派生变量、stdout/falsy 输出绑定 |
| 2026-06-14 | v4.0 | **SOP长命令截断治理与LLM纠错架构（PR #466）**：① `executor.py` 大输出截断前无损暂存 Redis `cmd_cache:{exec_id}`（1800秒），`ExecResult` 追加 `exec_id`；② 退出码 127/command not found 含 python 时自动纠错重写；③ `01_tool_definitions.sql` 为 `acli_exec/bash_exec` 添加禁用 Python 与截断过滤建议提示；④ 变量池新增 `json_extract` 策略，支持 `jsonpath-ng.ext`（含 `&` AND 过滤）从缓存/截断数据提取子变量；⑤ 新增 `acquisition_strategy.py` 公共解析器，统一"实体_动作"及冒号简写规则；⑥ `sop_execution.py` 创建执行实例时自动注入原始事实源（含 `alert_logs`）到变量池。详见 [SOP长命令截断治理与LLM纠错架构设计.md](../../solution/events/2026-06-14-SOP长命令截断治理与LLM纠错架构设计.md) |
| 2026-06-15 | v4.1 | **SOP 技能工具绑定修正与变量门禁范围优化（PR #470）**：① `database/seeds/03_skill_definitions.sql` 中 `hci-alert-parsing`/`hci-task-parsing` 的 `allowed_tools` 由 `'bash'` 修正为 `'bash_exec'`；② `nav.py` 中 `find_missing_guarded_variables_for_node_window` 优化为非叶子节点时仅检测当前节点本身受控变量，不再提前拦截子分支变量；③ kb-service 新增 `validate_variable_schema_dependencies` 校验器，在 SOP 发布及变量 schema 更新时检查依赖的工具/技能是否注册且启用，缺失时抛出 422 并返回 `ValidationIssue` 列表。详见 [SOP发布与变量更新依赖校验设计.md](../../solution/knowledge-base/sop-agent/SOP发布与变量更新依赖校验设计.md) |
| 2026-06-15 | v4.2 | **诊断自动流转与推理步数限制优化（PR #471）**：① `conversation_service.py` 在 S0 拦截后将 Assistant 分类确认文案追加到 `history_messages`，使大模型知晓阶段已确认，避免重复寒暄输出触发 `invoke_result.content is not None` 终止规则导致流程停顿；② `react_engine.py` 常数 `MAX_STEPS` 与 `investigation_agent.py` 参数 `max_iterations` 从 15 步上调至 40 步，给予复杂 SOP 或断线重连后重复命令运行的充足步骤预算 |
| 2026-06-21 | v4.3 | **Skill 调用失效修复（PR #475）**：① `get_sop_node`/`sop_advance` 返回体新增 `preferred_next_steps` 字段，当节点有未就绪 `skill_call/tool_call` 变量时嵌入显式推荐行动（Contextual Nudge）；② 变量门禁分层设计，新增「软推荐」层覆盖 `skill_call/tool_call` 类型；③ S0/S1 系统提示词种子数据新增「变量采集规范」，强制要求优先调用 `sop_request_variable`。详见 [skill调用失效根因分析与改进方案.md](../../solution/agent/skill调用失效根因分析与改进方案.md) |
| 2026-06-21 | v4.4 | **ConfirmService 初始化与 REACT_ENABLED 解耦（PR #476）**：① `main.py` 中 ConfirmService 初始化不再依赖 REACT_ENABLED 开关，只要 Redis 可用即启用；② 修复 InvestigationAgent SOP 轨道内嵌 ReactEngine 因 confirm_service=None 导致所有 risk≥2 工具被 fail-closed 拒绝执行的问题；③ 增加详细的可观测性日志（confirm_service_initialized/confirm_service_skipped）。详见 [skill调用失效改进后恶化根因与闭环方案.md](../../solution/agent/skill调用失效改进后恶化根因与闭环方案.md) |
| 2026-07-09 | v4.5 | **QKV/QFK 双核信号架构重构（PR #498）**：① 建立 KeySignal 抽象基类，统一 FrontendSignal（前端信号/生产者）与 BackendSignal（后端信号/消费者）的架构体系；② 实现 SignalExtractor 从 KBD/SOP 自然语言文本提取结构化信号；③ 实现 VariablePool 变量池管理生产者-消费者模式，前端信号提取 host/vm/time 等变量，后端信号通过 ${variable} 占位符消费；④ 自动类型判别机制 KeySignal.from_dict() 根据信号类别路由到派生类；⑤ 彻底废弃历史命名 QKVSignal/KeySignal（后端专用），统一为语义清晰的新架构。详见 [关键信号基类设计.md](../../solution/agent/02-架构设计/关键信号基类设计.md) 和 [关键信号架构迁移指南.md](../../solution/agent/02-架构设计/关键信号架构迁移指南.md) |

---

## 整体进度

```
阶段零（止血）     ██████████  已完成
阶段一（工具事务） ██████████  已完成
阶段二（事实体系） ██████████  已完成
阶段三（推理约束） ██████████  已完成
阶段四（评测闭环） ██████████  已完成
```

## 第一性原理审查结论

整体任务安排合理：先修工具结果语义、截断、Schema 与确认链路，再建设事实体系、结构化推理和评测闭环，符合“先稳定动作，再约束事实，再校验结论”的生产级 Agent 演进顺序。

本次审查确认并优化以下边界：

1. **高风险确认必须以服务端为权威**：前端自动执行模式只能作为偏好，不得对服务端已经判定为 `pending` 的高风险工具静默确认。
2. **确认事务必须绑定 `exec_id + input_hash`**：conversation-service 在解锁 agent-service Redis 确认队列前，必须先校验 `tool_result` 记录，hash 不匹配、记录缺失或审计失败均 fail-closed。
3. **授权必须可审计**：用户确认/拒绝都写入 `authorization`，并回填 `tool_result.authorization_id`，形成可追溯链路。
4. **不鼓励暴露完整隐藏思维链**：T3-3 保留 `<reasoning>` 折叠展示，但内容定义为“可展示推理摘要”，只呈现证据、假设、置信度和下一步动作。
5. **CI 门禁必须做真实回归对比**：仅固定阈值不足以发现退化，T4-4 从 PR base SHA 读取 `evaluation/report.json` 作为基线，避免当前分支自比。
6. **Agent 单测必须纳入根级门禁**：根级 pytest 与 CI 单测列表必须包含 `backend/agent-service/tests`，否则可靠性测试会在主门禁外漂移。
7. **事实权威源必须单向收敛**：PG 可用时 Redis 只能作为 read-through 热缓存，不能在 PG 未命中或写失败时反向成为权威。
8. **离线评测不得复读 golden answer**：Fake LLM 只替代 LLM 文本生成，分类与工具路径必须由被评测的 dispatcher/离线适配层产出。

---

## 阶段零：止血（预估 1 周，立即可开始）

> **目标**：修复现有代码中已确认的高危缺陷，零架构风险，全部在单文件内完成，1 周内可合并上线。
>
> **依据**：方案 B Phase 1 P0 项。

### T0-1 `ToolResultEnvelope` 替换 `str(tool_result)` 【P0】

- **文件**：`backend/agent-service/app/adapters/agents/htp/react_engine.py`
- **问题**：`work_messages.append({"content": str(tool_result)})` 将工具返回字典强制转字符串，破坏结构信息，LLM 在下一轮推理时需"猜测"结果语义
- **任务**：
  - [x] 新增 `ToolResultEnvelope` 数据类（`tool_name`、`exec_id`、`success`、`exit_code`、`stdout`、`stderr`、`exit_code_meaning`、`truncated`、`interpretation`、`suggested_next_action`）
  - [x] 实现 `to_llm_message()` 序列化方法，输出带 emoji 标注、结构清晰的 LLM 友好消息
  - [x] 替换 `react_engine.py` 中所有 `str(tool_result)` 调用点
- **验收**：工具调用成功/超时/失败三种场景下，LLM 收到的消息格式正确，无歧义

---

### T0-2 智能截断替换固定截断 【P0】

- **文件**：`backend/agent-service/app/adapters/executors/executor.py`、`backend/agent-service/app/adapters/agents/htp/kbd_differential.py`
- **问题**：`STDOUT_MAX_CHARS = 4000` 固定截断，可能截掉末尾最关键的报错信息
- **任务**：
  - [x] 实现 `smart_truncate(output, max_chars)` 函数：
    - 优先保留含 `error/fail/exception/critical/fatal/panic` 的行
    - 保留首尾各 20% 作为上下文
    - 中间部分压缩并注明截断说明
  - [x] 替换 `executor.py` 的 `STDOUT_MAX_CHARS` 截断逻辑
  - [x] 替换 `kbd_differential.py` 的 `truncated_output = actual_output[:2000]` 截断逻辑
- **验收**：超长输出场景下，错误行（含 error 关键字）被优先保留在截断结果中

---

### T0-3 exit_code 语义分类 【P0】

- **文件**：`backend/agent-service/app/adapters/executors/executor.py`
- **问题**：超时和真实失败都返回 `exit_code=-1`，LLM 无法区分，可能做出错误决策
- **任务**：
  - [x] 定义 `ExitCodeMeaning` 枚举：`success / timeout / permission_denied / command_not_found / connection_refused / unknown_error`
  - [x] 修改超时返回路径：`exit_code=-1` 同时附加 `exit_code_meaning="timeout"` 字段
  - [x] 修改 `ToolResultEnvelope.interpretation` 自动生成逻辑：超时时给出"命令超时，可能节点负载过高或 terminal_bridge 未连接"的提示
- **验收**：超时场景下 LLM 收到的错误描述明确包含"timeout"语义，不与命令本身失败混淆

---

### T0-4 证据锚定 Prompt 规则注入 【P1】

- **文件**：`backend/agent-service/app/adapters/agents/htp/investigation_agent.py`（或对应 Prompt DB 记录）
- **问题**：LLM 当前无约束，可在没有工具证据的情况下直接给出根因结论
- **任务**：
  - [x] 在所有 Agent 的 system prompt 中追加「证据锚定规则」（5 条强制规则，含正确/错误示例）：
    - 禁止凭空声明，每个结论必须明确引用工具输出
    - 不确定时必须声明不确定性
    - 禁止跳步推理
    - 区分观察与结论
    - 生成结论前幻觉自查
  - [x] 在 `fallback_mode` 中追加「降级模式警告」：所有建议标注"需要执行验证"，禁止给出"已确认根因"
- **验收**：新 Prompt 规则在 5 个典型幻觉测试用例下，无证据声明率下降 ≥ 50%

---

### T0-5 `ToolCallValidator` 参数前置校验 【P1】

- **文件**：`backend/agent-service/app/adapters/agents/htp/react_engine.py`
- **问题**：LLM 生成的工具参数在执行前无格式/语义校验，低质量调用直接到达执行器
- **任务**：
  - [x] 实现 `ToolCallValidator`：JSON Schema 校验 + IP 格式正则 + 必填项检查
  - [x] 在 `react_engine._execute_tool_call()` 前插入校验步骤
  - [x] 校验失败时向 LLM 返回结构化错误消息（含错误路径和修正提示），不抛出系统异常
- **验收**：传入非法 IP 格式、缺少必填参数时，LLM 收到错误提示并在下一轮自动修正，不导致执行器抛错

---

**阶段零验收标准**

- [x] 所有 P0 任务合并上线
- [x] 工具结果语义丢失问题消除（Code Review 验证）
- [x] 关键错误信息截断问题消除（日志验证）
- [x] exit_code 超时与失败可区分（手动测试验证）

---

## 阶段一：工具事务化地基（预估第 2-4 周）

> **目标**：让工具调用从"一次临时动作"升级为"平台管理的可恢复事务"。
>
> **依据**：方案 C Tool Plane 最小子集 + 方案 C §阶段一验收标准（因服务间 API 变更规范 (G-4) 和 8.2 破坏性变更禁令限制，放弃引入全新的 `tool_execution` 表，直接对 `tool_result` 表进行增量字段升级与状态扩展）。

### T1-1 增量升级 `tool_result` 数据模型 【P1】

- **文件**：`backend/shared/models/audit.py`、`database/` 迁移脚本
- **任务**：
  - [x] 增量更新 `ToolResult` SQLAlchemy 模型，在现有 `tool_result` 表中追加列：
    ```
    status (String(30), 默认 'committed' 以向下兼容历史记录)
    input_hash (String(64), nullable=True)
    authorization_id (String(36), nullable=True)
    idempotency_key (String(100), nullable=True)
    case_id (String(20), nullable=True)
    updated_at (DateTime(timezone=True), 自动更新)
    ```
  - [x] 新增 `Authorization` 授权模型并创建 `authorization` 表：`auth_id`、`exec_id`、`actor`、`decision`、`tool_input_hash`、`expires_at`
  - [x] 编写数据库增量迁移脚本 `20260608000000_optimize_tool_result_for_transaction.sql` 并执行
  - [x] 更新 `backend/shared/models/__init__.py` 和 `conversation-service` 导出新模型
- **验收**：Atlas 迁移脚本可在本地环境无报错执行（UP / DOWN 路径均成功）

---

### T1-2 工具确认请求携带 `exec_id` 【P1】

- **文件**：`backend/agent-service/app/adapters/agents/htp/react_engine.py`、`backend/conversation-service/`
- **问题**：现有 `tool_confirm` 请求仅携带 session_id，无法唯一标识一次工具执行
- **任务**：
  - [x] `AgentInteractiveRequest`（`tool_confirm` 类型）增加 `exec_id`、`input_hash`、`expires_at` 字段
  - [x] `ConfirmService.wait_for_confirm()` 改为按 `exec_id` 级别隔离 Redis key（`confirm:{exec_id}`）
  - [x] 前端 `tool_confirm` 响应携带 `exec_id` 回传
  - [x] conversation-service `submit_interactive_response` 路由验证 `exec_id` + `input_hash` 一致性
  - [x] conversation-service 写入 `authorization` 记录并回填 `tool_result.authorization_id`
- **验收**：同一 session 并发两个工具确认请求，两者互不干扰，各自正确解除阻塞

---

### T1-3 自动执行从前端开关升级为服务端策略 【P1】

- **文件**：`backend/agent-service/app/services/` 或新增 `policy_service.py`
- **问题**：前端 aggressive 模式可代替用户点击确认高风险工具，存在安全漏洞
- **任务**：
  - [x] 新增 `PolicyService.evaluate(tool_name, risk_level, user_id, session_id)` 方法
  - [x] 自动执行仅允许满足**所有**条件的工具：
    - `risk_level <= 1`
    - `side_effect = none`
    - input schema 校验通过
    - 服务端策略允许当前用户/工单
    - 未触发熔断（`CircuitBreaker` 未开启）
  - [x] `risk_level = 2` 的工具无论前端模式如何，必须进入服务端策略评估并记录授权
  - [x] 前端自动执行模式（Off/Safe-only/Aggressive）转为"用户偏好提示"，实际执行由后端决定
- **验收**：前端设为 Aggressive，发送 `risk_level=2` 的工具确认，后端仍要求人工授权并记录 `authorization` 记录

---

### T1-4 工具执行结果落库与状态机 【P2】

- **文件**：`backend/agent-service/app/adapters/agents/htp/react_engine.py`、`backend/agent-service/app/services/tool_audit.py`
- **任务**：
  - [x] 改造 `react_engine` 和 `ToolAuditService`，工具执行的各关键阶段对 `tool_result` 记录执行状态更新（`proposed` -> `executing` -> `committed` / `failed` / `cancelled`）
  - [x] 执行失败时记录 `error` 字段（含 exit_code 和 stderr 摘要）
  - [x] 实现 `ToolRetryPolicy`：区分可重试错误（超时）和不可重试错误（命令语法错误/权限拒绝），最大重试 2 次，指数退避
  - [x] 实现 `ToolCircuitBreaker`：单节点 3 次连续失败后熔断 60 秒，半开后自动探测恢复
- **验收**：
  - [x] 刷新页面后，pending/running 状态的工具执行可从 `tool_result` 表恢复显示
  - [x] 每次工具执行可通过 `trace_id + exec_id` 查询全链路记录
  - [x] 节点连续失败 3 次后，后续调用该节点的请求直接返回熔断错误，不再等待 30 秒超时

---

### T1-5 修复 Git 提交工具标识为 `gemini` 并升级项目规范 【P1】

- **文件**：`~/.my_custom_configs`、`AGENTS.md` (或 `CLAUDE.md`)
- **任务**：
  - [x] 优化配置文件 `~/.my_custom_configs` 中 `gcm` 与 `gpr` 的 `AGENT` 变量提取。若未指定且存在环境变量 `ANTIGRAVITY_AGENT=1`，则默认标识设定为 `gemini`，避免在 Antigravity-IDE 环境中默认采用 `claude`
  - [x] 在 `~/.my_custom_configs` 中定义快捷别名 `gcm-g` 和 `gpr-g` 显式以 `AGENT=gemini` 运行提交
  - [x] 升级规范文件 `AGENTS.md` 的前言和 `Git Commit/PR 标识规则` 章节，添加对 `gemini` 标识的说明和使用规范
- **验收**：在 Antigravity 终端中执行 `gcm` / `gpr` 提交和 PR 自动适配为 `[agent:gemini]` 后缀

---

**阶段一验收标准**

- [x] 刷新页面后 pending 工具状态可从 `tool_result` 恢复
- [x] 同一 session 并发两个确认不会串线
- [x] `risk_level >= 2` 的命令不可被自动执行
- [x] 每次工具执行可通过 `trace_id + exec_id` 查全链路
- [x] `gcm`/`gpr` 脚本提交自动应用 `gemini` 身份标识，且项目规范完成升级说明


---

## 阶段二：轻量事实体系（预估第 5-8 周）

> **目标**：解决信息不准确和 Prompt 噪声问题。从"文本上下文驱动"转为"事实证据驱动"的第一步。
>
> **依据**：方案 C Evidence Plane 最小子集 + 方案 B 的 `InformationPacket`/`StaleDataGuard` 作为轻量替代。

### T2-1 定义 `InformationPacket` 数据结构 【P1】

- **文件**：`backend/agent-service/app/models/` 或 `backend/shared/models/`（新增 `information.py`）
- **任务**：
  - [x] 定义 `InformationPacket` 数据类：
    ```python
    value: Any
    source: str   # user_input / tool_exec / kb_search / llm_inference / env_inject
    freshness_ts: float
    confidence: float   # 0.0-1.0
    raw_evidence: str | None
    verified: bool
    ```
  - [x] 定义 `StaleDataGuard`：各类数据的过期阈值（进程状态 60s、VM 状态 30s 等）
  - [x] 定义 `EvidenceBundle`：按目的（`intent_classification / hypothesis_verification / remediation`）检索事实的集合
- **验收**：数据结构定义完整，单元测试覆盖 `StaleDataGuard.is_stale()` 各阈值边界

---

### T2-2 环境采集结果写入轻量 Fact Store（Redis） 【P1】

- **文件**：`backend/agent-service/`（环境数据采集流程）
- **任务**：
  - [x] 环境采集完成后，将结果封装为 `InformationPacket` 存入 Redis（key：`fact:{session_id}:{fact_type}`，TTL 按 `StaleDataGuard` 阈值设定）
  - [x] 存储时同时保留 `raw_ref`（原始数据）和 `normalized_value`（标准化后的数据）
  - [x] 多来源冲突时，不覆盖旧值，而是追加 `conflict` 标记，并在 EvidenceBundle 中显式标注
- **验收**：
  - [x] 每个自动注入的变量可以追踪到事实来源（`source` + `freshness_ts`）
  - [x] 同一字段多来源冲突时，两个值均保留并带 `conflict=true` 标记，不静默覆盖

---

### T2-3 S0 分类 Prompt 改为消费 EvidenceBundle 【P1】

- **文件**：`backend/agent-service/app/adapters/agents/htp/investigation_agent.py`（S0 阶段）
- **问题**：S0 Prompt 直接注入大段原始 `env_context` 字典，噪声多，LLM 注意力分散
- **任务**：
  - [x] 实现 `EvidenceBuilder.build_for_intent_classification(session_id)` 方法，从 Fact Store 检索 S0 分类需要的核心事实（工单描述、环境基本信息、历史故障标签）
  - [x] 替换 S0 Prompt 的上下文注入：从原始字典拼接改为 EvidenceBundle 结构化注入
  - [x] Prompt 中对过期事实（`freshness=stale`）和冲突事实（`conflict=true`）显式标注
- **验收**：
  - [x] S0 Prompt 长度减少 ≥ 30%（对比修改前）
  - [x] S0 Prompt 中包含事实来源标注（用例：环境数据显示 `[采集于 2 分钟前，来源: 工具执行]`）

---

### T2-4 `InformationPacket` 置信度检查与用户澄清请求 【P2】

- **文件**：`backend/agent-service/app/adapters/agents/htp/investigation_agent.py`（诊断开始前）
- **任务**：
  - [x] 实现 `_check_information_quality(session_id)` 方法：
    - 检查 env_context 是否为空或不完整
    - 检查工单创建时间是否超过 24 小时（可能过期）
    - 检查关键事实置信度是否低于阈值（`CONFIDENCE_THRESHOLD = 0.75`）
  - [x] 低置信度时生成 `AgentInteractiveRequest`（`kind=information_clarification`），向用户寻求确认，不基于低质量信息继续推理
- **验收**：env_context 为空时，Agent 发起信息确认请求而非直接开始推理

---

**阶段二验收标准**

- [x] 每个自动注入变量可以追踪到事实来源
- [x] 同一字段多来源冲突不会被静默覆盖
- [x] S0 Prompt 长度受控且包含关键事实来源标注
- [x] 环境数据缺失时触发用户澄清而非直接推理

---

## 阶段三：结构化推理与反幻觉（预估第 9-12 周）

> **目标**：让模型输出可校验，从"模型直接回答"变成"模型提出可验证假设"。
>
> **依据**：方案 C Reasoning Plane + 方案 A 滑动窗口 + 方案 B HallucinationDetector + CoT 外显。

### T3-1 SOP 决策树滑动窗口上下文裁剪 【P2】

- **文件**：`backend/agent-service/app/adapters/agents/htp/`（SOP 相关模块）
- **问题**：全量注入大型 SOP Markdown 造成"Attention Lost in the Middle"效应，Token 消耗巨大
- **任务**：
  - [x] 实现 SOP 节点局部注入：Agent 处于决策树节点 Nk 时，仅将当前节点诊断指南 + 子节点分支前置条件注入 Prompt（目标窗口 ≤ 500 token）
  - [x] 实现 `get_sop_node(node_id)` 工具：返回指定节点的核心诊断指南
  - [x] 实现 `sop_advance(node_id, direction, reasoning)` 工具：推进决策树节点并记录状态转移证据
  - [x] 将 SOP 导航逻辑从硬编码双轨路由迁移为 ReactEngine 动态工具注入
  - [x] 在 `get_sop_node` 返回中补充 `tool_calls`，将 SOP Markdown 现场命令归一化为 `acli_exec`/`bash_exec`
  - [x] 在 `get_sop_node` 返回中补充 `required_variables`，并在 `sop_advance` 前校验 `user_input/user_confirm/env_*` 变量是否就绪
- **验收**：
  - [x] 单次 SOP 相关推理的 Token 消耗 ≤ 修改前的 10%（滑动窗口 vs 全量注入）
  - [x] LLM 通过调用 `get_sop_node` 而非从全文推断来获取诊断指引
  - [x] `container_exec -n vs-cp-manager -c "smartctl -a /dev/sda"` 自动映射为 `bash_exec(container="vs-cp-manager", command="smartctl -a /dev/sda")`
  - [x] `is_sys_disk` 等 `user_input/user_confirm` 变量未就绪时，`sop_advance` 不推进节点并返回 `next_tool_call=sop_request_variable`

---

### T3-2 关键节点输出 Schema 化 【P2】

- **文件**：`backend/agent-service/app/adapters/agents/htp/investigation_agent.py`、各 Agent 模块
- **任务**：
  - [x] 定义 `ReasoningOutput` 结构化输出 schema：
    ```
    summary, hypotheses[], evidence_needed[], tool_requests[], unsupported_claims[], user_questions[], next_state
    ```
  - [x] 定义 `Hypothesis` schema：`hypothesis_id`、`statement`、`confidence`、`supporting_fact_ids[]`、`contradicting_fact_ids[]`、`verification_plan[]`
  - [x] S3 假设生成阶段输出改为 `ReasoningOutput` 结构化 JSON
  - [x] S4 验证阶段输出改为 `ClaimVerification` 结构化 JSON
  - [x] Schema 解析失败时的降级策略：降级到纯文本响应 + 禁止执行 `risk_level >= 2` 的写操作
- **验收**：S3/S4 阶段 LLM 输出可被 Pydantic 解析，schema 解析成功率 ≥ 95%

---

### T3-3 可展示推理摘要外显 【P2】

- **文件**：`backend/agent-service/app/adapters/agents/htp/investigation_agent.py`（system prompt）
- **任务**：
  - [x] 在 ReAct/Investigation Agent system prompt 中加入 `<reasoning>` 强制模板：要求 LLM 在输出结论前先按格式整理可展示推理摘要（已收集证据、假设支撑/反对、置信度评估、下一步行动）
  - [x] `<reasoning>` 标签内容在前端以折叠方式展示（非主要内容，可选查看）
- **验收**：LLM 输出包含 `<reasoning>` 结构化推理摘要的比例 ≥ 90%

---

### T3-4 轻量版 `HallucinationDetector` 规则引擎 【P2】

- **文件**：`backend/agent-service/app/adapters/agents/htp/`（新增 `hallucination_detector.py`）
- **注意**：**不引入第二个 LLM 调用**（Claim Verifier 先用规则引擎实现，避免元递归风险）
- **任务**：
  - [x] 实现 `HallucinationDetector`，检测规则：
    - 检查 LLM 输出是否引用了未执行的工具（`phantom_tool_reference`）
    - 检查是否包含强事实声明但缺乏不确定性修饰词（`overconfident_claim`）
    - 检查数字事实（百分比、GB、ms）是否在工具输出中可找到来源（`ungrounded_number`）
  - [x] 检测到高风险幻觉时，在输出末尾追加系统提示标注（不删除内容，但标注"待验证"）
  - [x] 最终诊断报告生成前，运行 `HallucinationDetector`，高风险结论需要 Agent 重新生成
- **验收**：
  - [x] 幻觉检测器在 10 个构造的幻觉测试用例中，识别率 ≥ 70%
  - [x] 检测耗时 < 100ms（纯规则引擎，无 LLM 调用）

---

### T3-5 验证节点强制绑定（"校验优先"闭环） 【P3】

- **文件**：SOP 定义层 + `react_engine.py`
- **任务**：
  - [x] 在 SOP 结构中，所有修复行动节点（Remediation）必须强绑定验证节点（Verification）
  - [x] `react_engine` 检测 Agent 宣称"修复完成"时，强制要求先调用对应的验证工具（如 `check_service_status`）
  - [x] 跳过验证直接宣布 Closure 的行为被拦截，向 LLM 返回"你还未执行验证步骤"的系统提示
- **验收**：Agent 执行服务重启后，在未调用状态检查工具前，无法生成"已恢复"的最终报告

---

**阶段三验收标准**

- [x] SOP 推理 Token 消耗降低 ≥ 90%
- [x] S3/S4 结构化输出 schema 解析成功率 ≥ 95%
- [x] 幻觉检测器识别率 ≥ 70%（构造测试集）
- [x] 无证据根因无法进入最终报告（Claim Verifier 规则引擎拦截）
- [x] 修复行动后必须执行验证，不可跳过
- [x] `<reasoning>` 可展示推理摘要强制外显

---

## 阶段四：评测闭环（预估第 13-16 周）

> **目标**：把可靠性从主观体验变成可量化指标，建立持续改进机制。
>
> **依据**：方案 C Observability Plane + 方案 B DiagnosisQualityEvaluator。

### T4-1 黄金工单评测集 【P2】

- **文件**：`evaluation/`（新建目录）
- **任务**：
  - [x] 建立黄金工单评测集（初始目标：30 个典型 HCI 故障场景，覆盖磁盘、网络、VM、存储各类别）
  - [x] 每条评测集记录包含：用户原始描述、环境事实、期望分类、期望工具调用序列、允许的根因集合、禁止出现的幻觉结论、期望最终报告结构
  - [x] 实现离线 `AgentReplayRunner`：加载历史工单，模拟 Agent 执行，记录执行轨迹
- **验收**：评测集覆盖 ≥ 30 个典型场景，Replay Runner 可在 CI 环境无 GUI 运行

---

### T4-2 核心可靠性指标接入 Prometheus + Grafana 【P2】

- **文件**：`backend/agent-service/`（新增 `metrics.py`）、`deploy/` 可观测性配置
- **任务**：
  - [x] 实现 `AgentReliabilityMetrics`，暴露以下 Prometheus 指标：
    - `agent_tool_call_success_rate`（工具执行成功率）
    - `agent_tool_timeout_rate`（超时率）
    - `agent_hallucination_detected_total`（幻觉检测计数，按 severity 分标签）
    - `agent_information_confidence_avg`（平均信息置信度）
    - `agent_unsupported_claim_rate`（无证据结论率，来自离线评测）
    - `agent_mean_steps_to_resolution`（平均解决步数）
  - [x] 在 Grafana 新增"Agent 可靠性"看板，包含上述指标的时序图和告警规则
- **验收**：Grafana 看板可显示最近 24h 的工具成功率和幻觉检测趋势图

---

### T4-3 Fact Store 从 Redis 迁移到 PostgreSQL 【P3】

- **文件**：`backend/shared/models/`、`database/` 迁移脚本
- **前提**：阶段二的 Redis Fact Store 已稳定运行 ≥ 2 周
- **任务**：
  - [x] 新增 `fact` 表（见方案 C §12.1 数据模型）
  - [x] 新增 `claim_evidence_link` 表（见方案 C §12.3 数据模型）
  - [x] 迁移 EvidenceBuilder 的数据源从 Redis 改为 PostgreSQL
  - [x] 保留 Redis 作为热数据缓存（TTL 5 分钟），PostgreSQL 作为持久化存储
- **验收**：工单关闭后 30 天内，仍可通过 `fact_id` 追溯任一诊断结论的证据来源

---

### T4-4 CI 回归评测门禁 【P3】

- **文件**：`.github/workflows/`
- **任务**：
  - [x] 新增 CI job `agent-reliability-regression`：任何修改 `react_engine.py`、工具 schema、system prompt 的 PR，自动触发 Replay Runner 跑黄金工单评测集
  - [x] 对比指标：幻觉率、工具成功率、路径偏差率，任一指标劣化 ≥ 10% 则阻断合并
  - [x] 评测报告上传为 artifact，并在 PR 评论中展示汇总指标
- **验收**：CI job 可在 10 分钟内完成 30 个测试用例回归，结果报告在 PR 评论中展示

---

**阶段四验收标准**

- [x] 黄金工单评测集 ≥ 30 条，覆盖主要故障类别
- [x] Grafana 看板展示实时工具成功率、幻觉检测趋势
- [x] CI 回归评测可拦截明显降低可靠性的变更
- [x] 每次 Agent 核心改动能看到幻觉率、工具成功率、路径偏差率变化

---

## HCI/aCLI 实机契约收敛任务（2026-07-29）

权威事实与决策边界见 [HCI 底层目录、日志、容器与 aCLI 知识基线](../../solution/agent/02-架构设计/HCI底层目录日志容器与aCLI知识基线.md)。本任务坚持轻治理：先修确定错误，再用设备探测和确定性验证提高自动化，不增加双审或复杂审批状态机。

| 阶段 | 任务 | 状态 | 验收标准 |
|---|---|---|---|
| CI | 将 qfk_log 合法 fixture 改为 basename `file` + 独立 `path` | 已完成 | `check_signal_schemas.py` 通过 |
| CI | 增加 `file` 混入完整路径的非法反例 | 已完成 | 反例必须被 JSON Schema 拒绝 |
| 文档 | 建立 `/sf/log`、blackbox、`/cfs`、`/sf/cfg`、`/sf/data`、补丁、容器和 aCLI 基线 | 已完成 | 事实、推论、建议与跨版本假设分层 |
| P0 | qfk_log 路径规范化并按目标 aCLI 收紧允许根 | 待确认 | `..`、控制字符与越界根目录 Fail Closed |
| P0 | 将相对事故时间转换为 aCLI 接受的绝对日期 | 待确认 | 不再向 `-t` 传 `-1h`/`now` |
| P0 | 将无运行时数据源的 `qkv_dialog` 降级为 unsupported | 待确认 | UI、Prompt、Descriptor 与执行器状态一致 |
| P0 | 风险分类纳入具体参数、设备 manifest 和历史解压副作用 | 待确认 | 未知命令不再默认 risk=1/auto |
| P1 | 生成产品版本、aCLI 版本、manifest hash 的设备探测快照 | 待实施 | 可对比代码、数据库、设备与政策四层状态 |
| P2 | 在现有工具/KBD 页面增加“探测、对比、只读验证” | 待实施 | 专家一屏修改并立即回放，无新增复杂工作流 |
| P3 | 增加 blackbox、计数、阈值、范围、趋势 predicate | 待实施 | 不再用关键字搜索模拟数值/时间序列判断 |
| P4 | 将专家字段级 diff 转为 validator、Prompt 与模型评测数据 | 待实施 | 只有执行回放通过的数据可升级为 Expert Gold |

当前质量口径保持不变：126/126 真实来源完整；122 条为自动 LLM Proposal；4 条为 engineering Contract fixture；0/126 Expert Gold；0/126 正式业务专家批准；完整 Evidence/Execution Replay 尚未完成。

---

## 跨阶段关联约束

| 任务 | 前置依赖 | 说明 |
|------|---------|------|
| T1-4 工具执行结果落库 | T0-1 ToolResultEnvelope | 需要结构化结果才能落库 |
| T2-2 事实写入 Redis | T0-1 ToolResultEnvelope | 工具输出成为 Fact 的 raw_ref |
| T2-3 EvidenceBundle 注入 S0 | T2-1 InformationPacket 定义 | 数据结构必须先定义 |
| T3-1 SOP 滑动窗口 | T1-2 exec_id 确认机制 | SOP 导航工具调用需要可追踪的 exec_id |
| T3-2 输出 Schema 化 | T2-1 InformationPacket | Hypothesis 的 `supporting_fact_ids` 需引用 Fact |
| T3-4 HallucinationDetector | T2-2 事实写入 | 需要 tool_results 用于数字事实来源核查 |
| T4-3 Fact Store 持久化 | T2-2 Redis Fact Store 稳定运行 2 周 | 先验证 Redis 方案，再迁移 PG |
| T4-4 CI 回归门禁 | T4-1 黄金工单评测集 | 需要评测集才能运行回归 |

---

## 文档关联

- **方案分析** → [Agent可靠性三方案对比分析](../../solution/agent/Agent可靠性三方案对比分析.md)
- **三份原始方案草稿**：
  - [agent_problems_solution.md](../../solution/agent/agent_problems_solution.md)（方案 A）
  - [agent_reliability_solution.md](../../solution/agent/agent_reliability_solution.md)（方案 B）
  - [排障Agent可靠性整体解决方案.md](../../solution/agent/排障Agent可靠性整体解决方案.md)（方案 C）
- **现行 Agent 设计** → [agent设计.md](../../solution/agent/agent设计.md)
- **工具设计** → [agent工具设计.md](../../solution/agent/agent工具设计.md)
- QFK handlers/signal/kbd_differential 更新

---

## 2026-07-24 · v2 信号契约拍平与数据迁移对齐

- **契约拍平**：`acquire.args` 字段对齐运行时采集器（`target.scope`/`resource`/`path` → `host`/`file`/`container`，`sub_command` → `command`，`description` → `instruction`），覆盖 11 个 `acquirer_args/*.schema.json` 与 `shared/schemas/acquirer_args.py`。
- **Schema 修复**：`signal.v2.schema.json` 的 `match` 段扩宽为支持 6 类判定（`keyword`/`regex`/`state`/`threshold`/`json_path`/`exists`）落库，消除契约/运行时不一致坑。
- **数据迁移**：新增 `010_flatten_v1_signal_fields.sql` 将存量 `signals_json` 的 v1 残留字段拍平为 v2 规范名；`02_system_prompts.sql` 的 KBD 抽取 prompt 同步更新字段对照。
- **文档**：新增 `QKV_QFK信号模型v2参考.md`，改写 `QKV_QFK信号配置操作指南.md`（去 v1 扁平词汇）；`架构设计.md` / `数据库设计.md` 同步追加变更记录。

---

## 2026-07-26 · 分类驱动 KBD 主动诊断与结论门禁

- [x] S1 直接消费 S0 已确认分类的完整 KnowledgeSnapshot，不再用 route、embedding、FTS 或 top-K 过滤分类内 KBD。
- [x] 引入 SignalPlan、acquisition graph 和主动调度器，按判别力、required coverage、解锁价值、复用价值、成本、延迟和风险稳定排序。
- [x] 工具动作只从版本化 KBD signal 编译；共享 acquisition 只有一个 `exec_id`，每条 signal 独立生成 `evaluation_id` 并确定性求值。
- [x] 引入候选状态和四级 Conclusion Gate；required FAIL 后只取消被拒 KBD 的独占动作。
- [x] 仅 `DEFINITIVE` 可进入 S4；工具错误、缺变量和未决候选不能输出 KBD 根因或方案。
- [x] KBD 27123 golden case 和 agent-service 单元回归覆盖新不变量。

详细设计见 [KBD 主动诊断信号调度与证据闭环算法设计](../../solution/knowledge-base/events/2026-07-26-KBD主动诊断信号调度与证据闭环算法设计.md)。

## 2026-07-28 · Terminal Bridge P0 端到端可观测性与执行结果调优数据面

- [x] Agent 工具调用统一透传 `trace_id`、`traceparent`、`exec_id`、`tool_call_id` 和 `artifact_id`。
- [x] Terminal Bridge 命令结果记录退出码、错误类型、超时、耗时、输出字节数、hash 和截断标志，支持 Langfuse、Artifact、Tool Audit、Tempo 与 Prometheus 交叉核验。
- [x] Bridge 支持命令级 timeout 和受约束的字面量逐行输出筛选，禁止通过输出筛选参数注入 shell/正则/脚本。
- [x] WSL/K3s Pod 与 Windows 客户端复用同一套 Bridge 代码，并完成真实 HCI SSH 端到端验收。

详细设计与验收证据见 [Terminal Bridge 端到端可观测性重构设计](../../solution/observability/2026-07-27-terminal-bridge端到端可观测性重构设计.md) 和 [最终验收报告](../../verify/events/2026-07-27-terminal-bridge端到端验收报告.md)。

## 2026-07-29 · PR #632 真实入口重验追加门禁

- [x] 普通 Markdown 自动执行旁路已删除，S0 伪命令输出双门禁和 Alloy 数据面健康门禁已通过负向真实验收。
- [x] 真实 KBD 路径已经通过 `agent_exec_command → ssh_exec_process → Bridge → HCI → /exec-result` 执行 3 条命令，并在 Tempo/Loki/Artifact/Prometheus 形成证据。
- [x] 修复 conversation trace parent 构造、KBD QKV/QFK Langfuse TOOL、DiagnosticItemClient 注入、Tool Audit Artifact 关联和 Bridge raw/filtered 统计缺口；定向自动化回归通过。
- [x] K3s 部署修复镜像并完成三信号全部 PASS 的真实 HCI 正向验收（`Q2026072939295` / KBD `27123`）。
- [x] 确认同一 trace 下 Langfuse、Diagnostic Item、Tool Audit、Artifact、Tempo、Loki、Prometheus 全量互查一致；修复后真实 TOOL 的零值/null 字段 12/12 完整。
- [x] 正向与负向门禁全部通过，恢复“端到端完整可观测”结论；hci-sim 技术 Spike 可在用户明确决定投入后启动。

当前状态以 [2026-07-29 真实入口重验事件](../../verify/events/2026-07-29-terminal-bridge真实入口P0修复与端到端重验.md) 为准；2026-07-27 报告是 PR #632 合并时的历史验收，不覆盖本次真实业务案例追加门禁。
