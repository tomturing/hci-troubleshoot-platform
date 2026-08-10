---
status: in_progress
category: verify
audience: developer, tester, operator, security
last_updated: 2026-08-10
owner: team
---

# hci-sim 阶段 D：Scenario 调度与通用 KBD 测试验证方案

> **证据状态：pending。** 本文是 TestRun/Scheduler/Runner 的验收计划，不是已执行报告。当前相关表与服务尚不存在；所有用例必须在阶段 C 通过后执行。

关联[需求](../../requirement/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试需求.md)、[方案](../../solution/agent/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试方案.md)和[任务](../../task/agent/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试任务.md)。

## 验证目标

证明用户可按 support ID 通过统一 API 创建 immutable TestRun，经 Scheduler、短时 Lease、Headless Runner、真实 Bridge/Agent 协议完成全链路，并获得不压平错误状态的报告；取消、重试、故障和 1/10 并发下无串线、泄漏或真实 HCI 回退。

## 样本与环境

- 样本：KBD 27123 active revision + 阶段 C 另外两个异构 published Bundle；每个执行适用 variants。
- 控制面：API、PostgreSQL、queue/outbox、Scheduler、Issuer、Registry。
- 数据面：至少 2 个 Runtime replica；stateful 用例另有 shard/overlay。
- 产品链路：Headless Runner → Customer UI/Terminal Bridge 同协议 → Agent/CDD → Runtime。
- 隔离：无真实 HCI route/credential 的 dev namespace；单独授权环境只做 real regression，不做 fallback。

记录所有 image/bundle/KBD revision、feature flags、资源 limit、网络策略和 test seed。

## 用例矩阵

| ID | 前置/步骤 | 期望 | 必需证据 |
|---|---|---|---|
| VD-01 | 用 27123 `active` 创建 Run，随后切换 active | Run 保存创建时精确 revision/bundle，不随指针漂移 | request/DB/report |
| VD-02 | 同 idempotency key/body 并发创建；再用不同 body | 前者同一 Run，后者 409；无重复副作用 | API/DB counts |
| VD-03 | 遍历状态机和非法/并发转换 | CAS 只允许合法单一转换，终态不可复活 | event/version log |
| VD-04 | Retry failed/inconclusive Run | 新 attempt_no，旧证据和首次结果保持 | attempts/report |
| VD-05 | 创建 missing/draft/unpublished/stale/retired/incompatible Bundle | 结构化拒绝，无 reservation/Lease/Runner | side-effect audit |
| VD-06 | 容量不足、Runtime 不兼容、pin 失败、Issuer 失败 | 明确失败并补偿已分配资源，不切 real | Scheduler trace |
| VD-07 | 篡改 Lease run/attempt/scenario/bundle/variant/node/container/target/mode | 每一项拒绝，不能跨 Run 使用 | auth matrix |
| VD-08 | 重放/过期/revoke Lease，经长连接再发命令 | 每命令拒绝，Run 状态和错误可解释 | SSH/trace/Store |
| VD-09 | 冷/热 cache、pin 后制造淘汰压力、篡改对象 | active pin 不淘汰；篡改拒绝；release 后可淘汰 | cache metrics/log |
| VD-10 | Runtime 重启并 reconciliation pin/Lease | 恢复或显式 inconclusive，孤儿资源最终清理 | before/after state |
| VD-11 | 两个 stateful Scenario 并发更新 overlay | CAS 正确，按 run/scenario 隔离，无状态串线 | overlay history |
| VD-12 | 对照 Headless Runner 与 Customer UI/Bridge contract | message、auth、command/exec-result 语义一致，无内部捷径 | contract capture |
| VD-13 | 执行 positive/negative/near-miss/timeout/permission/unknown | transport/Artifact/Matcher/Signal/Conclusion/Run 状态符合 Oracle | layered report |
| VD-14 | 注入 fixture miss、UNKNOWN、ERROR、BLOCKED | 不归约为 false/PASS；结果为 failed 或 inconclusive 的规定类型 | report/assertions |
| VD-15 | 在 preparing/leased/running/stream 各阶段 cancel | 幂等终止，Lease/connection/pin/overlay/reservation 回收 | cleanup deltas |
| VD-16 | 让 Runner/Runtime/Scheduler 崩溃和 outbox 重复/迟到 | reconciliation 收敛；事件/exec-result 不串 attempt | event/trace |
| VD-17 | 用错误 DNS/Registry 故障/fixture miss 并监控 egress | 无真实 HCI 连接尝试，明确失败 | network flow logs |
| VD-18 | Artifact/Evaluation/Conclusion 逐项反查 | 可由 run/scenario/attempt/bundle/route/hash 关联，证据不断链 | query/report |
| VD-19 | 3 个 KBD 经同一 API/Runner 执行 | 无 KBD 专用脚本/分支；各自 Oracle 正确 | source scan/runs |
| VD-20 | 10 个混合 variants 并发，多副本放置 | 零 route/变量/Artifact/Conclusion 串线，无状态覆盖 | canary matrix |
| VD-21 | 10 并发中注入 timeout/cancel/output overflow | 其他 Run 正常，队列/资源回落基线 | metrics/profiles |
| VD-22 | Batch 按列表/影响集、fail-fast/continue 执行 | 单 Run 门禁不被绕过，汇总区分所有终态 | batch report |

## 隔离判定方法

每个 Run 注入不可冲突的 canary：`run_id` 派生的虚拟节点、route 输出 token、变量值和 expected Conclusion。测试结束在 Runtime trace、Artifact 内容 hash、变量池、Evaluation 和 Conclusion 五层做全连接检查；任一 token 出现在其他 Run 即判 P0 串线，不能用最终结论恰好相同解释。

## 对抗性审查

- 客户端提交任意 URL、host、raw command、execution_mode 改写和超大 labels，必须在 API/Policy 层拒绝。
- 同时取消与完成、Retry 与过期、Runtime 心跳抖动与重复 outbox，验证 CAS 和幂等。
- 使用已经 terminal 的 Lease、其他 attempt 的 exec-result、迟到事件攻击新 attempt。
- 模拟 browser/Runner 泄露 Lease，验证 TTL、audience、target 和网络边界限制影响。
- 禁用 Registry/Runtime/Redis 后观察，任何自动连接 real target 都直接 FAIL。

## 证据模板

```yaml
phase: D
verdict: pending|pass|fail|blocked
control_plane_sha: ""
runner_image: ""
bridge_image: ""
runtime_image: ""
samples: []
concurrency: [1, 10]
cases:
  VD-01: {result: pending, run_ids: [], trace_ids: [], evidence: []}
resource_cleanup: {leases: null, pins: null, overlays: null, connections: null}
network_evidence: []
```

## 退出标准

- VD-01～VD-22 全部 PASS；3 个 KBD 走同一产品协议，无专用绕过。
- 1/10 并发五层 canary 零串线，所有资源在批准窗口回落。
- stale/非法 Lease/任意输入/真实 fallback 零绕过。
- Retry/Attempt、取消/过期、inconclusive 和报告语义符合版本化契约。
- 产品、QA、Runtime/Bridge、Security、SRE 签署阶段 E Go。

## 当前状态（2026-08-10）

27123 最小 Gateway/TestRun/Bridge smoke 为 `passed`；持久化、幂等/CAS、重启恢复、并发和 23821 full fixture 为 `pending/capability_gap`，阶段 D 未签署。

## 失败分类与文档更新

分类：`api_contract`、`idempotency_cas`、`bundle_gate`、`placement_compensation`、`lease_binding`、`cache_integrity`、`overlay_isolation`、`runner_bypass`、`oracle_mismatch`、`state_flattening`、`cleanup_leak`、`event_cross_attempt`、`real_fallback`、`report_correlation`、`batch_bypass`。完成后同步接口、数据库、安全、部署、可观测性、架构和测试指南；阶段 E 未完成前不得把 1/10 结果外推为 100+。
