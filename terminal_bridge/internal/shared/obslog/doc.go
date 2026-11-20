// Package obslog 是 terminal_bridge **自身**的结构化事件日志与投递通道。
//
// 它回答"桥自己做了什么、做坏了什么"，而不是"客户节点上有什么日志"：
//   - 业务域通过 Emit 上报结构化事件（会话生命周期、交付阶段、采集意图、上传通道状态）；
//   - 事件先落本地环形文件（hub），再经 WS 实时推给前端（type=bridge_log），
//     并直传 / outbox 补传 / stderr 补传到平台证据表（工单绑定、event_id 幂等）。
//
// 与相邻概念的分工：
//   - logcollect：采集**客户节点**上的业务日志，发往工单归档，不是本包；
//   - delivery：交付客户日志包，不是本包；
//   - shared/telemetry：trace 门面（span/propagation），本包只消费其 Traceparent。
//
// 两条不可破坏的约束：
//  1. 本包是证据通道，桌面产物不得随 build tag 剥离（CI 有 nm 符号门禁）；
//  2. 事件名 `log_upload.*`、前端 `type=bridge_log`、`/status` 字段是跨端契约，
//     不随代码改名而变化（详见 docs/plans/2026-11-20-terminal_bridge重构审查与优化.md §15）。
package obslog
