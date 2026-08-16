# hci_sim · 现行全量设计说明

> 适用范围：`hci_sim/` 源码目录及其关联的控制面、数据库、部署与验收链路
> 详细阶段设计见 [`docs/solution/hci-sim/`](docs/solution/hci-sim/README.md)；任务见 [`docs/task/hci-sim/`](docs/task/hci-sim/README.md)；验证见 [`docs/verify/hci-sim/`](docs/verify/hci-sim/README.md)

---

## 1. 它是什么

`hci_sim` 是 HCI 仿真 SSH Runtime 的**唯一正式源码目录**。产品、镜像、Helm release 名保持 `hci-sim`。

**本质**：一个被管制的仿真内核——只接受带 HMAC 签名租约的 SSH 连接，命令必须精确匹配已发布的 Fixture Bundle，否则 fail-closed 拒绝。用于在**不可触碰真实生产客户环境**的前提下，对 KBD（知识库）修复命令做端到端验证。

**为什么需要**：真实 KBD 修复命令直接在生产跑风险极高，但又要证明「这条 KBD 给的命令在真实节点上确实能跑通、exit_code 正确」。解法不是测试环境，而是强隔离、确定性、带租约鉴权的仿真 Runtime。

---

## 2. 代码层

源码结构：`hci_sim/`（`internal/` 核心，`cmd/` 入口），无 `pkg/`。

### 2.1 程序入口（`cmd/`）

| 程序 | 文件 | 作用 |
|---|---|---|
| `hci-sim` | `cmd/hci-sim/main.go` + `bootstrap.go` + `offline_manifest.go` | 主服务。子命令：`serve`（默认，SSH:2222 + HTTP:18080 + `readyz`）、`lease`（签发 `htp2` 前缀的租约 token）、`manifest-digest`（写入/校验 bundle `sha256:` 摘要）、`bootstrap`（控制面初始化）。 |
| `hci-sim-smoke` | `cmd/hci-sim-smoke/main.go` | 冒烟测试。经真实 Terminal Bridge WebSocket→SSH 链路执行 `recommended_command`，只输出 `support_id/test_run_id/exit_code` 摘要。 |

### 2.2 核心包（`internal/`）

| 模块 | 职责 |
|---|---|
| `controlplane/` | 阶段 C–E 业务内核：Bundle 生命周期状态机（draft→validated→approved→published→stale/retired）、双角色审批、TestRun、Run Lease、差分/mutation/稳定性/容量证据模型。 |
| `database/` | PostgreSQL 访问层（`RunRepository`）：scenario/run/run_attempt/run_event/run_result/run_outbox 的 CRUD、CAS 幂等、过期、outbox claim/complete。 |
| `fixture/` | 从 GitOps 只读发布集合加载多个已发布 Manifest v2，按租约 `support_id` 选择 Bundle，再按精确 RouteKey 确定性路由；Fault 模型（none/nonzero_exit/permission/timeout/disconnect/truncate）。 |
| `lease/` | `htp2` 前缀 Capability 租约签发、每命令复验、单副本配额；HMAC 签名，Claims 绑定 Run/Bundle/Target。 |
| `server/` | SSH + HTTP 服务面；exec/交互 shell 共用有界 worker 队列、授权、fail-closed 路由、观测。 |
| `reconciler/` | Runtime 侧 durable outbox 投递循环；无下游 URL 时只恢复过期 processing，不标 processed。 |
| `metrics/` | OpenTelemetry 指标。 |
| `telemetry/` | OpenTelemetry trace 初始化/导出（OTLP HTTP）。 |

**依赖方向**：`controlplane` 依赖 `fixture`/`lease`；`reconciler`/`server` 依赖 `database`。

### 2.3 运行时安全基线

- 仅加载带 `bundle.digest` 的已发布 Manifest v2；拒绝未知字段、摘要漂移、非规范 argv、歧义 RouteKey。
- `HCI_SIM_REQUIRED_BUNDLES` 必须与实际加载集合完全一致；缺失、夹带或 digest 漂移都会阻止 Runtime 启动。
- 数据库启用时，Runtime 在监听端口前原子同步全部 Bundle Registry 元数据；新 digest 激活、旧 digest 失效和审计共用唯一 `trace_id`，任一步失败都会阻止启动。
- 每个命令精确绑定 `variant + tool + acquisition_key + argv + virtual_node_id + container`，无正则/通配符/评分路由/默认 fixture。
- SSH 仅接受 `htp2` 租约；签名、issuer、audience、`support_id + Bundle digest + KBD/工具/策略版本`、目标、时效及会话/命令/输出配额均受校验。
- `exec` 与交互 shell 共用有界 worker 队列、每命令授权、fail-closed 路由；原始命令与 token 不进日志。
- Terminal Bridge 仅通过 `auth_type=lease` + `execution_mode=sim-ssh` 模拟执行，绝不按 host 名猜测回退真实 HCI。

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

---

## 4. 部署与脚本

### 4.1 Helm（`deploy/helm/hci-sim/`）

- `templates/deployment.yaml` / `templates/service.yaml`：部署与 SSH/HTTP 服务暴露。
- `templates/networkpolicy.yaml`：限制入站来源命名空间，仅允许 `terminalBridgeNamespace` / `observabilityNamespace` / `apiGatewayNamespace` / `conversationServiceNamespace` 四个命名空间访问（CI 校验必填项缺失即失败）。
- `templates/pdb.yaml` / `templates/resourcequota.yaml`：可用性与资源配额。
- `files/*-fixture-manifest.json`：随附夹具清单；`fixture.manifestFiles` 是唯一 GitOps 发布集合，Helm 将其渲染为只读 ConfigMap，并把集合 digest 写入 Pod 模板触发滚动更新。
- `values.yaml` 中 `replicaCount` 为 `1`：内存配额 Tracker 是单副本实现。

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
| `.github/workflows/hci-sim-go.yml` | Go 质量门禁：`gofmt`/`go test`/`go test -race`/`go vet`/`go build`；`published bundle and Helm gate` 校验已发布 bundle digest 一致、拒绝已退役的运行时 marker、Helm lint/template 并校验 NetworkPolicy 必填项。 |
| `.github/workflows/hci-sim-db-migration-test.yml` | 隔离 PG 跑迁移校验 16 表，反向校验主库无 `agent_test_*` 表，验证 `hci_sim_runtime` 角色不能建表（DDL 负向检查），再跑 `internal/database` + `internal/reconciler` 的 CAS/幂等测试。 |

---

## 6. 端到端链路与验收入口

### 6.1 纵向闭环

```text
Admin UI 环境构建
  → API Gateway（注入 Runtime Token）
  → hci_sim Scenario + published Bundle + Run
  → 真实 Case / Conversation
  → Agent 权威 sim-ssh context
  → K3s terminal_bridge（auth_type=lease, execution_mode=sim-ssh）
  → K3s hci-sim（SSH :2222，exact RouteKey）
  → Agent 发起 task / lsof / ps
  → passed Result
```

### 6.2 用户验收入口（admin-ui）

- 入口组件：`frontend/admin/src/components/SimulationConversation.vue`。
- 组件自动 `createConversation()` + `connectBridge()`，经受管 `terminal_bridge`（WebSocket `/terminal-bridge` → SSH:2222）连接，无需手动粘贴 `connection.json`。
- Lease 由 admin-ui 自动填充（页面 SSH 标签显示「已连接」）。
- Agent 产出 `recommended_command` 以命令卡片呈现：risk=1 自动跑、risk=2 需点「允许执行」、risk=3 自动阻止。
- 结果在会话流直接可见：命令卡片 `passed`/`failed`/`blocked`；会话级 `agentOutcome` 为 `passed`/`failed`/`inconclusive`；`exit_code !== 0` 自动判 `failed`。

---

## 7. 文档导航

| 维度 | 入口 |
|---|---|
| 设计（全量 + 阶段 A–E 事件） | [`docs/solution/hci-sim/`](docs/solution/hci-sim/README.md) |
| 任务（全量 + 阶段 A–E 事件） | [`docs/task/hci-sim/`](docs/task/hci-sim/README.md) |
| 验证（全量分析 + 阶段 A–E 事件） | [`docs/verify/hci-sim/`](docs/verify/hci-sim/README.md) |
| 需求 | [`docs/requirement/hci-sim/events/`](docs/requirement/hci-sim/events/) |
