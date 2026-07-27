---
status: active
category: event
audience: developer
date: 2026-07-27
related_prs: []
owner: team
---

# Terminal Bridge 可观测性改进前后差异与效果报告

| 能力 | 改进前 | 改进后 | 可验证效果 |
|---|---|---|---|
| Bridge 地址 | localhost 与 Pod 路径概念混淆 | desktop 保持 localhost；Pod 仅用 Ingress path | 不分叉协议和源码 |
| Trace | 只传 trace ID，固定 Span ID=1，不导出 | 完整 traceparent + OTel Go SDK真实 Span | Tempo 可显示父子层级 |
| 结果回传 | 新 Trace，链路在浏览器处分叉 | Bridge 结果 traceparent 进入 HTTP header | 同一 Trace ID覆盖返回路径 |
| 输出内存 | Go strings.Builder和浏览器 Map 无界 | stdout/stderr各4 MiB上限，仍持续 drain | 避免 Bridge/浏览器 OOM |
| 超时 | 浏览器30秒、Agent 30秒先超时，远端可能继续；仅关 Session 仍可能等待进程自然结束 | Bridge 120秒权威超时，每次执行使用独立 SSH TCP 连接+Session；浏览器/Agent/Redis依次为135/150/180秒 | 2秒阈值下从修复前5008ms降至修复后2002ms，超时真实中止 |
| 完整输出 | Redis临时保存，日志只留 preview | 专用 Artifact，30天保留，hash与截断元数据 | Agent 调优证据可复现 |
| 普通日志 | 混合 JSON/文本，可能含 raw payload/命令 | 一行 JSON，敏感字段脱敏，命令hash | Loki解析稳定、泄漏面降低 |
| 日志时间 | Bridge ts/seq在API丢失 | event_time、observed_time、instance、seq专列 | 可重建真实事件顺序 |
| 幂等 | HTTP响应丢失会重复插入 | event_id、instance+seq唯一，ON CONFLICT | 重试不重复 |
| 回采失败 | 第5次失败后丢弃 | localStorage Outbox持续退避 | 浏览器重启后可续传 |
| SSH指纹 | InsecureIgnoreHostKey | 默认 accept-new，持久 known_hosts；可 strict | 已知主机指纹变化被拒绝 |
| 凭据内存 | lastAuth跨连接保留 | WebSocket断开即清理 | 缩短凭据驻留时间 |
| 日志采集 | EOL Promtail + Docker parser | Alloy + CRI parser | 对齐K3s containerd和官方生命周期 |
| Langfuse | tool仅有exec/session/risk | 增加 OTel Trace、Artifact、hash、error type | 模型效果与系统证据互查 |
| 指标 | success/latency不完整，错误未分类 | success、latency、error_type三维 | 可按工具定位可靠性瓶颈 |

## 效果判定方法

定量效果不使用主观描述，统一采用以下验收指标：

- Trace 完整率：抽样执行中，同一 Trace ID同时包含 Agent、Bridge和结果HTTP Span的比例，目标100%；
- Artifact 关联率：`tool_result.artifact_id` 非空且能命中 Artifact 的比例，目标100%；
- 日志幂等率：同一batch重复提交后行数不增长，目标100%；
- 输出可解释率：每次执行均有字节数、hash、截断标志、退出码、耗时和错误类型，目标100%；
- 敏感信息泄漏：日志搜索 password/private key/token原值，目标0；
- 采集健康：Alloy Ready、Loki可查询、Promtail资源不存在；
- Agent工具可靠性：按工具持续观察成功率、P95延迟和error_type分布，作为后续Prompt/工具/SOP调优基线。

dev 实测中，真实 ReAct 失败样本已同时进入 Langfuse、Artifact、Tool Audit、Loki 和
`agent_tool_error_total{tool_name="bash_exec",error_type="nonzero_exit"}`。同一 Trace 内连续失败命令也能被量化，
后续可据此优化同类失败去重、节点能力探测缓存和工具尝试预算，而不再依赖人工阅读终端回显猜测模型行为。

真实 dev 验收数据见 `docs/verify/events/2026-07-27-terminal-bridge端到端验收报告.md`。在该报告未给出通过结论前，本报告只代表设计与实现差异，不代表生产就绪。
