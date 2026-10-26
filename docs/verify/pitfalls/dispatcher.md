---
status: active
category: verify
audience: developer
last_updated: 2026-10-15
owner: team
---

# Dispatcher / 状态机 / 幂等资源管理避坑

## PIT-006：状态机转换未加分布式锁导致重复处理

并发场景下，状态转换前必须使用 `SELECT FOR UPDATE` 或 Redis 分布式锁，防止同一任务被多个 worker 同时处理。

## PIT-007：幂等键未覆盖所有入口

外部回调、重试队列、手动触发三个入口都需要幂等检查，只在其中一个入口加锁不够。

## PIT-008：Dispatcher 重启后未恢复 in-flight 任务

进程崩溃后处于 `running` 状态的任务不会自动回到队列，需要在启动时执行 `recover_stuck_tasks()`，把超时的 running 任务重置为 `pending`。

## V-017：自动取值执行器调用漏传 conversation_id + 泛化 except 吞掉真实原因

工单 Q2026100812343 复盘：`sop_request_variable` 的 `tool_call` 策略调用
`tool_executor.execute(acquisition_tool, tool_args)` 未透传 `conversation_id`，命中执行器
`缺少 conversation_id` 早退分支（`_extract_value` 得 `None`），命令从未下发；却被泛化
`except Exception` 归一为 `sop_tool_variable_acquire_failed："工具执行或取值失败"`，误导排查。

**规则：**
1. 跨层执行器调用必须显式透传会话级上下文（`conversation_id`/`case_id`），不能依赖共享单例的实例属性缺省值。
2. 失败分支必须按 `error_type` 分类落库并保留真实原因（契约错误 / 不可达 / 超时 / 取值为空 / 工具能力缺失），禁止大而全、模糊归一的错误信息。
3. SOP 编排模式下采集失败应"失败即接管"转人工，不得把控制权交回模型即兴敲命令。

## V-021：S2/S3 假设生成 user_questions 不受变量池事实源约束，重复索取已提交变量并凭空索要未声明字段

工单 Q2026101511258「硬盘坏道」复盘：用户已通过 SOP 变量弹框提交 `disk_dev`（后端 `set_variable` 已写库、清 `pending_variable_name`、状态回 active，前端「已提交」态正确），但下一轮进入 S2 假设生成时，`ReasoningOutput.user_questions`（[shared/models/reliability.py](../../../backend/shared/models/reliability.py)）仍续写出「请正式提交 disk_dev，该槽位仍为 needs_input」并索要 SOP#4 根本未声明的 `disk_sn`/`disk_type`。

**根因**：`user_questions` 是模型自由编写的字符串列表，**不经任何变量池/门禁校验**；模型从对话历史里 S1 阶段遗留的失败原文（asan_disks 截断、smartctl 报错、「请人工提供该变量值」）中「续写」出貌似合理的需求清单，而非以当前变量池为事实源。现有反幻觉硬拦截（V-017 §3.3）只覆盖「结论语境 + 无溯源数值」，对「提问语境 + 无依据需求项」无拦截，仅追加「待验证」标注仍原样抵达用户。

**规则：**
1. 任何「向用户索取信息」的自由文本通道（`user_questions`/`evidence_needed`/语义 `manual_evidence_request`）都必须以变量池/表单为权威事实源做后置过滤，不得只靠 Prompt 约定。参照 `_evidence_request_satisfied` 的 bigram 兜底思路，本仓库在 [react_engine.py](../../../backend/agent-service/app/adapters/agents/htp/react_engine.py) 的 `filter_ungrounded_user_questions` 用「已就绪变量名 + SOP 声明变量名」确定性剔除「重复索取已提交变量」「引用未声明的变量样 token」两类幻觉。
2. 过滤必须带「无事实源护栏」：拿不到 `variable_schema` 声明集（加载失败）时**整体不过滤**，否则空 known 会把所有含下划线 token 的合法追问误删。
3. 提及已就绪变量的追问按冗余处理（可能误伤「提及但不索取」的授权类问句，属可接受的低价值损失，模型可下轮重问）；Prompt 侧同步在 `s1_react_output_constraint_v1` 追加「已知变量权威」规则从源头收敛。
4. 教训：结构化输出（`response_schema`）的每个「需求类」字段都不能默认可信——模型会把历史错误文本当作当前事实续写，必须以运行时权威状态做模型无关校验。

## V-035：SOP 变量声明无契约，任意字符串都能成为"变量"，导致"填了还要填"死循环

工单 Q2026102600003 复盘：用户通过变量卡片提交「显卡切分方式不匹配或显卡资源不足」后，弹框消失、`sop_execution.context_variables` 也确实写入了，但下一轮仍在索要变量。查库发现变量池里只有 `{"任务原始数据": {...}}` —— 一个 **SOP schema 中不存在的键**，而真正的 `vmid` / `task_logs` / `hostname` 从未被写入。

**完整因果链**：

1. SOP#3 的 Markdown 变量表「依赖」列写的是自然语言 `任务原始数据`（应为变量名）；
2. 解析器原样入库，`task_logs.depends_on = ["任务原始数据"]`；
3. `sop_request_variable("task_logs")` 判为依赖悬空，返回 `next_tool_call` **引导模型去请求这个不存在的变量**；
4. `sop_request_variable("任务原始数据")` 在 schema 中找不到、字符级 Jaccard 对中英混排恒为 0、无相似项，于是**降级为自由输入**，下发 `needs_input=True` 卡片；
5. 用户填值写入野键，`pending_variable_name` 被清空（前端显示"已提交"），但 SOP 永远消费不到 → 下一轮继续索要。

**规则**：

1. 变量声明必须有值对象契约（`shared/models/sop_variable.py`），**构造即校验**：标识符、类型、策略可识别性、策略↔字段矩阵、依赖存在性、DAG 无环、重名。裸 `dict.get()` 取值一律视为缺陷。
2. **禁止"降级自由输入"**。未声明变量必须 `fail-loud`（`sop_variable_not_defined` + `config_error=true`）。任何"找不到就让人填"的兜底，实质是把配置错误伪装成正常业务。
3. 依赖指向未声明变量时，**不得返回 `next_tool_call` 指向该变量**——这等于把模型推进死循环。应返回 `sop_schema_invalid_dependency` 并中止。
4. 三道闸（save / publish / runtime）必须复用同一校验器，且 **save 闸要覆盖所有写入路径**。本项目 `variable_schema` 只有 2 处写库（approve / PATCH），历史上前者只校验工具注册、后者漏检依赖，命名 `validate_variable_schema_dependencies` 却从不校验 dependencies，直接导致坏配置上线。
5. 章节级变量表若无法表达 `acquisition_tool`，解析结果必然是 `tool_call + tool=NULL`，运行时会静默转人工。此类变量应按语义改为 `path_template` / `tool_call:xxx` / `user_input + 明确 description`，不得留空。
6. 诊断"填了还要填"时，**先查 `sop_execution.context_variables` 的键名是否 ∈ schema 声明集**，再谈前端态机与模型幻觉。键名不匹配时，前两次修复（#1149 前端已提交态 / #1151 user_questions 过滤）都只是在给症状打补丁。
