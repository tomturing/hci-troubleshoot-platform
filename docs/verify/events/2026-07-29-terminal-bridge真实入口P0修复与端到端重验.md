---
status: completed
category: verify
audience: all
last_updated: 2026-07-29
owner: team
---

# Terminal Bridge 真实入口 P0 修复与端到端重验

## 1. 事件与范围

PR #632 的首轮真实 UI 验收使用工单 `Q2026072884353`。页面表现与预期不一致后，按用户入口、SSE、WebSocket、Bridge、HCI、结果回传、数据库、Loki、Tempo、Langfuse 和 Prometheus 逐层核对。本事件只修复并重验 Terminal Bridge P0，不启动 hci-sim。

## 2. 首轮分析证据

- 工单仍在 S0，实际由不具备工具能力的 TriageAgent 处理；模型返回了看似命令输出的 fenced `bash` 内容。
- Customer UI 把普通 Markdown 升级为 `CommandBlock`，Aggressive 模式倒计时后经 `ssh_input` 执行展示文本；HCI 对 `PR632_E2E_OK`、`Linux` 返回 command not found/127。
- Bridge `bridge_exec_commands_total` 未增长，DB 无 `tool_call/tool_result`，无 Artifact，Langfuse 无 TOOL observation，命令请求 Trace 无 terminal-bridge Span。
- Loki 的消息类型为 `ssh_input`，不是受审计的 `ssh_exec_process`。因此首轮结果既不是 stdout 回灌，也不能证明 Agent 工具链或完整可观测性通过。
- 同期 Alloy 在旧 limit 下 CPU 顶满且 `/metrics` 超时，Pod 仍 Ready，新 Bridge 日志不再进入 Loki；扩容后恢复，证明进程存活不能替代流水线健康验收。

## 3. 修复决策

1. **展示面与控制面物理分离**：普通 Markdown 永不自动执行，只保留复制和人工发送；自动执行只接受服务端 `agent_exec_command + exec_id`。
2. **S0 双门禁**：明确执行祈使句在 LLM 前确定性拒绝；模型回复中的 shell fence、执行结果和退出码在透传前整体阻断，日志只留内容 SHA-256。
3. **Alloy 四层健康**：资源基线、HTTP 探针、Prometheus 抓取、日志数据面告警与唯一标识查询共同构成 Ready。

## 4. 验收矩阵

| 层级 | 正向 S1/ReAct | 负向 S0/Markdown | 当前状态 |
|------|---------------|------------------|---------|
| 自动化测试 | Triage/前端/Helm 回归 | Aggressive + connected + 10 秒仍零执行 | 已通过：Agent 全套 591 passed/1 skipped；Conversation unit 215/215；UI 107/107；Go/Helm/Ruff/文档门禁通过 |
| UI/SSE | 收到 `agent_exec_command` 与 `exec_id` | 不出现可执行伪输出 | 负向通过；正向 KBD 27123 三信号全部 PASS，并完成修复后最小复验 |
| WebSocket/Bridge | `ssh_exec_process`，exec counter 增长 | `ssh_input`/exec counter 均不增长 | 负向 `0 → 0`；正向累计 `0 → 8`，8 次执行均走受审计入口 |
| 结果与审计 | `/exec-result`、Tool Audit、Artifact、Diagnostic Item 对齐 | 均为零 | 通过：三信号 Tool Audit/Artifact/S2-S4 互相对齐，重复执行按确定性 exec_id 幂等更新 |
| Trace/Log/LLM | Tempo/Loki/Langfuse 可按 trace_id/exec_id 互查 | 无伪造 TOOL/Span | 通过：Tempo/Loki/Langfuse 均命中同一 trace，确定性 QKV/QFK TOOL 完整 |
| Metrics | Bridge counter 增长、Alloy target up 且无 drop | counter 不增长 | 通过：counter=8、errors=0；Bridge/Alloy `up=1`，drop=0，无相关 firing alert |

负向真实样本：case `Q2026072884353`，conversation `ab7e517c-ac53-4988-a89c-b619c62c2128`，trace `ac62ab011171f574366c325e02f2f796`。SSE 只返回“本次未执行任何命令”，Agent 命中 `triage_command_execution_blocked`；Bridge 无消息、DB 无工具轮次、Artifact 为 0。

Alloy 数据面样本：Prometheus target `http://10.42.0.36:12345/metrics` 为 up，四条 Alloy 告警均 inactive/ok；新 Terminal Bridge event `150185c6-06b9-5bd2-9811-032c69b45941` 已从 Loki 命中。

## 5. 第二轮真实正向分析

工单 `Q2026072816487`、conversation `f9917f31-70e2-45ea-ad41-230b26897c41` 在用户发送“继续诊断”后产生 trace `0e80bfd78d5f052bcfb7ae67c8180b30`。本轮已经证明真实入口可用，但不能作为最终通过结论：

1. QKV task 执行成功，产出 `VM=18864231143`、`HOST=172.28.25.4`、`END=2026-07-29 07:54:12`；
2. 节点解析执行成功；
3. `acli system lsof` 在目标节点退出码为 0，但安全筛选后 stdout 为 0，未产出 PID；
4. 第三步 `ps -p {{PID}} -o cmd=` 因 PID 缺失按依赖门禁 BLOCKED，没有进入 Bridge；
5. 最终报告为“证据不足”，符合 Conclusion Gate，不是假阳性。

对该次 Artifact 的受控复盘还确认：筛选后 stdout 为 0，stderr 的 6006 字节主要是重复的 `lsof: no pwd entry for UID 65535+` 警告，退出码仍为 0。它能解释 stderr 噪音，但旧 Bridge 没有 raw stdout 统计，仍不能证明原始 stdout 物理为空；因此必须由修复后的 raw/filtered 指标完成最终归因。

同一 trace 在 Tempo 有 184 个 Span、覆盖 6 个服务；3 次真实 Bridge 执行各有 `websocket.receive → ssh.exec → websocket.result.send` 三段 Span。Loki 命中 67 条日志，Artifact 有 3 条，Prometheus `bridge_exec_commands_total=3`。但现场同时发现以下 P0 缺口：

- conversation-service 错用不存在的 `trace.generate_span_id/attach/detach`，导致工单 trace 入口无法恢复；
- 确定性 KBD QKV/QFK 路径未创建 Langfuse TOOL observation；
- `DiagnosticItemClient` 已实现但未注入 `InvestigationAgent`，S2/S3 诊断项为空；
- KBD `tool_result` 只写基本状态，未按 exec_id 关联 Artifact，且依赖阻断被笼统记录为 failed；
- Bridge 的 `stdout_len=0` 只表示筛选结果为空，无法区分“远端原始流为空”与“原始流有数据但零行命中”。

## 6. 第二轮后 P0 修复

- 使用合法非零 64-bit Span ID 构造 `NonRecordingSpan`，通过 `opentelemetry.context.attach/detach` 激活和恢复会话 trace；
- 在 QKV/QFK 的真实 `_executor.execute()` 边界创建 Langfuse TOOL observation，只记录 exec/artifact/hash/字节数/截断/退出码/错误类型/耗时，不复制原始输出；
- 生命周期中创建、注入并关闭 `DiagnosticItemClient`；
- conversation-service 在 `tool_result` 阶段按 exec_id 回查 `bridge_execution_artifacts`，补齐 `exec_id/artifact_id/output_sha256/error_type/bridge_trace_id/duration_ms`；依赖缺失稳定记录为 `blocked + blocked_dependency`；
- Bridge 分别统计 raw 与 filtered 字节数、SHA-256、扫描行数、保留行数和 filter_applied；原始正文仍不会进入 WebSocket、HTTP、Artifact 或 Langfuse。

自动化回归已通过：Agent 全套 `591 passed, 1 skipped`（其中 KBD/QKV/QFK/Diagnostic Item 定向 `96/96`），conversation-service unit `215/215`（新增零值契约测试后），Terminal Bridge Go tests 全部通过，Ruff 全部通过。三个修复镜像已构建、导入并滚动部署到 dev K3s，Pod 和探针均健康。该段记录的是第三轮真实复验前的部署状态；最终放行结论见第 8、9 节。

## 7. 第三轮真实正向验收

新工单 `Q2026072939295`、conversation `9e5da773-7462-44e6-aa0a-c3510fabf315` 的权威 trace 为 `c6acbb7bdf5faffaedf5a6faa04eeb97`。用户从 customer-ui 建立 SSH 后完整跑完 KBD `27123`，页面生成根因与解决方案；结构化数据面确认这不是只依赖最终文本的表面闭环。

### 7.1 业务信号与诊断项

- S2 hypothesis 已创建，`support_id=27123`；
- S3 `sig_001/sig_002/sig_003` 均为 `confirmed + PASS`，对应 exec 分别为 `c2a191ae-b71c-5cf0-882f-7b28940ecb3b`、`6eb6a779-4fc0-521c-9af3-25312fc9a030`、`1ede201d-e643-5326-987e-b23a2b726276`；
- S4 root_cause 为 `confirmed`，`is_definitive=true`，`evidence_exec_ids` 精确包含上述三条 KBD exec；
- 内部 node resolve 辅助执行 `da03985c-9499-4db1-9927-96bb392bb7ad` 只负责从任务结果解析目标节点，不作为独立 KBD S3/Tool Audit 条目，符合工具语义边界。

### 7.2 Artifact、Tool Audit 与 Bridge 物理流

4 条真实 Bridge Artifact 全部 `exit_code=0/status=success/trace_id=c6ac...`：

| 执行 | exec_id | artifact_id | stdout/stderr 字节 | Bridge 耗时 |
|---|---|---|---:|---:|
| QKV task | `c2a191ae-b71c-5cf0-882f-7b28940ecb3b` | `2c99f0a7-0114-5cc2-ab89-2d7c42830401` | 1363 / 0 | 656 ms |
| node resolve | `da03985c-9499-4db1-9927-96bb392bb7ad` | `9016e8e9-2e28-5fff-b8f7-d6cd57706d91` | 574 / 0 | 1033 ms |
| lsof | `6eb6a779-4fc0-521c-9af3-25312fc9a030` | `8f2f444f-4ae8-5dbc-bb00-d83329a9c6ee` | 418 / 6006 | 34313 ms |
| ps | `1ede201d-e643-5326-987e-b23a2b726276` | `fdbf9489-d36f-569d-958e-5055aa6e717b` | 132 / 0 | 262 ms |

三条 KBD Tool Audit 均为 success，`exec_id/artifact_id/trace_id/bridge_trace_id/output_sha256/duration_ms` 完整且与 Artifact 对齐。node resolve 是 QFK 调度器的内部路由辅助命令，存在 Artifact 和 Bridge Span，但不伪装成一条业务 ToolResult。

Bridge `exec.done` 在数据库和 Loki 中均保留 raw/filtered 统计。关键 lsof 物理流为 `61,334,411 bytes / 408,665 lines`，安全筛选后为 `418 bytes / 2 lines`，`filter_applied=true` 且 raw/filtered SHA-256 不同；ps 为 `133 bytes / 2 lines`，筛选后 `132 bytes / 1 line`。这证明旧样本的零命中不是“远端没有输出”，本次现场信号真实存在，且 61 MB 原始流没有进入浏览器、HTTP、Artifact 或 Langfuse。

### 7.3 Tempo、Loki、Langfuse 与 Prometheus

- Tempo：同一 trace 共 438 个 Span，覆盖 agent-service 228、api-gateway 30、case-service 31、conversation-service 120、kb-service 17、terminal-bridge 12；4 次 Bridge 执行各有 `websocket.receive → ssh.exec → websocket.result.send`，没有 trace_id 分叉。长会话中的浏览器和 `NonRecordingSpan` 恢复边界会出现未导出的远端父 Span 引用，但不生成第二个 trace；这是当前“一案一 trace”模型的拓扑边界，不影响按 trace/exec 互查。
- Loki：同一 trace 命中 159 条日志，覆盖 agent/api-gateway/case/conversation/kb/terminal-bridge 六类容器；Bridge 的 request/start/done 各 4 条，raw/filtered 字段可以直接检索。
- Langfuse：session `Q2026072939295` 只有一个 trace，trace ID 与 OTel 权威 trace 一致；9 个 observation 中包含 `tool.qkv_task` 一条和 `tool.qfk_system` 两条，exec/artifact/hash/非零字节/退出码/截断/耗时均可回查。
- Prometheus：`bridge_exec_commands_total=4`、`bridge_exec_command_errors_total=0`、`bridge_websocket_connections_active=1`、Bridge target `up=1`；Alloy target `up`，配置加载失败、Loki dropped bytes/entries、Kubernetes SD failure 和网络拨号失败均为 0。

### 7.4 验收中追加发现的零值字段缺口

字段级检查发现：Bridge 的 `OutMessage` 使用 Go `omitempty`，当 stderr 为 0 字节时浏览器没有收到 `stderr_bytes`；conversation-service 虽已在 Artifact 中按正文计算为 0，但送回 Agent 的 Redis result 仍保留为 null。与此同时，`exec_result_observation()` 会过滤值为 null 的字段，导致本轮 Langfuse QKV/ps TOOL 缺少显式 `stderr_bytes=0`，成功记录也缺少显式 `error_type=null`。这不影响命令执行、Artifact、Tool Audit 或诊断结论，但违反改进手册的稳定字段集合要求，因此本轮不能直接宣称最终全绿。

已追加修复：conversation-service 对旧客户端缺失的字节数按 UTF-8 正文兜底，并把有效值同时写入 Artifact 和 Redis result；Agent Langfuse 摘要不再过滤 null/零值。定向测试 QKV/QFK `47/47`、conversation 执行契约 `10/10`、Ruff 与 diff check 通过；新镜像已导入 dev K3s 并滚动成功，Pod 内运行时探针确认完整 12 字段集合以及 `stdout_bytes=0/stderr_bytes=0/error_type=null`。修复后的真实验证结果见下一节。

## 8. 第四轮修复后最小复验

用户在同一真实工单中请求基于 KBD 27123 重新执行只读关键信号验证。Bridge counter 从 4 增长到 8，errors 保持 0；新一轮 4 次执行继续使用权威 trace `c6acbb7bdf5faffaedf5a6faa04eeb97`。QKV 和 lsof 使用确定性 exec_id 幂等更新，node resolve 与 ps 分别生成 `9e057683-ea6c-438c-8a36-a4242d8efbc6`、`b8e0110f-1eb2-53b4-83fe-cd971eb9576e`。

修复后的 3 条 Langfuse TOOL 全部具备稳定的 12 字段集合：

| TOOL | exec_id | stdout/stderr bytes | 截断 | error_type | 字段完整 |
|---|---|---:|---|---|---|
| `tool.qkv_task` | `c2a191ae-b71c-5cf0-882f-7b28940ecb3b` | 1363 / **0** | false / false | **null** | 12/12 |
| `tool.qfk_system`（lsof） | `6eb6a779-4fc0-521c-9af3-25312fc9a030` | 418 / 6006 | false / false | **null** | 12/12 |
| `tool.qfk_system`（ps） | `b8e0110f-1eb2-53b4-83fe-cd971eb9576e` | 132 / **0** | false / false | **null** | 12/12 |

三条 TOOL 的 `otel_trace_id/artifact_id/stdout_sha256/stderr_sha256/exit_code` 与 Artifact、Tool Audit 完全一致。Tempo 更新为 612 个 Span，其中 terminal-bridge 24 个，正好对应 8 次执行的三段 Span；仍只有一个 trace ID。新时间窗 Loki 命中 104 条日志，覆盖 agent/api-gateway/conversation/kb/terminal-bridge，Bridge request/start/done 各 4 条，lsof raw `61,272,982 bytes`、filtered `418 bytes`。Prometheus 为 commands=8、errors=0，Bridge/Alloy target 均 up，Alloy dropped entries/bytes=0，无 Alloy/TerminalBridge 相关 firing alert。

最后一个 Langfuse 零值/空值字段门禁已由真实生产路径样本关闭。

## 9. 放行标准

只有真实 S1/ReAct 自主调用只读工具的正向链路和原复现文本/普通 Markdown 的负向链路全部通过，才允许恢复“Terminal Bridge P0 端到端完整可观测”的结论。任一层缺少同一 `trace_id + exec_id` 证据都不放行 hci-sim 技术 Spike。

本事件已经满足上述标准：负向样本零执行，正向样本三信号全部 PASS，修复后最小复验的 Langfuse/Diagnostic Item/Tool Audit/Artifact/Tempo/Loki/Prometheus 可按同一 trace 和 exec 互查。因此 Terminal Bridge P0 技术门禁恢复为通过，hci-sim 技术 Spike 可以在用户明确决定投入后启动；本次任务本身不实施 hci-sim。
