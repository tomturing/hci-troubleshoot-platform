# hci_sim · 现行全量设计说明

> 版本：2026-08-14 全量修订（整合 `docs/solution/hci-sim`、`docs/task/hci-sim`、`docs/verify/hci-sim` 全部事件文档）
> 适用范围：`hci_sim/` 源码目录及其关联的控制面、数据库、部署与验收链路
> 详细阶段设计见 [`docs/solution/hci-sim/`](docs/solution/hci-sim/README.md)；任务见 [`docs/task/hci-sim/`](docs/task/hci-sim/README.md)；验证见 [`docs/verify/hci-sim/`](docs/verify/hci-sim/README.md)

---

## 1. 它是什么（第一性原理定位）

`hci_sim` 是 HCI 仿真 SSH Runtime 的**唯一正式源码目录**。产品、镜像、Helm release 名保持 `hci-sim`，但仓库不再保留第二套 `hci-sim/` Go Spike，避免实现和安全边界漂移。

**本质**：一个被管制的仿真内核——只接受带 HMAC 签名租约的 SSH 连接，命令必须精确匹配已发布的 Fixture Bundle，否则 fail-closed 拒绝。用于在**不可触碰真实生产客户环境**的前提下，对 KBD（知识库）修复命令做端到端验证。

**为什么需要**：真实 KBD 修复命令直接在生产跑风险极高，但又要证明「这条 KBD 给的命令在真实节点上确实能跑通、exit_code 正确」。解法不是测试环境，而是强隔离、确定性、带租约鉴权的仿真 Runtime。

---

## 2. 代码层

源码结构：`hci_sim/`（`internal/` 核心，`cmd/` 入口），无 `pkg/`。

### 2.1 程序入口（`cmd/`）

| 程序 | 文件 | 作用 |
|---|---|---|
| `hci-sim` | `cmd/hci-sim/main.go` + `bootstrap.go` + `offline_manifest.go` | 主服务。子命令：`serve`（默认，SSH:2222 + HTTP:18080 + `readyz`）、`lease`（签发 htp2 租约 token）、`manifest-digest`（写入/校验 bundle `sha256:` 摘要）、`bootstrap`（控制面初始化）。 |
| `hci-sim-smoke` | `cmd/hci-sim-smoke/main.go` | 冒烟测试。经真实 Terminal Bridge WebSocket→SSH 链路执行 `recommended_command`，只输出 `support_id/test_run_id/exit_code` 摘要。 |

### 2.2 核心包（`internal/`）

| 模块 | 职责 |
|---|---|
| `controlplane/` | 阶段 C–E 业务内核：Bundle 生命周期状态机（draft→validated→approved→published→stale/retired）、双角色审批、TestRun、Run Lease、差分/mutation/稳定性/容量证据模型。 |
| `database/` | PostgreSQL 访问层（`RunRepository`）：scenario/run/run_attempt/run_event/run_result/run_outbox 的 CRUD、CAS 幂等、过期、outbox claim/complete。 |
| `fixture/` | 加载已发布 Manifest v2，按精确 RouteKey 确定性路由；Fault 模型（none/nonzero_exit/permission/timeout/disconnect/truncate）。 |
| `lease/` | htp2 Capability 租约签发、每命令复验、单副本配额；HMAC 签名，Claims 绑定 Run/Bundle/Target。 |
| `server/` | SSH + HTTP 服务面；exec/交互 shell 共用有界 worker 队列、授权、fail-closed 路由、观测。 |
| `reconciler/` | Runtime 侧 durable outbox 投递循环；无下游 URL 时只恢复过期 processing，不标 processed。 |
| `metrics/` | OpenTelemetry 指标。 |
| `telemetry/` | OpenTelemetry trace 初始化/导出（OTLP HTTP）。 |

**依赖方向**：`controlplane` 依赖 `fixture`/`lease`；`reconciler`/`server` 依赖 `database`。

### 2.3 运行时安全基线（阶段 A/B）

- 仅加载带 `bundle.digest` 的已发布 Manifest v2；拒绝未知字段、摘要漂移、非规范 argv、歧义 RouteKey。
- 每个命令精确绑定 `variant + tool + acquisition_key + argv + virtual_node_id + container`，无正则/通配符/评分路由/默认 fixture。
- SSH 仅接受 `htp2` 租约；签名、issuer、audience、Bundle/KBD/工具策略版本、目标、时效及会话/命令/输出配额均受校验。
- `exec` 与交互 shell 共用有界 worker 队列、每命令授权、fail-closed 路由；原始命令与 token 不进日志。
- Terminal Bridge 仅通过完整 `auth_type=lease` + `execution_mode=sim-ssh` + `htp2.*` 标记模拟执行，绝不按 host 名猜测回退真实 HCI。

---

## 3. 数据库层（独立库 `hci_sim`，16 张表 / 4 schema）

迁移目录：`database/hci-sim-migrations/000001_control_plane.sql`（**独立库**，绝不被主库 Atlas Job 读取）。

### 3.1 四个领域 schema

| schema | 责任 | 表（共 16） |
|---|---|---|
| `control_plane` | 仿真运行控制面 | `scenario`、`run`、`run_attempt`、`run_event`、`run_result`、`run_outbox`、`runtime_instance` |
| `fixture` | 已编译夹具 Bundle 与审批 | `bundle`、`dependency`、`provenance`、`approval`、`stale_outbox` |
| `artifact` | 准入制品与安全扫描 | `metadata`、`scan`、`approval` |
| `audit` | 审计 | `entity_event` |

### 3.2 关键设计不变量

1. 跨库只存 `support_id` / KBD revision / checksum / digest，**不建跨库外键**；
2. 原始 Artifact、Lease 明文**不进 PostgreSQL**（对象存储保存不可变内容）；
3. 状态/枚举全用 `CHECK` 约束强制，运行时不能回退内存态；
4. 所有表带 `trace_id` 字段，满足调用链可追踪；
5. 应用只接受 `HCI_SIM_DATABASE_URL`（或等效拆分字段），**禁止 fallback 到 `DATABASE_URL`**；缺少配置即启动 fail-closed。

### 3.3 独立数据库落地状态（2026-08-11）

| 项 | 状态 |
|---|---|
| 独立 migration / 四领域 schema / migrator+runtime 角色 / Secret / `HCI_SIM_DATABASE_REQUIRED=true` | ✅ 已落地 |
| PostgreSQL Repository 取代内存控制面（idempotency/CAS、Event/Result/outbox 同事务） | ✅ 已落地 |
| 最终 Run `run-27123-...` 只出现在 `hci_sim`，revision 25/digest 与 Runtime 一致 | ✅ PASS |
| 权限负向（runtime 连主库失败、不能 DDL） | ✅ PASS |
| 主库 15 张空 `agent_test_*` 旧表 contract/drop 与备份恢复演练 | ⏳ BLOCKED（独立破坏性发布，仍是剩余项） |

> 详细门禁见 [`docs/verify/hci-sim/events/...独立数据库隔离验证.md`](docs/verify/hci-sim/events/2026-08-10-hci_sim独立数据库隔离验证.md)。

---

## 4. 部署与脚本

### 4.1 Helm（`deploy/helm/hci-sim/`）

- `deployment.yaml` / `service.yaml`：部署与 SSH/HTTP 服务暴露。
- `networkpolicy.yaml`：强制只允许来自 `postgres` / `conversation-service` 命名空间的 5432 访问（CI 校验空值直接失败）。
- `pdb.yaml` / `resourcequota.yaml`：可用性与资源配额。
- `files/*-fixture-manifest.json`：随附夹具清单，Helm 版必须与 `testdata` 字节一致（CI `cmp -s` 校验）。
- **`replicaCount` 必须为 `1`**：内存配额 Tracker 是单副本实现。

### 4.2 脚本（`scripts/hci-sim/`）

| 脚本 | 用途 |
|---|---|
| `two-step-acceptance.sh` | C3 两步人工验收（通用最小入口），生成 15 分钟 Lease，**不打印密码** |
| `capability-matrix.sh` / `capability-matrix-test.sh` | 能力矩阵生成与 CI 门禁校验 |
| `stage-e-matrix.sh` | 阶段 E 规模化验证矩阵 |
| `diagnosis-lab.py` | 诊断样例实验室（五篇 KBD 手测+自动回归统一入口） |

### 4.3 DB 工具

- `scripts/db/hci-sim-copy.sh`：库间拷贝（生产 autosync），默认 fail-closed 只生成 inventory，`HCI_SIM_ALLOW_COPY=1` 才复制，永不自动删源表。

---

## 5. CI/CD 门禁

| 工作流 | 作用 |
|---|---|
| `.github/workflows/hci-sim-go.yml` | Go 质量门禁：`gofmt`/`go test`/`go test -race`/`go vet`/`go build`；`manifest-and-helm` 校验已发布 bundle digest 一致、拒绝已退役的运行时 marker、Helm lint/template 并校验 NetworkPolicy 必填项。 |
| `.github/workflows/hci-sim-db-migration-test.yml` | 隔离 PG 跑迁移校验 16 表，反向校验主库无 `agent_test_*` 表，验证 `hci_sim_runtime` 角色不能建表（DDL 负向检查），再跑 `internal/database` + `internal/reconciler` 的 CAS/幂等测试。 |

---

## 6. 端到端链路与验收入口（现行最新）

### 6.1 纵向闭环（已验证：KBD 27123 revision 25）

```text
Admin UI 环境构建
  → API Gateway（注入 Runtime Token）
  → hci_sim Scenario + published Bundle + Run
  → 真实 Case / Conversation
  → Agent 权威 sim-ssh context（不再执行前端固定 smoke）
  → K3s terminal_bridge（同源 /terminal-bridge，auth_type=lease, execution_mode=sim-ssh）
  → K3s hci-sim（SSH :2222，exact RouteKey）
  → Agent 发起 task / lsof / ps
  → passed Result
```

### 6.2 用户验收入口（**已迁移至 admin-ui**，旧 Customer UI 仿真租约表单已废弃）

- 入口组件：`frontend/admin/src/components/SimulationConversation.vue`。
- 组件自动 `createConversation()` + `connectBridge()`，经受管 `terminal_bridge`（WebSocket `/terminal-bridge` → SSH:2222）连接，**不再需手动粘贴 `connection.json`**。
- Lease 由 admin-ui 自动填充（页面 SSH 标签显示「已连接」）。
- Agent 产出 `recommended_command` 以命令卡片呈现：risk=1 自动跑、risk=2 需点「允许执行」、risk=3 自动阻止。
- 结果在会话流直接可见：命令卡片 `passed`/`failed`/`blocked`；会话级 `agentOutcome` 为 `passed`/`failed`/`inconclusive`；`exit_code !== 0` 自动判 `failed`。

> ⚠️ 旧版 `hci_sim/README.md` 与部分任务文档曾描述「Customer UI 终端面板 → 仿真租约连接（dev）」流程，该形态已被 admin-ui 取代，本文为现行权威描述。

---

## 7. 当前状态与边界（对抗性审查结论）

### 7.1 已证明

- KBD 27123 revision 25 真实纵向样板：PASS（不依赖 Custom UI、Windows/Linux Docker Bridge 或真实 HCI）。
- 仿真运行控制面（Runtime/Bridge/Manifest）代码级基础已实现；C1 对 dev 126 条 KBD 完成只读快照基线。

### 7.2 BLOCKED（面向终端用户的生产交付）

以下前提未满足即不可宣称产品级验证：

1. 无 approved Artifact（artifact.approval 双角色审批通过）；
2. 无真实校准环境与生产 CAS / Bridge E2E；
3. 20-repeat、100+ 并发、真实 Capability Matrix 未验证；
4. 主库 15 张空旧表 contract/drop 未完成。

> 决策：接受 27123 作为纵向基线与回归 gold case；**不接受「27123 通过」等价于「hci-sim 可生产交付」**。

### 7.3 下一阶段目标：Bundle 工厂化

把新增 KBD 从「修改并发布应用」改为「编译并发布不可变 Bundle」：

- 唯一 Bundle Registry：数据库存 metadata，对象存储存不可变内容，Runtime 按 digest 加载；
- 统一 Bundle Compiler：校验 revision/Signal/Route/Catalog/argv/timeout/输出/变体/digest/provenance/审批；
- 服务端收敛 Build/Bundle 注册/调度/Lease/Case 绑定/Result 关闭；
- 新增 KBD 禁止修改 Agent/hci-sim 代码、Dockerfile、Helm、Argo、服务镜像 digest。

**生产放行标准**（全部满足前保持 BLOCKED）：新 KBD 无需代码/镜像/Helm/Argo 变更；Bundle 仅一个权威 digest；≥3 个差异化 KBD 三变体自动 E2E 通过；capability matrix 可自动执行且错误不吞没；干净集群部署/重复运行/重启/回滚不依赖人工 patch；失败可定位到 KBD/Bundle/Agent/Bridge/Runtime/Result 层。

---

## 8. 文档导航（整理后）

| 维度 | 入口 |
|---|---|
| 设计（全量 + 阶段 A–E 事件） | [`docs/solution/hci-sim/`](docs/solution/hci-sim/README.md) |
| 任务（全量 + 阶段 A–E 事件） | [`docs/task/hci-sim/`](docs/task/hci-sim/README.md) |
| 验证（全量分析 + 阶段 A–E 事件） | [`docs/verify/hci-sim/`](docs/verify/hci-sim/README.md) |
| 需求 | [`docs/requirement/hci-sim/events/`](docs/requirement/hci-sim/events/) |

### 关键事件文档索引

- 生产化差距与 Bundle 工厂化重构基线（BLOCKED 定义）：`docs/solution/hci-sim/events/2026-08-11-hci-sim生产化差距审查与Bundle工厂化重构基线.md`
- P0–P1 27123 全链路修复（implemented）：`docs/solution/hci-sim/events/2026-08-11-hci-sim-P0-P1-27123全链路修复方案.md`
- 独立数据库隔离（in-progress）：`docs/solution/hci-sim/events/2026-08-10-hci_sim独立数据库隔离方案.md`
- 阶段 C3 两步人工验收闭环：`docs/solution/hci-sim/events/2026-08-06-hci-sim阶段C3两步人工验收闭环方案.md`
- 双轨运行时设计（real/sim）：`docs/solution/hci-sim/events/2026-07-30-HCI真实环境与hci-sim双轨运行时设计.md`
