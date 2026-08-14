# hci-sim 架构评估与构建机制分析（现行全量）

> 本文从「为什么目录和表众多」的角度，解释 hci-sim 的构建逻辑。所有事实基于 `hci_sim/` 源码、`database/hci-sim-migrations/` 迁移与 `docs/solution/hci-sim`、`docs/task/hci-sim` 事件文档。
> 版本：2026-08-14。

## 1. 它解决的根本问题

hci-sim 不是测试环境，而是一个**被管制的仿真内核**：强隔离、确定性、带租约鉴权，用于在不可触碰真实生产客户环境的前提下，对 KBD 修复命令做端到端验证。它把「证明命令能跑通」与「不碰真实客户风险」两件事同时解决。

## 2. 四个正交关注点（为什么拆出众多目录与表）

目录/表众多是因为把四个关注点正交拆开，而非冗余：

| 关注点 | 代码落点 | 数据库 schema |
|---|---|---|
| 数据面执行内核 | `internal/server`、`internal/fixture`、`internal/lease` | （运行时内存 + 对象存储，原始不落 PG） |
| 控制面治理 | `internal/controlplane` | `control_plane`、`fixture`、`artifact`、`audit` |
| 可观测与可审计 | `internal/metrics`、`internal/telemetry` | `audit.entity_event`（全表带 `trace_id`） |
| 准入与供应链安全 | `internal/controlplane` + Artifact Gate | `artifact`（scan/approval） |

## 3. 代码模块职责

- `cmd/hci-sim`：主服务（`serve`/`lease`/`manifest-digest`/`bootstrap`）。
- `cmd/hci-sim-smoke`：经真实 Bridge 链路冒烟，输出 `support_id/test_run_id/exit_code`。
- `internal/controlplane`：Bundle 生命周期状态机、双角色审批、TestRun、Run Lease、差分/mutation/稳定性/容量证据。
- `internal/database`：`RunRepository`，scenario/run/run_attempt/run_event/run_result/run_outbox 的 CRUD、CAS 幂等、outbox claim/complete。
- `internal/fixture`：已发布 Manifest v2 精确 RouteKey 路由、Fault 模型。
- `internal/lease`：htp2 HMAC 租约签发与每命令复验。
- `internal/server`：SSH+HTTP 服务面、有界 worker 队列、fail-closed 路由。
- `internal/reconciler`：durable outbox 投递循环。

## 4. 数据库：独立库 `hci_sim`，16 张表 / 4 schema

| schema | 表 | 存什么 |
|---|---|---|
| `control_plane` | `scenario` `run` `run_attempt` `run_event` `run_result` `run_outbox` `runtime_instance` | 场景、TestRun、尝试、事件流（带 trace_id）、Oracle 结果、事件出队、运行时实例 |
| `fixture` | `bundle` `dependency` `provenance` `approval` `stale_outbox` | 不可变 Bundle metadata、依赖、来源、双角色审批、失效事件 |
| `artifact` | `metadata` `scan` `approval` | 制品元数据/digest、secret/PII/license/schema 四重扫描、expert+security 双角色审批 |
| `audit` | `entity_event` | 实体状态变更前后快照，trace_id 调用链 |

**关键边界**：跨库只存不可变 ID（support_id/revision/checksum/digest），不建跨库外键；原始 Artifact 与 Lease 明文不进 PG；所有表带 `trace_id`。

## 5. 部署与 CI

- Helm：`replicaCount=1`（内存配额 Tracker 单副本），NetworkPolicy 仅允许 postgres/conversation-service 访问 5432。
- CI：`hci-sim-go.yml`（gofmt/test/race/vet/build + manifest/helm 一致性）、`hci-sim-db-migration-test.yml`（隔离 PG 校验 16 表 + DDL 负向检查）。

## 6. 现行状态

- 代码级基础就绪；KBD 27123 revision 25 纵向样板 PASS。
- 面向终端用户生产交付 **BLOCKED**：需 approved Artifact、真实校准、规模验证、主库旧表 contract/drop。
- 验收入口已迁移至 **admin-ui `SimulationConversation.vue`**（受管 terminal_bridge），旧 Customer UI 仿真租约表单已废弃。

## 7. 用户视角（不需要懂上面，只需懂这四步）

1. 喂哪个 KBD（起点）；
2. Lease 由 admin-ui 自动填充（关注 TTL 与本地来源文件）；
3. 在 admin-ui 仿真会话面板验收（命令卡片 passed/failed）；
4. 看会话级 `agentOutcome` 与命令 `output` 判断过没过。

其余（执行内核、状态机、16 表、迁移、Helm）均为平台黑盒。
