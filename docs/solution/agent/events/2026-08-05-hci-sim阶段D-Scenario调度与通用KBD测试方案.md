---
status: proposed
category: solution
audience: architect, developer, tester, operator, security
last_updated: 2026-08-05
owner: team
---

# hci-sim 阶段 D：Scenario 调度与通用 KBD 测试方案

## 背景与需求

阶段 C 解决“Fixture 是否可信且可发布”，但 published Bundle 仍不能自行完成一次 Agent E2E。阶段 D 引入 TestRun、Scheduler、短时 Lease、Bundle 分发和通用 Headless Runner，使用户能按 `support_id` 创建一次可审计、可取消、无真实环境回退的测试。

需求见[阶段 D 需求](../../../requirement/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试需求.md)。

## 方案（WHAT）

### 1. 端到端架构

```text
User/CI
  → TestRun API → Registry resolve + policy
  → Scheduler → Runtime placement + Bundle pin
  → Lease Issuer → short-lived capability
  → Headless Runner → Customer UI/Terminal Bridge protocol
  → Agent/CDD → Bridge → hci-sim SSH Runtime
  → Artifact/Evaluation/Conclusion
  → Oracle Evaluator → immutable Run Report
```

Runner 必须经过真实 Customer UI/Terminal Bridge command/exec-result 协议；不得因“只是测试”直接调用 Runtime API。控制面只把 Lease 和目标信息交给授权 Runner，不把 Registry 写权限或长期密钥下发给浏览器。

### 2. TestRun API

创建请求：

```json
POST /api/v1/agent-test-runs
Idempotency-Key: ci-27123-r24-positive-realistic-001
{
  "support_id": "27123",
  "revision": "active",
  "variant": "positive-realistic",
  "execution_mode": "sim-ssh",
  "runner": "headless",
  "deadline_seconds": 900,
  "labels": {"trigger": "pr", "change_id": "..."}
}
```

服务端原子解析为不可变 `kbd_revision + scenario_id + bundle_digest`。响应返回 `run_id`、状态、解析结果和 trace，不返回 Lease secret。`active` 只在创建时解析一次；运行中不得跟随指针变化。

Registry 返回 missing、draft、unpublished、stale、retired、签名无效或 Runtime schema 不兼容时，创建以结构化 4xx 拒绝且不产生可执行 Run。授权、配额或环境策略失败同样 fail closed。

### 3. 状态机、幂等和 Attempt

```text
requested → preparing → leased → running
    │           │          │         ├→ passed
    │           │          │         ├→ failed
    │           │          │         ├→ inconclusive
    └───────────┴──────────┴─────────┼→ cancelled
                                     └→ expired
```

- 所有状态转换在 PostgreSQL 通过事务/CAS 校验合法前态和 `version`。
- 同一 actor + idempotency key + request digest 返回同一 Run；相同 key 不同 body 返回冲突。
- `passed/failed/inconclusive/cancelled/expired` 为终态；取消是幂等操作。
- 重试创建同一逻辑 Run 下的新 `attempt_no`，保留旧 Lease、Artifact、日志和首次失败；不得覆盖历史证据。
- outbox 发送调度事件，consumer 以 run/attempt 幂等，避免数据库提交与消息投递双写不一致。

### 4. 数据模型

| 表 | 关键字段 | 说明 |
|---|---|---|
| `agent_test_run` | `id, support_id, kbd_revision, scenario_id, bundle_digest, variant, mode, status, version, deadline, idempotency_key` | 逻辑 Run。 |
| `agent_test_run_attempt` | `run_id, attempt_no, runtime_id, lease_id, status, started_at, ended_at, failure_type` | 物理尝试。 |
| `agent_test_run_event` | `run_id, attempt_no, seq, type, payload_digest, trace_id, created_at` | 追加式状态和审计事件。 |
| `agent_test_run_result` | `run_id, attempt_no, oracle_version, outcome, report_uri, report_digest` | 不可变机器报告。 |
| `agent_test_runtime_instance` | `id, shard, schema_versions, capacity, heartbeat, status` | Runtime 能力和放置。 |

Lease secret 只保存 hash/jti 和状态，不保存可重放明文。Artifact/Evaluation/Conclusion 使用现有业务表时增加 run/scenario/attempt/bundle 关联，不复制业务事实。

### 5. Scheduler 和放置

Scheduler 的输入是 Run immutable snapshot 和 Runtime capability snapshot。放置约束：环境、`sim-ssh` mode、Manifest schema、所需 fault/stream 能力、容量、shard 和数据驻留。

- stateless Scenario：可放任意 compatible replica；
- stateful Scenario：固定一致性 shard，overlay 以 `run_id + scenario_id` 隔离，更新用 CAS；
- 先 reserve 容量，再 pin Bundle，最后签发 Lease；任一步失败执行补偿清理；
- queue 有界，超载明确 `capacity_unavailable`，不隐式等待超过 Run deadline；
- Runtime/Registry 不可用时 fail closed，禁止切换到 `real-ssh`。

### 6. Lease Issuer

Issuer 使用阶段 B Claims v2，绑定：

```text
run_id + attempt_no + scenario_id + support_id + kbd_revision
+ bundle_digest + variant + virtual_node/container
+ target service identity + execution_mode + quotas + deadline
```

Lease TTL 短于 Run deadline；续期只由 Scheduler 对仍为 running 的 Run 发起，并重新检查 Bundle 状态和 Runtime。取消、终态、超时、Bundle 紧急撤销时写入 revocation store。Runtime 在握手和每命令复验。

### 7. Bundle 分发、缓存和 Overlay

Runtime 通过 workload identity 从 Registry 获取服务端生成的 digest URL，下载后校验大小、schema、signature 和 digest。缓存采用 LRU + 总字节上限：

- 调度前 `pin(bundle_digest, run/attempt)`；
- active pin 不可淘汰；
- Run 终态 `release`；
- Pod 重启后从 Run/Lease reconciliation 恢复 pin；
- 校验失败隔离对象并拒绝 Run，不回退旧 revision。

Bundle 本体不可修改。需要命令序列状态时使用按 Run/Scenario 隔离的有界 overlay，设置 TTL、版本和最大字节数；终态或 orphan reconciliation 负责删除。

### 8. 通用 Headless Runner

Runner 复用真实前端/Bridge 客户端协议并实现受支持的无头认证，而不是伪造数据库结果：

1. 创建/绑定测试工单和会话；
2. 提交 execution mode、权威目标和一次性 Lease 引用；
3. 触发 Agent/CDD 分析；
4. 消费 command 请求，经真实 Bridge SSH 执行并回传 exec-result；
5. 收集 Artifact、Evaluation、Conclusion 以及 Gate；
6. 遵守 Run context/deadline，支持 cancel；
7. 产出逐 Signal 机器报告和可阅读摘要。

协议适配器必须与 Customer UI 使用同一 contract test；如果 WebSocket/UI 是唯一真实入口，Headless Runner 使用相同公开协议，不调用内部“测试捷径”。

### 9. Oracle 和结果判定

结果模型至少保留以下层次，禁止压扁为布尔值：

```text
transport outcome
→ command route/fault outcome
→ Artifact integrity
→ variable extraction
→ Matcher outcome
→ Signal outcome
→ Conclusion Gate/outcome
→ TestRun outcome
```

推荐终态规则：

- `passed`：所有必需观察、Signal 和 Conclusion 与 Oracle 一致；
- `failed`：系统完成执行但 expected/actual 存在确定性业务差异；
- `inconclusive`：capability gap、timeout、permission、UNKNOWN、证据不足或基础设施错误导致无法判断；
- `cancelled/expired`：由用户取消或 deadline 引起，不伪装为业务失败。

报告包含代码 SHA、镜像 digest、KBD/Bundle/Tool/Policy revision、run/attempt、route hit/miss、变量、Artifact hash、Matcher、Conclusion、错误分类和 trace links。

### 10. Batch/CI 接口

Batch API 接受 support ID 列表、标签分类或影响集，服务端展开为单 Run；每个 Run 仍独立执行所有安全和发布门禁。Batch 定义最大并发、fail-fast/continue 策略、总 deadline 和预算，汇总时分别统计 passed/failed/inconclusive/cancelled。

阶段 D 的生产门禁仅声明 1 和 10 Scenario 并发。100+ 容量、100 Agent 和浏览器负载属于阶段 E，不可由 batch API 的存在推断。

### 11. 取消、超时和 Orphan Recovery

取消流程：CAS 标记 cancelling/终态意图、撤销 Lease、取消 Runner context、关闭 SSH、release cache pin/overlay/reservation、写入终态报告。每步幂等，失败进入 reconciliation。

后台 reconciliation 比对 DB Run、Scheduler reservation、Runtime pin/overlay、Lease 和 Runner heartbeat：超过 grace period 的 preparing/running Run 标为 expired/inconclusive，撤销能力并清理孤儿。清理证据记录资源前后计数，不以“请求已返回”作为清理完成。

### 12. 安全与环境隔离

- prod 默认不部署 sim Runtime，Issuer 不能为 prod audience 签发 sim Lease；
- sim workload 无真实 HCI 凭据、DNS/路由和 egress；
- `execution_mode` 创建后不可变，Bridge 结果标记来自认证上下文；
- 用户只能选择 support ID/variant 等逻辑字段，不能提交任意 host、Bundle URL 或命令；
- 日志只记录 hash/ID/错误类型，不记录 Lease、raw command secret 或客户原始输出。

## 决策依据（WHY：为什么选此方案，为什么不选其他方案）

### 为什么 TestRun 绑定 immutable snapshot

一次 Run 的结果必须能被重放和解释。若运行中跟随 active KBD 或 Bundle，状态和证据会同时指向多个事实版本，审计失效。

### 为什么 Runner 必须走真实 Bridge 协议

直接调用 Runtime 只能证明 Fixture 命中，无法覆盖 Agent 生成命令、Bridge 策略、exec-result、Artifact 和 Conclusion 链路。通用 KBD E2E 必须验证产品真实边界。

### 为什么使用共享 Runtime + digest 缓存

每 Run 独立 Pod 隔离强但启动和资源成本高；共享 stateless Runtime 配合强 Lease、精确 RouteKey、不可变 Bundle 和 pin 可先满足 1/10 并发。需要状态的场景通过显式 shard/overlay 隔离，而不是在进程全局变量中隐式共享。

### 为什么不允许 sim→real fallback

Fixture miss 或控制面故障时访问真实 HCI 会把安全边界和测试结论同时破坏。可用性不足必须显式失败，不能用扩大权限补偿。

### 为什么重试创建 Attempt 而不是覆盖 Run

覆盖会隐藏 flaky 和首次失败，破坏阶段 E 的稳定性指标。Attempt 模型保留逻辑请求与物理执行的双层事实。

## 影响范围（哪些现行全量文档需要更新）

- `requirement/需求说明.md`：登记按 support ID 创建 TestRun 的用户能力。
- `solution/架构设计.md`、`solution/接口设计.md`：Run/Scheduler/Lease/Runner 协议和状态机。
- `solution/数据库设计.md`：Run/Attempt/Event/Result desired schema。
- `solution/安全设计.md`：身份、environment/mode、网络和 secret 边界。
- `solution/可观测性设计.md`：跨 API/Scheduler/Runner/Bridge/Runtime trace。
- `deploy/部署设计.md`：Runtime、Runner、queue、cache 和 reconciliation。
- `verify/测试指南.md`、`task/架构任务.md`：阶段 D 门禁。

## 验收标准

- `support_id=27123, revision=active` 可创建 immutable Run 并走真实 Bridge/Agent 链路完成；
- 至少 3 个不同 published Bundle 复用同一 API、Scheduler 和 Runner；
- 状态机、idempotency、Attempt、cancel、expiry 和 orphan cleanup 可验证；
- 所有 Lease 精确绑定 Run/Scenario/Bundle/target/mode/quota；
- missing/stale/unpublished/incompatible Bundle 无副作用地拒绝；
- sim miss 和基础设施故障永不访问真实 HCI；
- 报告能关联 Signal、Artifact、Matcher、Conclusion 和所有版本 digest；
- 1/10 并发无串线、状态覆盖、资源泄漏或隐式重试；
- 阶段 E 可在该通用接口上增加差分、稳定性和容量验证，无需再建专用脚本。

## 关联任务与验证

- [阶段 D 任务](../../../task/agent/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试任务.md)
- [阶段 D 验证](../../../verify/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试验证方案.md)
