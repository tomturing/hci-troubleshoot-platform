---
status: active
category: solution
audience: product, architect, developer, tester, operator
last_updated: 2026-07-30
owner: team
---

# HCI 真实环境与 hci-sim 双轨运行时设计

## 背景与需求

Terminal Bridge P0 已把 Custom UI、WebSocket、Bridge、SSH、HCI、结果回传、Agent 和可观测性链路打通。真实 HCI 是最终事实源，但案例故障难构造、难重复、难隔离，不适合 100+ Agent 并发回归。

此前已经提出 KBD Fixture Compiler、Scenario Scheduler、共享 hci-sim 和逻辑隔离。本次决策进一步回答：到底使用真实 HCI 还是 hci-sim、模拟器用什么技术、部署在哪里、如何同时承载 100+ KBD、怎样防止模拟器自洽假通过，以及如何控制投入产出。

完整需求见 [HCI 真实环境与仿真环境双轨测试需求](../../../requirement/hci-sim/events/HCI真实环境与仿真环境双轨测试需求.md)。

## P0 实施状态（2026-07-30）

本设计的 KBD 27123 单场景 Golden slice 已实施并部署到 dev：

- `hci_sim/` Go SSH runtime、signed Lease、Fixture Router、worker/queue、OTel/metrics 已实现；
- `deploy/helm/hci-sim/` 已部署为 `hci-sim-dev/hci-sim`；
- Terminal Bridge 共用代码已支持 Lease 和 Trace over SSH；
- 工单 `Q2026073088434` 已通过 Customer UI Headless Browser → Agent/CDD → Bridge → SSH → hci-sim → Artifact/Evaluation/Conclusion 完整验收；
- trace `956f403dbf67fc597fd3a372e175ca61` 覆盖 7 个服务，三个 Signal Evaluation 均 PASS，S4 为 definitive；
- Prometheus、Loki、Tempo 已能观测 hci-sim。

详细证据见 [KBD 27123 hci-sim P0 端到端验证](../../../verify/hci-sim/events/KBD27123-hci-sim-P0端到端验证.md)。

该状态不代表 Windows desktop Bridge、real/sim differential、20 次稳定性和 100+ 并发已经通过；后续仍按本文 P1 门禁推进。

## 方案（WHAT）

### 1. 双轨而非混轨

平台同时保留：

```text
L2 主回归：Agent -> Custom UI 协议 -> Terminal Bridge -> hci-sim SSH
L3 校准：  Agent -> Custom UI        -> Terminal Bridge -> hci-real SSH
```

一次 run 在创建时固定 `execution_mode`：

- `ssh`：真实 HCI；
- `sim-ssh`：hci-sim；
- `replay`：仅快速协议测试，不属于完整 E2E。

生产只走 real；日常自动回归和 100+ 并发只走 sim；校准时创建两个独立 run，再以 `calibration_group_id` 比较。禁止同次 run 切换，禁止 sim miss 后 fallback 到 real。

### 2. 数据面使用 Go 自定义 SSH Server

hci-sim 是 Go 单二进制容器，使用 `golang.org/x/crypto/ssh` 实现受控 SSH Server。它不启动完整 OpenSSH，不把输入交给 `/bin/sh -c`，只处理 auth、session、受控 PTY/env、exec、stdout/stderr、exit-status、timeout 和 cancel。

内部模块：

```text
SSH Server
  -> Lease Validator
  -> argv/AST Parser + Policy
  -> Canonical Command Fingerprint
  -> Fixture Router
  -> Bounded Worker Pool
  -> Chunk/Delay/Fault Injector
  -> stdout/stderr/exit-status
  -> OTel + Prometheus + structured log
```

Fixture Compiler、Scenario API 和 Scheduler 属于控制面，P1 使用项目已有 Python/FastAPI。P0 先用 Go runtime、静态 manifest 和测试 CLI 验证最小闭环。

### 3. 使用容器，但不按 KBD 创建容器

常态使用共享 Deployment：P0 1 replica，P1 2～4 replicas。100+ KBD/测试环境通过 ScenarioContext 逻辑隔离，每个 Scenario 引用不可变 fixture bundle。

只有强状态、不同软件版本或独立网络拓扑场景才分配专用 shard/Pod。完整 HCI VM 只保留为极少数系统级测试手段。

### 4. 部署于 WSL K3s 独立 namespace

dev 部署位置：

```text
namespace: hci-sim-dev
release: hci-sim
service: hci-sim.hci-sim-dev.svc:2222
```

P0 不并入 `hci-dev`，不恢复当前 GitOps self-heal，不把 live drift 当作目标基线。未来 staging 使用 `hci-sim-staging`；prod 默认完全不部署。

NetworkPolicy 默认 deny all，只允许带指定 label 的 dev/test Terminal Bridge 访问 2222，允许必要的 DNS、OTLP 和只读 fixture storage；显式禁止访问真实 HCI 网段。

### 5. Scenario Lease 在 SSH 认证阶段绑定环境

Scheduler/后端签发短时 signed lease：

```json
{
  "lease_id": "opaque-id",
  "test_run_id": "run-id",
  "scenario_id": "scenario-id",
  "fixture_manifest_hash": "sha256:...",
  "execution_mode": "sim-ssh",
  "expires_at": "...",
  "max_sessions": 2,
  "max_commands": 40,
  "signature": "..."
}
```

P0 使用固定 username `sim` 和短时 signed token password；P1 可升级 SSH certificate。hci-sim 校验后把 ScenarioContext 绑定到 SSH connection。浏览器只中继 token，Terminal Bridge 保持领域透明，scenario id 不拼进命令。

### 6. 100+ 并发由逻辑 universe 和有界调度实现

每个 Scenario 是独立 universe：

```text
test_run_id + scenario_id
  -> fixture_manifest_hash / variant
  -> virtual_node_id / container
  -> variable profile / state overlay
  -> SSH session / command / output quota
  -> trace / exec / artifact / evaluation namespace
```

命令路由键包含 scenario、node、container、acquisition 和 canonical fingerprint，不能只看 command string。

Pod 内处理路径：

```text
SSH session
 -> lease validation
 -> parse/policy
 -> per-run quota
 -> per-scenario semaphore
 -> bounded queue
 -> fixed worker pool
 -> streamed fixture output
```

队列满时返回 `sim_overloaded`，unknown 返回 `fixture_not_found`，均为非零 exit/fail closed。每命令、每场景和每 run 都有时间、命令数和输出总量预算。

Stateless fixture 可由任意 Pod 处理；同一 TCP connection 自然固定 Pod。跨重连状态放 Redis/CAS，强状态场景由 Scheduler 分配固定 shard，不依赖某 Pod 的隐式内存。

### 7. 初始容量和压测

P1 初始假设：3 replicas、每 Pod 8～16 workers、每 worker 16～32 个流式命令、100～200 active Scenario、每 Scenario 默认 1/最大 4 个命令。单 Pod初始 request/limit 为 500m/2 CPU、512Mi/1Gi；单命令默认 4MiB 输出预算。

这些是压测起点，不是验收结论。必须按 1/10/50/100/200 梯度记录 p50/p95/p99、队列、拒绝、内存、GC、SSH session、吞吐和串线数，再更新容量参数。

### 8. 存储和 Fixture 编译

- 小型 P0 manifest 可放 ConfigMap/镜像 testdata；
- metadata、审批、run 记录放 PostgreSQL；
- 大 stdout/stderr 和原始 Artifact 放对象存储；
- 编译 bundle 以 manifest hash 内容寻址、只读发布；
- Pod 用有界 `emptyDir` LRU 缓存；
- KBD/工具/Matcher/policy 变化使 fixture stale。

Compiler 从 KBD revision、signals、真实 Artifact provenance 生成 positive-minimal、positive-realistic、negative、near-miss、timeout、permission 和 unknown。真实 Artifact 必须脱敏和参数化，图片/OCR 不能直接发布为 observation。

### 9. Trace 与执行证据

Terminal Bridge 在 SSH exec channel 通过 allowlist `env` request 传播 `TRACEPARENT`、可选 `TRACESTATE`、`HTP_EXEC_ID` 和 `HTP_TEST_RUN_ID`。hci-sim 校验后创建子 Span。

关联链：

```text
trace_id
 -> exec_id
   -> hci-sim command span
     -> fixture_id / artifact_id
       -> scenario_id / KBD revision / signal_id
         -> evaluation_id
           -> candidate state / conclusion
```

模拟链路增加 `simulation=true`、`execution_mode=sim-ssh`、scenario/lease/fixture/fingerprint/worker/shard 等字段。完整输出进入受控 Artifact，日志只记录字节数、hash、截断、错误分类等 metadata。

### 10. 真实 HCI 作为 Oracle

真实 HCI 仅做：

- 新版本 SSH/aCLI/PTY/quote/output 校准；
- 真实 Artifact 复采；
- 少量只读 smoke；
- real/sim differential comparison。

真实凭据只在受控 Secret/凭据系统中，绝不写入文档、Git、fixture、日志或 hci-sim。hci-sim 没有真实凭据且网络不可达真实 HCI。

### 11. 100+ Agent 与浏览器的边界

hci-sim 支持 100+ SSH 场景不等于已经验证 100 个浏览器窗口。测试分层：

- 1～10 个真实浏览器/Playwright context 做 Golden UI E2E；
- headless runner 实现相同 Custom UI 命令中继协议，驱动 100+ conversation，经同一 cluster Terminal Bridge 和 hci-sim 做 Bulk Agent E2E；
- 如需宣称 100 浏览器并发，单独运行并报告浏览器负载测试。

### 12. P0 Golden KBD

使用 dev 已发布并有真实验收 Trace 的 KBD 27123，覆盖 task → HOST/VM/END、lsof → PID、ps → CMD。已验收 Trace `c6acbb7bdf5faffaedf5a6faa04eeb97` 作为 provenance，生成脱敏的 positive-realistic 及 negative/near-miss/timeout/unknown 变体。

P0 先冻结 Scenario Lease v1、Fixture Manifest v1、错误 envelope、Trace over SSH 和 real/sim diff 契约，再开发运行时。

## 决策依据（WHY：为什么选此方案，为什么不选其他方案）

### 为什么 real 与 sim 双轨共存

只用真实 HCI 无法稳定构造大量故障、反例和 100+ 隔离环境；只用 hci-sim 又无法发现模拟器与真实 aCLI、SSH、PTY、stderr 和 exit code 的漂移。L2 sim 提供确定性和规模，L3 real 提供外部真实性约束，两者各自独立运行后做差异校准，投入产出最优。

### 为什么一次运行不能混用

一次运行的 Observation 必须来自同一事实世界。混用会导致证据来源不可解释、Trace 无法审计、sim 缺口被真实 fallback 掩盖，还可能让自动测试触达真实 HCI。固定 mode 是数据正确性和安全边界，不只是配置偏好。

### 为什么不在 Terminal Bridge mock

Bridge mock 会跳过 SSH、认证、PTY、远程取消、stdout/stderr、exit-status 和目标路由，并把 KBD 规则污染到 Windows/K3s 共用代码。它只能做快速 replay，不能代表完整 E2E。

### 为什么使用 Go 自定义 SSH Server

Go 与 Terminal Bridge 同语言和 SSH/OTel 生态，适合大量连接、流式输出和取消；单二进制运行成本低。自定义 Server 能拒绝任意 shell，比 OpenSSH + 脚本更容易保证 command policy、租户上下文和统一观测。

### 为什么不是每场景 Pod/VM

绝大多数 KBD 测试只需要不同的 observation、变量和时序，不需要真实内核或完整 HCI。按场景复制 Pod/VM 会让启动、Service、Secret、存储和清理成本线性增长，却不增加相应诊断保真度。逻辑隔离满足主流场景，专用 shard 作为强状态例外。

### 为什么不能把 100 个 KBD 合并到全局命令表

不同 KBD 可对同一命令、节点、PID 或日志关键字有相反期望。没有 Scenario Lease 的全局映射会串用 fixture，得出假结果。共享只读 corpus 可以节省资源，但路由和状态必须隔离。

### 为什么不选 Docker-in-Docker、完整 OpenSSH 或完整 HCI 虚拟化

- Docker-in-Docker 需要高权限，扩大集群攻击面，仍不等价于真实 HCI；
- OpenSSH + 通用 shell 容易命令注入、脚本漂移和观测碎片；
- 完整 HCI 虚拟化保真度最高，但 100+ 的资源、构造和维护成本远超首期收益。

### 投入产出

P0-0 契约/Oracle 1～2 人日，P0 Runtime 3～5 人日，P1 MVP 30～45 人日，产品化累计约 55～80 人日。P0 以 KBD 27123 证明 real/sim 契约等价后再投资 P1；若不能证明，应修正契约而不是扩充案例数量。

## 影响范围（哪些现行全量文档需要更新）

- [Agent 并发测试与 KBD 驱动 HCI 仿真环境方案](../Agent并发测试与KBD驱动HCI仿真环境方案.md)：已补齐双轨和运行时定版；
- [Agent 方案文档索引](../README.md)：增加 hci-sim 导航；
- [系统架构任务](../../../task/架构任务.md)：登记 P0-0/P0 Spike；
- [docs 冷启动入口](../../../README.md)：登记当前里程碑和事实状态；
- P0 实施已同步任务、验证事件和冷启动入口；部署设计、接口设计和 100+ 测试指南将在 P1 控制面/容量阶段继续补齐。
- 实施后还需继续更新部署设计、部署指南、可观测性设计、接口设计和测试指南；本次 P0 只宣称 KBD 27123 单场景 Golden E2E，不宣称 100+ 并发已完成。

## 验收标准

- 文档明确平台双轨共存、单 run 二选一和禁止 fallback；
- 文档明确 Go 自定义 SSH Server、容器/K3s 独立 namespace 和存储方案；
- 文档明确 Scenario Lease、命令复合路由和 Trace over SSH；
- 文档明确 100+ 逻辑隔离、worker/backpressure、多副本状态和容量压测；
- 文档明确真实 HCI Oracle、安全边界和凭据处理；
- 文档明确 Golden UI E2E 与 100+ Bulk Agent E2E 的区别；
- P0 使用 KBD 27123 和已验收真实 Trace，不以不存在的示例替代；
- 需求、方案、任务和索引同步，且不把 proposed 设计写成已实施事实。
