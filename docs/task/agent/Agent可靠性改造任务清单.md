---
status: active
category: task
audience: developer
last_updated: 2026-06-13
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
