---
status: in_progress
category: task
audience: developer, tester, operator, security
last_updated: 2026-08-10
owner: team
---

# hci-sim 阶段 D：Scenario 调度与通用 KBD 测试任务

关联[阶段 D 需求](../../../requirement/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试需求.md)、[阶段 D 方案](../../../solution/agent/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试方案.md)和[阶段 D 验证](../../../verify/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试验证方案.md)。

> 当前事实：TestRun/Attempt schema、immutable Bundle pin、幂等请求、容量预留、Run Lease 与状态机参考内核已实施；真实 API、持久 Scheduler 与 Customer UI/Bridge Headless Runner 未接入，仍不能写为阶段 D E2E 完成。见[阶段 C–E 控制面代码级实施验证报告](../../../verify/events/2026-08-06-hci-sim阶段C-E控制面代码级实施验证报告.md)。

## 前置 Go/No-Go

- [ ] 阶段 C 至少 3 个异构 published Bundle 通过 Runtime 兼容验证。
- [ ] TestRun/Attempt/Result 状态与 Oracle 语义完成产品、架构和测试评审。
- [ ] Customer UI/Terminal Bridge 真实协议有可复用 contract test。
- [ ] 环境隔离、Runner 身份、Lease issuer 和 sim egress 策略完成 Security 评审。

## WBS

| ID | 任务 | 主要影响 | 产物/证据 | 依赖 |
|---|---|---|---|---|
| T-SIM-D-01 | 实现 Run/Attempt/Event/Result/Runtime desired schema 与 migration | DB | migration tests | C Go |
| T-SIM-D-02 | 实现 Create/Get/List/Cancel/Retry TestRun API、鉴权和幂等 | API | contract tests | 01 |
| T-SIM-D-03 | 实现事务/CAS 状态机、outbox 和终态规则 | control plane | concurrency tests | 01–02 |
| T-SIM-D-04 | 实现 Registry resolve、stale/compatibility/policy 门禁 | API/Registry | rejection tests | 02 |
| T-SIM-D-05 | 实现 Scheduler capacity reservation、placement 和补偿 | Scheduler | placement tests | 03–04 |
| T-SIM-D-06 | 实现绑定 Run/Scenario/Bundle/target/mode/quota 的 Lease Issuer | auth | token/revoke tests | 05 |
| T-SIM-D-07 | 实现 Runtime digest fetch、LRU、pin/release 和 state overlay | Runtime | cache/isolation tests | 05–06 |
| T-SIM-D-08 | 实现复用真实 UI/Bridge 协议的 Headless Runner | Runner/Bridge | protocol E2E | 02、06 |
| T-SIM-D-09 | 实现 Artifact/Evaluation/Conclusion 关联和 Oracle Evaluator | Agent/report | result tests | 08 |
| T-SIM-D-10 | 实现 cancel/deadline、orphan reconciliation 和确定性 cleanup | 全链路 | leak/recovery tests | 05–09 |
| T-SIM-D-11 | 实现 Batch/CI 接口、影响集输入和汇总报告 | API/CI | batch tests | 02–10 |
| T-SIM-D-12 | 完成 27123 + 2 KBD、variants、1/10 并发验收 | 全链路 | 阶段 D 报告 | 01–11 |

## 实现检查

- [x] 已发布 Bundle 在 Run 创建时固定为精确 revision/digest；生产 active Resolver 待接入。
- [x] 同 idempotency key/body 返回同 Run；不同 body 冲突。
- [ ] Retry 创建新 Attempt，不覆盖首次失败和 Artifact。
- [ ] stale/unpublished/missing/incompatible Bundle 创建无副作用。
- [ ] reserve→pin→Lease 任一步失败都补偿，reconciliation 可重入。
- [ ] stateful overlay 以 Run/Scenario 隔离、CAS 更新、有 TTL/字节上限。
- [ ] Runner 走真实 Bridge contract，无 Runtime/DB 捷径。
- [x] ERROR/UNKNOWN/BLOCKED/INCONCLUSIVE 不归约为 false/PASS。
- [ ] execution mode 不可变，所有失败路径禁止 real fallback。

## 测试任务

- [ ] API 鉴权、参数、幂等、冲突、并发创建和状态转换。
- [ ] stale/unpublished/missing/signature/schema 不兼容的拒绝与无副作用。
- [ ] Lease target/run/attempt/scenario/bundle/variant/mode 绑定和重放拒绝。
- [ ] Scheduler 容量、compatible replica、stateful shard、补偿与队列过载。
- [ ] Cache 冷/热、digest 篡改、pin 防淘汰、重启恢复和 overlay 串线。
- [ ] Runner 协议与真实 Customer UI/Bridge contract 一致。
- [ ] positive/negative/near-miss/timeout/permission/unknown 结果模型。
- [ ] cancel、expiry、Runner/Runtime 崩溃、orphan Lease/pin/overlay 清理。
- [ ] 1 和 10 并发 Run 的 route/Artifact/Conclusion 隔离和资源基线。
- [ ] sim miss/Registry 故障时网络证据证明未访问真实 HCI。

## 部署与上线顺序

1. 先部署 schema/API 只读查询与创建 feature flag；
2. 部署 Scheduler/Issuer/Runtime cache，feature flag 仅限测试身份；
3. 部署 Headless Runner 和 reconciliation；
4. 单 KBD、单 Run canary，再扩 3 KBD/10 并发；
5. 检查 trace、状态、清理、网络和证据后决定阶段 E Go。

feature flag 只能关闭创建能力，不能绕过 Bundle、Lease、mode 或网络门禁。

## 阻断条件

- 阶段 C 未 Go 或没有 3 个可用 Bundle；
- Runner 需要绕过真实 Bridge 协议才能工作；
- 状态机/幂等/CAS 无法证明并发安全；
- cancel/超时后 Lease、pin、overlay 或连接残留；
- sim 路径可能访问真实 HCI；
- 1/10 并发出现跨 Run 证据或状态污染。

## Definition of Done

- [ ] 指定 support ID 可通过统一 API 创建、执行、查询、取消和重试 TestRun。
- [ ] 27123 和至少 2 个异构 KBD 通过真实 Bridge/Agent 链路。
- [ ] variants、状态、Attempt、Oracle 和报告语义完整可追溯。
- [ ] 1/10 并发无串线、泄漏、覆盖或隐式重试。
- [ ] 无 sim→real fallback，安全/网络演练通过。
- [ ] migration、API、运行手册、阶段 D 验证报告获签署，阶段 E 获得明确 Go。

## 当前状态（2026-08-10）

27123 的最小 build/TestRun/Bridge smoke 已通过；Repository、Runner、Agent context 和并发退出条件未通过，任务保持 `in_progress`。
