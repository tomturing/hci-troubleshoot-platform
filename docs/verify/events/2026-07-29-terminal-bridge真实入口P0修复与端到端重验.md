---
status: active
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
| 自动化测试 | Triage/前端/Helm 回归 | Aggressive + connected + 10 秒仍零执行 | 已通过：Agent 409 passed/1 skipped；UI 107/107；Go/Helm/文档门禁通过 |
| UI/SSE | 收到 `agent_exec_command` 与 `exec_id` | 不出现可执行伪输出 | 负向通过；正向等待 HCI SSH 重连 |
| WebSocket/Bridge | `ssh_exec_process`，exec counter +1 | `ssh_input`/exec counter 均不增长 | 负向通过：counter `0 → 0`；正向待验 |
| 结果与审计 | `/exec-result`、tool turn、Artifact 各一条 | 均为零 | 负向通过；正向待验 |
| Trace/Log/LLM | Tempo/Loki/Langfuse 可按 trace_id/exec_id 互查 | 无伪造 TOOL/Span | 负向通过；正向待验 |
| Metrics | Bridge counter 增长、Alloy target up 且无 drop | counter 不增长 | Alloy `up=1`、四规则 health=ok、drop 增量=0；正向 Bridge 增量待验 |

负向真实样本：case `Q2026072884353`，conversation `ab7e517c-ac53-4988-a89c-b619c62c2128`，trace `ac62ab011171f574366c325e02f2f796`。SSE 只返回“本次未执行任何命令”，Agent 命中 `triage_command_execution_blocked`；Bridge 无消息、DB 无工具轮次、Artifact 为 0。

Alloy 数据面样本：Prometheus target `http://10.42.0.36:12345/metrics` 为 up，四条 Alloy 告警均 inactive/ok；新 Terminal Bridge event `150185c6-06b9-5bd2-9811-032c69b45941` 已从 Loki 命中。

## 5. 放行标准

只有真实 S1/ReAct 自主调用只读工具的正向链路和原复现文本/普通 Markdown 的负向链路全部通过，才允许恢复“Terminal Bridge P0 端到端完整可观测”的结论。任一层缺少同一 `trace_id + exec_id` 证据都不放行 hci-sim 技术 Spike。
