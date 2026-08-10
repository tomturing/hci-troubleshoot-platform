---
status: approved
category: requirement
audience: product, architect, developer, tester, operator
last_updated: 2026-08-10
owner: team
---

# hci-sim 阶段 D：Scenario 调度与通用 KBD 测试需求

## 1. 背景

阶段 C 能发布可信 Bundle，但仍缺少按 `support_id` 创建测试、签发 Lease、选择 Runtime、驱动真实 Bridge/Agent 并收集结果的通用执行闭环。阶段 D 把单个 Bundle 变成用户可调用、可审计、可清理的 TestRun。

## 2. 用户故事

| 用户 | 用户故事 |
|---|---|
| 测试工程师 | 我希望通过 support ID 和 variant 创建一次完整 Agent E2E，不再手写 ConfigMap 和 smoke 命令。 |
| KBD 专家 | 我希望从 KBD 管理入口启动测试并看到每个 Signal、Artifact 和结论。 |
| CI | 我希望批量运行受影响 KBD，并在 fixture miss、stale、串线或误判时阻断。 |
| 运维人员 | 我希望每个 Run 有 deadline、配额、清理和明确状态。 |

## 3. 功能需求

### D-FR-01 TestRun API

创建请求至少包含：

```json
{
  "support_id": "27123",
  "revision": "active",
  "variant": "positive-realistic",
  "execution_mode": "sim-ssh",
  "runner": "headless",
  "idempotency_key": "..."
}
```

控制面解析 published Bundle；不存在、stale、未批准或不兼容时拒绝创建。

### D-FR-02 状态机和幂等

TestRun 状态：

```text
requested → preparing → leased → running
→ passed/failed/inconclusive/cancelled/expired
```

- 同一 idempotency key 不得创建重复 Run。
- 状态转换必须 CAS/事务化。
- 重试必须创建明确 attempt，不得覆盖历史物理执行证据。
- Run 完成后普通“继续”不得隐式重采集。

### D-FR-03 Scenario Scheduler

Scheduler 负责：

- 选择 compatible Runtime/shard；
- 校验容量和 Bundle 可用性；
- 签发短时 Lease；
- 绑定 target Service、scenario、run、variant、node/container 和 quota；
- deadline、续期、撤销、取消；
- orphan lease 和状态自动清理；
- 不可用时明确失败，永不 fallback real。

### D-FR-04 Runtime Bundle 分发

- 共享 Runtime 按 bundle digest 加载只读 Bundle。
- 缓存必须有 LRU、总字节上限、checksum 和 pin/release。
- 客户端不能指定任意 URL；Runtime 只从受信 Registry 获取。
- stateless scenario 可任意副本处理；stateful scenario 必须固定 shard 并使用 CAS overlay。

### D-FR-05 通用 Headless Runner

Runner 必须复用真实 Customer UI/Terminal Bridge command/exec-result 协议，不能直接调用 Runtime 绕过链路。

Runner 负责：

- 创建或绑定测试工单/会话；
- 提交环境和 Lease；
- 驱动 Agent/CDD；
- 中继真实命令执行；
- 收集 Artifact/Evaluation/Conclusion；
- 遵循 Run deadline；
- 生成机器可读报告。

### D-FR-06 Oracle 和结果模型

报告至少包含：

- support ID/revision/bundle/variant；
- run/scenario/attempt；
- 计划 Signal 与实际执行；
- fixture hit/miss；
- producer/consumer 变量；
- Matcher outcome；
- Artifact ID/hash；
- Conclusion Gate；
- expected/actual diff；
- capability gap 和错误分类。

执行成功不等于业务 PASS；`ERROR/BLOCKED/INCONCLUSIVE` 不得归约为 False 或 PASS。

### D-FR-07 批量和 CI 接口

- 支持按 support ID 列表、分类、变更影响集创建 batch。
- batch 具有并发上限、失败策略和汇总报告。
- 初期只开放小规模并发；100+ 声明留给阶段 E。

### D-FR-08 权限和环境隔离

- 只有授权测试身份可以创建 sim Run。
- `execution_mode` 创建后不可切换。
- prod 默认不部署 hci-sim，也不得接收 sim Lease。
- sim Runtime 无真实 HCI 凭据和网络路由。

## 4. 非功能需求

| 编号 | 要求 |
|---|---|
| D-NFR-01 | API、Scheduler、Runner 和 Runtime 全链路 trace 可关联。 |
| D-NFR-02 | Run 取消或超时后，Lease、连接、缓存 pin 和 overlay 可回收。 |
| D-NFR-03 | 一个 Scenario timeout/miss 不影响其他 Scenario。 |
| D-NFR-04 | 所有状态变更和人工操作可审计。 |
| D-NFR-05 | Batch 不得绕过单 Run 的安全、配额和发布门禁。 |

## 5. 范围外

- 不在阶段 D 宣称 100/200 并发通过。
- 不自动使用真实 HCI 做 fallback。
- 不在浏览器保存长期 Lease 或 Runtime 凭据。
- 不把“能创建 Run”视为“所有 KBD 都可编译”。

## 6. 验收标准

- [ ] `support_id=27123, revision=active` 可通过 API 创建并完成 sim TestRun。
- [ ] 至少 3 个 published Bundle 可通过同一 API/Runner 执行。
- [ ] positive/negative/near-miss/timeout/permission/unknown 有明确状态和报告。
- [ ] stale/unpublished/missing Bundle 创建失败且无副作用。
- [ ] Run 重试、取消、过期和幂等符合状态机。
- [ ] sim miss 永不连接真实 HCI。
- [ ] Artifact/Evaluation/Conclusion 可通过 run/scenario/fixture digest 互查。
- [ ] 1/10 场景并发下无串线、资源泄漏和状态覆盖。

## 7. 关联文档

- [阶段 D 设计](../../solution/agent/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试方案.md)
- [阶段 D 任务](../../task/agent/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试任务.md)
- [阶段 D 验证](../../verify/events/2026-08-05-hci-sim阶段D-Scenario调度与通用KBD测试验证方案.md)

## 当前状态（2026-08-10）

阶段 D 的最小 build/TestRun HTTP 契约和 Runtime 内存 RunStore 已有代码；PostgreSQL Repository/CAS、Runner/Agent context 持久化、并发与重启恢复尚未完成，不能将单条 smoke 外推为通用 KBD 测试能力。
