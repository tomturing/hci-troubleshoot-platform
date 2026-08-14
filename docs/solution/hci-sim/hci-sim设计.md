# hci-sim 仿真运行时 · 全量设计

> 本文为 hci-sim 子模块的现行全量设计说明，事件化阶段设计见同目录 `events/`。
> 适用范围：强隔离、确定性、带租约鉴权的 HCI 仿真 SSH Runtime，用于在不可触碰真实生产客户环境的前提下，对 KBD 修复命令做端到端验证。

## 1. 第一性原理定位

- **本质**：被管制的仿真内核，只接受带 HMAC 签名租约的 SSH 连接，命令必须精确匹配已发布 Fixture Bundle，否则 fail-closed 拒绝。
- **为什么需要**：真实 KBD 修复命令直接在生产跑风险极高，但又要证明命令在真实节点上能跑通、exit_code 正确。解法不是测试环境，而是仿真 Runtime。

## 2. 代码层

源码唯一正式目录：`hci_troubleshoot-platform/hci_sim/`（`internal/` 核心，`cmd/` 入口）。

### 程序入口

| 程序 | 作用 |
|---|---|
| `hci-sim` | 主服务：子命令 `serve`（SSH:2222 + HTTP:18080 + readyz）、`lease`（签发租约）、`manifest-digest`（bundle 摘要）、`bootstrap`（控制面初始化） |
| `hci-sim-smoke` | 冒烟测试：经真实 Terminal Bridge WebSocket→SSH 执行 `recommended_command`，输出 support_id/test_run_id/exit_code 摘要 |

### 核心包

| 模块 | 职责 |
|---|---|
| `controlplane/` | Bundle 生命周期状态机、双角色审批、TestRun、Run Lease、差分/mutation/稳定性/容量证据 |
| `database/` | PostgreSQL 访问层（RunRepository）：scenario/run/run_attempt/run_event/run_result/run_outbox |
| `fixture/` | 加载已发布 Manifest v2，按 RouteKey 确定性路由，Fault 模型 |
| `lease/` | htp2 Capability 租约签发、每命令复验、单副本配额 |
| `server/` | SSH + HTTP 服务面，exec/shell 有界 worker 队列、fail-closed 路由 |
| `reconciler/` | Runtime 侧 durable outbox 投递循环 |
| `metrics/` / `telemetry/` | OpenTelemetry 指标与 trace 导出 |

## 3. 数据库层（独立库 `hci_sim`，16 张表 / 4 schema）

- `control_plane`：scenario / run / run_attempt / run_event / run_result / run_outbox / runtime_instance
- `fixture`：bundle / dependency / provenance / approval / stale_outbox
- `artifact`：metadata / scan / approval（安全扫描，禁止存客户原始字节/URL/命令输出）
- `audit`：entity_event（调用链 trace_id）

**关键约束**：跨库只存 support_id/KBD revision/checksum/digest，不建跨库外键；原始 Artifact、Lease 明文不进 PG；状态/枚举全用 CHECK 约束；所有表带 trace_id。

## 4. 部署与脚本

- Helm（`deploy/helm/hci-sim/`）：deployment/service/networkpolicy（仅允许 postgres、conversation-service 访问 5432）/pdb/resourcequota；`replicaCount` 必须为 1（内存配额 Tracker 单副本）。
- 脚本（`scripts/hci-sim/`）：`two-step-acceptance.sh`（C3 人工验收）、`capability-matrix.sh`、`stage-e-matrix.sh`、`diagnosis-lab.py`。

## 5. CI 门禁

- `hci-sim-go.yml`：gofmt/test/race/vet/build + manifest-and-helm（digest 一致性、拒绝退役 marker、NetworkPolicy 必填）。
- `hci-sim-db-migration-test.yml`：隔离 PG 跑迁移校验 16 表，反向校验主库无 agent_test_*，DDL 负向检查。

## 6. 用户视角（与平台机制边界）

用户关注：喂哪个 KBD、Lease 有效性窗口与来源文件、在 admin-ui 仿真会话面板验收、看命令卡片 passed/failed 与 output。
平台黑盒：server/fixture/lease 执行内核、controlplane 状态机、16 张表 CRUD、迁移与 CI、Helm 编排、trace_id/idempotency_key。

## 7. 当前状态与边界

- 仿真运行控制面（Runtime/Bridge/Manifest）代码级基础已实现；C1 对 dev 126 条 KBD 完成只读快照基线。
- **面向终端用户的生产交付仍为 BLOCKED**：无 approved Artifact / 真实校准环境前，只能声明代码级基础就绪，不能宣称任意 KBD 自动构建或产品级验证。
