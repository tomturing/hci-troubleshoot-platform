---
status: proposed
category: solution
audience: architect, developer, tester, operator
last_updated: 2026-07-30
owner: team
---

# Agent 并发测试与 KBD 驱动 HCI 仿真环境方案

> 对应需求：[Agent 并发测试与 KBD 驱动 HCI 仿真环境需求](../../requirement/events/2026-07-28-Agent并发测试与KBD驱动HCI仿真环境需求.md)

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|---|---|---|---|
| 2026-07-30 | v1.1 | 补齐 hci-real/hci-sim 双轨边界、Go 容器运行时、K3s 独立部署、Scenario Lease、100+ 并发容量模型、真实 HCI Oracle 校准与安全门禁 | [HCI 真实环境与 hci-sim 双轨运行时设计](events/2026-07-30-HCI真实环境与hci-sim双轨运行时设计.md) |
| 2026-07-28 | v1.0 | 首版 KBD Fixture、共享 hci-sim、场景调度和 100+ 逻辑隔离方案 | [Agent 并发测试与 KBD 驱动 HCI 仿真环境需求](../../requirement/events/2026-07-28-Agent并发测试与KBD驱动HCI仿真环境需求.md) |

## 0. 先回答最容易混淆的问题

平台不是在“只用真实 HCI”“real 与 sim 在一次运行中混用”“只用 hci-sim”之间三选一。正式设计是：

> **平台层面让 hci-real 与 hci-sim 双轨共存；一次 Agent 运行必须且只能选择其中一条轨道。**

| 使用场景 | 选择的轨道 | 原因 |
|---|---|---|
| 生产工单诊断 | 仅 `hci-real` | 目标是诊断客户真实状态，模拟结果没有生产事实效力 |
| 日常 Agent 自动回归、100+ 并发、negative/near-miss/timeout 测试 | 仅 `hci-sim` | 可重复、可隔离、低成本、可精确构造稀有故障和反例 |
| 新 aCLI/HCI 版本校准 | 分别独立运行一次 `hci-real` 和一次 `hci-sim` | 用真实环境作 Oracle，计算协议和输出漂移；不是在同一次运行中混用 |
| 人工演示或协议调试 | 明确选择其中一轨 | 结果必须标记 `execution_mode`，避免把模拟证据当成真实证据 |

严禁以下行为：

1. 同一次 Agent 运行前半段使用 hci-sim、后半段切换到真实 HCI；
2. hci-sim 返回 `fixture_not_found`、超时或故障后自动 fallback 到真实 HCI；
3. 把真实 HCI 凭据注入 hci-sim，或允许 hci-sim 访问真实 HCI 网段；
4. 把 real/sim 结果混入同一成功率分母，或省略 `execution_mode`；
5. 因为模拟结果“符合 KBD”就把它当成真实 HCI 已存在该故障的证据。

两条轨道共享的是需要被验证的执行契约：Agent、Custom UI、Terminal Bridge、SSH 语义、result envelope、`trace_id/exec_id/artifact_id/evaluation_id`、Matcher 和 CDD。两条轨道不共享 SSH 目标、凭据、环境状态、fixture、网络权限和成功率统计。

## 1. 决策摘要

当前 Terminal Bridge 已支持 Windows desktop 和 WSL/K3s cluster 两种运行形态，并具备 WebSocket、SSH、trace、stdout/stderr、超时、Artifact 和结果回传能力。现有方案可以扩展为 100+ 并发 Agent 测试，但必须增加“测试场景调度层”和“模拟 HCI 目标层”，不能仅在 terminal_bridge 中按命令返回固定文本。

推荐把控制面与执行数据面分开：

```text
控制面：KBD revision -> Fixture Compiler -> Scenario Registry / Scheduler
                                      | signed lease + immutable bundle
                                      v
执行面：Agent/Custom UI -> terminal_bridge -> SSH -> hci-sim 多租户运行时
       Agent/CDD        <- exec-result     <- stdout/stderr/exit-status
```

核心取舍：

1. **Terminal Bridge 只负责传输和执行，不理解 KBD。**
2. **hci-sim 通过 SSH 提供模拟 HCI 行为，保留真实 Bridge→SSH 路径。**
3. **100+ 默认采用逻辑隔离，不默认创建 100 个完整 HCI Pod。**
4. **每个场景拥有独立 fixture namespace、变量 profile、SSH session、trace 和结果。**
5. **同一个 hci-sim 进程可以同时承载多个 KBD 场景，但路由键必须带 `scenario_id` 或隔离的 SSH target。**
6. **fixture 必须绑定不可变 KBD revision，KBD 变化自动使 fixture stale。**

## 2. 第一性原理：测试对象分层

Agent 排障能力不是一个单一指标，而是多个阶段的组合：

```text
KBD 信号
  -> acquisition 选择
  -> 变量解析
  -> qkv/qfk 工具执行
  -> 命令/目标路由
  -> SSH/Bridge 传输
  -> stdout/stderr observation
  -> matcher 判定
  -> Candidate State Reducer
  -> Conclusion Gate
  -> 报告和证据链
```

因此采用测试金字塔：

| 层级 | 运行方式 | 主要验证内容 | 速度 | 保真度 |
|---|---|---|---:|---:|
| L0 | 纯 Python/Matcher | matcher、状态机、Conclusion Gate | 极快 | 低 |
| L1 | 工具契约 | qfk/qkv、command、变量、filter、timeout | 快 | 中 |
| L2 | Bridge→SSH→hci-sim | 完整 WebSocket、SSH、stdout/stderr、trace | 中 | 高 |
| L3 | 真实 HCI smoke | 少量真实 HCI 只读案例 | 慢 | 最高 |

“Agent 真实排障能力”的主判定应以 L2 为准，L3 用于校准，不应把所有测试都建立在完整 HCI 虚拟化上。

## 3. 为什么不能只在 Terminal Bridge 中 mock

以下做法不建议作为主方案：

```go
if strings.Contains(command, "too many file") {
    return fixtureOutput
}
```

它只能验证 Bridge 协议的一部分，无法验证 SSH 连接与认证、shell argv 解析、PTY/非 PTY 差异、stdout/stderr 分流、exit code、命令超时、远程进程终止、container 包装、多节点路由、未知命令失败处理和 Agent 是否真的选择了正确 acquisition。

此外，Bridge 会开始包含 KBD 领域知识，Windows EXE 和 K3s Pod 也可能在模拟规则上产生漂移。

定义三种执行后端，但只把 `sim-ssh` 作为主 E2E 路径：

| execution_mode | 路径 | 用途 |
|---|---|---|
| `ssh` | Bridge → 真实 HCI SSH | 生产和真实 smoke |
| `sim-ssh` | Bridge → hci-sim SSH | 主 Agent 回归、并发测试 |
| `replay` | Bridge → 本地 fixture replay | 快速协议/单元契约，不宣称完整 E2E |

所有模式使用同一结果 envelope 和观测字段，并显式记录 `execution_mode`。

## 4. 目标总体架构

### 4.1 逻辑架构

```text
KBD Revision -> Fixture Compiler -> Scenario Registry/Scheduler
                                         | manifest + scenario lease
                                         v
Agent / Conversation / CDD <-> Custom UI / Headless Runner
          ^                              |
          | exec-result                  | WebSocket command
          |                              v
          +---------------------- terminal_bridge
                                         |
                                         | SSH
                                         v
                             hci-sim Runtime
                             - lease validator
                             - fixture router
                             - virtual nodes/containers
                             - tenant/scenario namespace
```

### 4.2 运行形态

#### 方案 A：共享 hci-sim + 逻辑场景隔离（首选）

```text
100+ scenario
   ├── scenario-0001
   ├── scenario-0002
   ├── ...
   └── scenario-0100
          ↓
     hci-sim router
```

每个场景不是一个 Pod，而是一组隔离状态：`scenario_id`、fixture manifest、environment profile、virtual node/host/container、SSH session key、trace context 和 output/artifact namespace。

优点：启动快、资源少、适合 100～1000 个逻辑场景。

#### 方案 B：每个场景独立 hci-sim Pod

适用于需要持久化状态变化、不同软件版本、独立网络或节点拓扑的场景。100 个实例可以支持，但 Pod、SSHD、Service、Secret、fixture volume 数量会快速膨胀，不应作为默认模式。

#### 方案 C：共享只读 fixture + 每场景临时 overlay

```text
只读 fixture corpus + 每场景 overlay state
                  ↓
          scenario-specific runtime
```

fixture 本身只读共享，场景变化写入独立 overlay，既节省磁盘/内存，又能防止测试互相污染。这是共享模式向有状态场景扩展时的推荐形态。

## 5. 100+ 并发的关键设计

### 5.1 100+ 不等于 100 个完整 HCI 集群

Agent 信号测试通常需要的是不同日志响应、节点变量、命令返回码、stdout/stderr、超时/权限行为、KBD fixture 和变量生产链。这些大部分可以用逻辑隔离表达，不需要 100 个真实数据库、控制面和存储集群。

### 5.2 场景隔离键

场景路由至少使用以下复合键之一：

```text
tenant_id + scenario_id + virtual_node_id + command_fingerprint
```

在当前项目中建议优先使用：

```text
test_run_id + scenario_id + node_ip + acquisition_key
```

不能只使用 command 字符串路由，否则两个场景同时执行同一条命令时会串用 fixture。

### 5.3 场景租约

Scenario Scheduler 为每个场景发放短期 lease：

```json
{
  "test_run_id": "run-20260728-001",
  "scenario_id": "scenario-0042",
  "fixture_manifest_hash": "sha256:...",
  "expires_at": "...",
  "execution_mode": "sim-ssh"
}
```

lease 必须有过期时间，绑定 KBD revision 和 fixture manifest hash，只能用于当前测试运行，不能由浏览器修改，过期后未知命令一律报错。

### 5.4 并发控制

首期建议配置：

| 资源 | 建议 |
|---|---:|
| 同时活跃 scenario | 100～200 |
| 每场景同时 SSH command | 1～4 |
| hci-sim worker | 4～16 |
| 单 worker command 并发 | 16～64 |
| stdout 内存预算 | 按场景限额 |
| fixture artifact 总预算 | 按 test run 限额 |

必须使用 global worker pool、per-scenario semaphore、backpressure、每场景 timeout、test run 总预算和失败重试上限，禁止按场景创建无界 goroutine/任务。

### 5.5 hci-sim 的两种运行模式

#### Stateless Router

命令只根据当前请求和 fixture 返回结果，适合日志、服务、节点、任务、`ps`、`lsof` 等只读命令，可支持很高并发。

#### Stateful Scenario Worker

每个场景拥有内存状态或 overlay 文件系统，适合第一步产出 PID、第二步使用 PID，第一步产出 HOST、第二步路由 HOST，或需要多轮恢复的 KBD。推荐默认 Stateless，仅为有变量链/状态变化的 KBD 启用 Stateful Worker。

## 6. hci-sim 设计

### 6.1 组件

```text
hci-sim
├── sshd
├── session manager
├── shell command parser
├── command fingerprint resolver
├── fixture router
├── virtual filesystem
├── virtual node/container registry
├── stdout/stderr chunk scheduler
├── fault injector
└── OTel/Prometheus exporter
```

### 6.2 命令路由

不能只用 `strings.Contains` 匹配命令。推荐使用 canonical command fingerprint：

```json
{
  "tool": "qfk_log",
  "resource_keyword": "too many file",
  "path": "/sf/log/today/sfvt_vtpdaemon.log",
  "host": "node-001",
  "container": "host",
  "policy_version": "qfk-v2"
}
```

路由流程：

```text
真实 shell command
  → shell argv parser
  → qfk semantic parser
  → canonical args
  → acquisition_key
  → scenario_id/fixture_id 查找
```

Fixture Compiler 应复用当前 qfk HandlerRegistry 的命令构造逻辑，不重新维护一套 qfk 命令规则。

### 6.3 未知命令必须 fail closed

如果没有 fixture：

```text
exit_code = 127 或测试专用错误码
stderr = fixture_not_found
error_type = fixture_not_found
```

不能返回空 stdout + exit_code 0，否则 Agent 可能把模拟器配置缺失误判为 HCI 正常。

### 6.4 输出模型

Fixture 必须支持 stdout、stderr、exit_code、chunk 顺序、chunk 延迟、总延迟、timeout、cancelled、truncated、SHA-256、输出大小、编码异常、permission denied 和 command not found。

## 7. KBD Fixture Compiler

### 7.1 输入和输出

输入是不可变 KBD revision 的 `signals_json` v2：`schema_version`、`signals[]`、`id`、`acquire.tool`、`acquire.args`、`match`、`orchestrate.requires`、`orchestrate.produces`、`review`、`provenance`。

Compiler 产生：

- `scenario_manifest`；
- `acquisition_template`；
- `command_fingerprint`；
- `variable_profile`；
- fixture draft；
- positive/negative witness；
- validation issues；
- source provenance。

### 7.2 witness 自动生成能力

| matcher | 自动 witness 能力 |
|---|---:|
| keyword | 高 |
| exists | 高 |
| regex | 中 |
| state | 中 |
| threshold | 中 |
| json_path | 中 |
| 组合 and/or/not | 中低 |
| 复杂业务日志 | 低 |

自动生成的 witness 只能是 draft，不能跳过人工确认。

### 7.3 三类主要 fixture

#### positive-minimal

用于验证 matcher 最小判定：

```text
2026-07-28 ERROR too many file descriptors
```

#### positive-realistic

用于验证 Agent 观察和报告能力，包含时间、组件、上下文、相邻日志和可能的 stderr。

#### negative/near-miss

用于验证删除关键字、相似日志、不同节点、正常状态、错误路径等场景不会误 PASS。

### 7.4 图片边界

图片和 OCR 结果可以帮助定位日志格式、生成 fixture 草稿、作为 provenance 和生成 realistic context，但不能直接作为机器 observation，也不能因为 OCR 提取到关键字就自动发布 fixture。

## 8. Fixture 数据模型

不修改 KBD 的核心 `signals_json` 来存放大量输出，建议增加独立模型。

### `agent_test_scenario`

```text
scenario_id
kbd_id
kbd_revision
signal_snapshot_hash
category_id
environment_profile
fixture_manifest_hash
status: draft/validated/published/retired
created_by
approved_by
created_at
updated_at
```

### `agent_test_fixture`

```text
fixture_id
scenario_id
acquisition_key
signal_id
variant: positive/negative/near_miss/error/timeout
command_match
target_host
container
exit_code
stdout_artifact_id
stderr_artifact_id
delay_profile
chunk_profile
expected_outcome
matcher_version
status
```

### `agent_test_run`

```text
run_id
test_run_id
scenario_id
trace_id
execution_mode
fixture_manifest_hash
expected_result
actual_result
status
duration_ms
metrics
created_at
```

小型 fixture 可以放 Git 管理的 YAML/JSON，大型日志和图片放对象存储，数据库保存 metadata 和 hash。不能把大段日志直接塞进 `kbd_entry` 行。

## 9. KBD 导入和日常使用流程

```text
导入 KBD
  → signals_json v2 抽取
  → KBD 人工审核
  → 生成测试场景草稿
  → 自动生成 witness
  → 人工补充 realistic/near-miss
  → fixture lint
  → 运行正向/负向/异常验收
  → validated
  → published
  → 纳入 CI/批量并发回归
```

KBD 修改后重新计算：

```text
hash(signals_json + matcher + tool schema + policy version)
```

如果 hash 变化，原 fixture 自动变为 `stale`，不能静默复用。

## 10. 并发场景调度器

调度器不直接调度命令，而是调度 scenario：

```text
TestRun
  → ScenarioLease
    → Agent Conversation
      → Bridge Session
        → hci-sim target
```

100+ 场景可以按 category、fixture manifest hash、目标软件版本、stateful/stateless、预期延迟和资源配额分片。同一批次不应让所有场景同时加载完整大日志，fixture artifact 采用按需加载和只读缓存。

## 11. 完整可观测性

模拟运行复用真实字段，并增加：

```text
simulation=true
execution_mode=sim-ssh
test_run_id
scenario_id
fixture_id
fixture_variant
fixture_manifest_hash
simulation_backend=hci-sim
```

完整关联：

```text
trace_id
  → exec_id
    → fixture_id
      → scenario_id
        → kbd_id/revision/signal_id
          → evaluation_id
            → candidate state
              → conclusion level
```

至少记录 scheduler 选择理由、command fingerprint、fixture 命中/未命中、stdout/stderr 大小与 hash、matcher 版本、scenario lease、candidate state transition 和 Conclusion Gate stop reason。

## 12. 安全边界

- `replay` 只允许 dev/test namespace；
- hci-sim 不允许访问生产 HCI；
- NetworkPolicy 只允许 terminal-bridge 访问 SSH；
- fixture 只读；
- 未发布 fixture 不能被批量执行；
- scenario lease 由后端签发，浏览器不能自行选择任意 fixture；
- fixture lint 扫描密码、token、私钥和真实客户数据；
- unknown command fail closed；
- 每个 scenario 有超时和资源限额；
- 测试 namespace 和数据有生命周期清理；
- 模拟 trace 不能混入生产成功率统计。

## 13. 变异测试和防止自洽假通过

如果 fixture 完全由 matcher 自动生成，可能只证明 matcher 和 fixture 互相自洽。因此必须支持 mutation：删除关键字、修改数字阈值、改变大小写、移动关键字到 stderr、改变节点或路径、插入相似日志、删除 producer 输出、注入 timeout/permission denied、替换命令参数和打乱执行顺序。

变异后如果结论不变且预期应变化，则测试失败。

## 14. 方案比较和投入产出比

| 方案 | 工作量 | 保真度 | 并发能力 | 维护成本 | 结论 |
|---|---:|---:|---:|---:|---|
| Bridge 直接按字符串返回 | 3～7 人日 | 低 | 高 | 高 | 仅做协议测试 |
| Bridge replay backend | 8～15 人日 | 低中 | 高 | 中 | 快速回归 |
| hci-sim SSH + fixture router | 20～35 人日 | 中高 | 高 | 中 | 推荐核心 |
| hci-sim + compiler + CLI | 30～45 人日 | 高 | 高 | 中低 | 推荐 MVP |
| hci-sim + UI/审批/CI | 45～65 人日 | 高 | 高 | 低 | 产品化 |
| 完整 HCI 虚拟化 | 80～150+ 人日 | 最高 | 低中 | 很高 | 不建议首期 |

首期基于现有 Bridge、KBD v2、qfk/qkv、CDD 和可观测性能力，MVP 估算 30～45 人日；两个人并行约 3～5 周，一个人约 6～9 周；完整 UI、审批、趋势和变异测试版本约 55～80 人日。

## 15. 分阶段实施建议

### P0：技术 Spike（3～5 人日）

验证 1 个 KBD、1 个 scenario、hci-sim SSH、Terminal Bridge、Agent/CDD、trace/evidence 的最小闭环。Windows Bridge 和 WSL Bridge 都应连通同一 hci-sim，并验证 `trace_id -> exec_id -> fixture_id -> evaluation_id`。

### P1：MVP（30～45 人日）

共享 hci-sim、100+ 逻辑场景、fixture manifest、compiler 草稿、CLI runner、positive/negative/near-miss/error、K3s 配额和 10～20 个核心 KBD。

### P2：规模化运营

Scenario Scheduler、Stateful Worker、fixture UI 和审批、批量并发测试、mutation testing、趋势报表、stale 管理和 100～1000 场景持续回归。

## 16. 验收门禁

在宣称“Agent 具备端到端测试覆盖”之前，至少满足：

- 核心 KBD fixture 覆盖率 ≥ 95%；
- positive/negative/near-miss 验证通过率 100%；
- 100+ 场景并发时无 trace、exec、artifact 串线；
- unknown fixture 不返回成功；
- ERROR/BLOCKED 不被转成 PASS；
- CDD 候选全集和 Conclusion Gate 语义不因模拟模式改变；
- 运行结果可回到 KBD revision 和 fixture manifest hash；
- 模拟测试和真实 smoke 测试的字段、协议、审计链一致；
- 至少一组 producer/consumer 多信号案例通过；
- 至少一组 timeout、权限、stderr、大输出案例通过；
- mutation testing 能检测关键证据缺失。

## 17. 最终建议

采用：

```text
KBD Fixture Compiler
  + 共享 hci-sim SSH
  + scenario 调度和逻辑隔离
  + Terminal Bridge 透明接入
  + positive/negative/near-miss/error fixture
  + 完整 trace/evidence 关联
```

不要把 KBD 规则硬编码到 Terminal Bridge，也不要为每个 KBD 启动一个完整 HCI 集群。100+ 并发的关键不是复制 100 个 HCI，而是构建一个具备场景隔离、fixture 路由、状态租约和资源配额的仿真运行时。

## 18. 2026-07-30 运行时技术定版

### 18.1 hci-sim 是容器化服务，但场景不是容器

推荐将 hci-sim 实现为一个独立的 **Go 服务**，构建为 OCI 镜像并以 K3s Deployment 运行。这里有两个容易混淆的层级：

- hci-sim 进程/Pod 是承载模拟能力的运行时；
- KBD 场景是运行时内部的逻辑租户，不默认映射为 Pod。

因此，100 个 KBD 场景的常态拓扑是 2～4 个共享 hci-sim Pod 承载 100～200 个逻辑 Scenario Lease，而不是创建 100 个 Pod，更不是创建 100 套 HCI。

推荐仓库结构：

```text
hci_sim/                         # Go module，与 terminal_bridge 同级
├── cmd/hci-sim/                 # 主程序
├── internal/sshserver/          # SSH 协议、认证与 session
├── internal/lease/              # Scenario Lease 校验
├── internal/command/            # argv 解析与 canonical fingerprint
├── internal/fixture/            # manifest、router、artifact reader
├── internal/runtime/            # worker pool、配额、state overlay
├── internal/telemetry/          # OTel、指标、结构化日志
└── testdata/                    # 仅非敏感测试 fixture

deploy/helm/hci-sim/             # 独立 Helm release，不与生产 chart 默认耦合
```

### 18.2 为什么运行时选择 Go

| 判断维度 | Go | Python/FastAPI | OpenSSH + Shell 脚本 |
|---|---|---|---|
| 自定义 SSH Server | `x/crypto/ssh` 可直接控制认证、channel、env、exec | 生态和性能不如 Go 直接 | SSH 真实，但命令路由/租户隔离容易散落到脚本 |
| 100+ 长连接与流式输出 | goroutine、channel、context 很适合 | 可以实现，但需更谨慎控制事件循环和阻塞 I/O | 进程模型开销更高，取消和统一观测困难 |
| 与 Terminal Bridge 共享经验 | 同语言、同 OTel/SSH 生态 | 只能共享协议 | 共享很少 |
| 镜像与启动 | 单二进制、小镜像、启动快 | 依赖较多 | 需要 sshd、用户和 shell 环境 |
| 安全边界 | 不提供通用 shell，只实现允许的协议子集 | 可实现 | 容易误暴露任意 shell |

结论：数据面运行时使用 Go。Fixture Compiler、Scenario API、批量 Scheduler 属于控制面，后续可使用项目已有的 Python 3.12 + FastAPI 技术栈。P0 不必先做完整控制面，可由静态 manifest loader + 测试 CLI 签发固定短租约，先验证协议闭环。

### 18.3 不运行完整 OpenSSH 和真实 Bash

hci-sim 应实现 SSH 协议兼容端点，而不是在容器中启动一个拥有真实系统权限的 sshd。它只接受以下受控 channel request：

- `session`；
- 受控的 `pty-req`，只用于复现 PTY 行为，不映射宿主终端；
- allowlist 内的 `env`；
- `exec`；
- P1 以后按需支持受控 `shell`，默认关闭。

收到 `exec` 后，将 shell 文本解析为 argv/AST，只允许已注册命令族、参数 schema 和安全管道组合。禁止把输入交给 `/bin/sh -c`。这样既保留 Bridge 的 SSH、PTY、stdout/stderr、exit-status、timeout 和 cancel 语义，又不会把 hci-sim 变成集群内任意命令执行入口。

## 19. 实际部署位置与 K3s 拓扑

### 19.1 dev 首选部署位置

P0/P1 部署在当前 WSL Ubuntu 的 K3s 中，但使用独立 namespace：

```text
hci-sim-dev
```

不直接放进 `hci-dev`，原因是：

1. hci-sim 是测试基础设施，不是生产业务服务；
2. 独立 namespace 才能明确 NetworkPolicy、ResourceQuota、Secret 和生命周期；
3. 模拟 Trace/指标需要与真实诊断统计隔离；
4. 当前 dev GitOps 应用存在 live drift 且 self-heal 暂停，P0 不应扩大现有漂移面；
5. hci-sim 故障、清理或扩容不应影响真实 Terminal Bridge P0 链路。

未来 staging 使用 `hci-sim-staging`。prod 不创建 hci-sim namespace，Helm/GitOps 默认 `enabled=false`；任何 prod 启用请求必须经过独立安全评审。

### 19.2 推荐资源拓扑

```text
WSL K3s
├── namespace: hci-dev
│   ├── customer-ui
│   ├── conversation-service / agent-service
│   └── terminal-bridge (cluster mode)
│
├── namespace: hci-sim-dev
│   ├── hci-sim Deployment, P0=1 replica, P1=2～4 replicas
│   ├── hci-sim ClusterIP Service, TCP/2222
│   ├── Scenario API/Scheduler, P1 引入
│   ├── ConfigMap, 仅放小型配置和 P0 manifest
│   ├── Secret, 仅放 lease 签名密钥/内部认证，不放真实 HCI 凭据
│   ├── emptyDir, 每 Pod 有界只读缓存
│   ├── ResourceQuota / LimitRange
│   └── default-deny NetworkPolicy + 精确 allow rules
│
└── namespace: observability
    ├── Tempo / Loki / Prometheus / Grafana / Alloy
    └── OTLP receiver

集群外
├── Windows terminal_bridge.exe（人工兼容性 smoke，可通过受控端口访问）
└── hci-real（仅 L3 Oracle；hci-sim 无网络权限和凭据）
```

SSH 数据面使用 ClusterIP `hci-sim.hci-sim-dev.svc:2222`，不通过 HTTP Ingress。管理 API 可以使用 ClusterIP HTTP 端口，只有确需人工访问时才增加受认证的内部 Ingress。Windows Bridge 的 smoke 测试通过显式、短期、受控的端口转发或内网入口，不把 SSH Service 永久暴露为任意网页可达入口。

### 19.3 存储策略

ConfigMap 有大小限制，不适合承载大量真实日志。推荐分层：

| 数据 | 存储 | 原因 |
|---|---|---|
| P0 单案例小型 manifest | ConfigMap 或镜像内 testdata | 简单、可重复 |
| fixture metadata、状态、审批、hash | PostgreSQL | 可查询、可审计 |
| 大 stdout/stderr、图片、原始 Artifact | S3/MinIO 等对象存储 | 流式读取、内容寻址、避免数据库膨胀 |
| 编译后只读 fixture bundle | 对象存储/OCI artifact，以 manifest hash 寻址 | 不可变、可缓存、可回滚 |
| Pod 本地缓存 | 有上限的 `emptyDir` | 加速热点，不成为权威数据源 |
| 场景 overlay | P0 内存；P1 Redis/CAS 或小型外部状态 | 多副本可恢复、避免 Pod 粘滞依赖 |

运行时只按 hash 读取已发布 bundle，禁止修改 fixture。缓存使用 LRU、总字节上限和 checksum 校验。

## 20. real/sim 运行选择与防串轨机制

### 20.1 运行创建时一次性选择

`execution_mode` 必须在创建 `agent_test_run` 或生产诊断 run 时确定并持久化：

```text
ssh      = hci-real
sim-ssh  = hci-sim
replay   = 仅协议级快速测试
```

同一 `test_run_id/scenario_id` 的所有命令继承该模式。后续消息不能改变模式；如需比较 real 与 sim，应创建两个独立 run，并通过 `calibration_group_id` 关联。

### 20.2 三层强制门禁

1. **控制面门禁**：Scheduler 根据 run 模式签发不同目标和凭据；sim lease 只能得到 hci-sim Service。
2. **网络门禁**：hci-sim namespace 禁止访问真实 HCI 网段；真实凭据 Secret 不挂载到 hci-sim。
3. **数据门禁**：每条 Trace、Artifact、Evaluation 和报表都必须携带 `execution_mode`；聚合指标按模式分区。

`fixture_not_found` 是测试失败，不是“尝试真实环境”的信号。自动 fallback 会把可重复测试变成带生产副作用的非确定性运行，也会掩盖 fixture 覆盖缺口，因此必须 fail closed。

## 21. Scenario Lease 与透明 SSH 路由

### 21.1 为什么必须有 Lease

100 个场景可能执行完全相同的命令，仅靠 command 文本无法知道应该返回哪一份 fixture。路由身份必须在执行命令之前建立，并且不能由浏览器任意伪造。

推荐 lease 载荷：

```json
{
  "lease_id": "opaque-id",
  "test_run_id": "run-...",
  "scenario_id": "scn-...",
  "fixture_manifest_hash": "sha256:...",
  "environment_profile": "hci-6.11.1_R1-acli-1.0.0",
  "execution_mode": "sim-ssh",
  "issued_at": "...",
  "expires_at": "...",
  "max_sessions": 2,
  "max_commands": 40,
  "signature": "..."
}
```

浏览器只中继不透明 token。P0 可将短时签名 token 作为 SSH password，固定 username 为 `sim`；hci-sim 在认证回调中校验签名、时效、mode、manifest hash 和配额，然后把 scenario identity 绑定到 SSH connection context。token 不写日志、不进入 Artifact。P1 可升级为短期 SSH certificate/principal，接口语义不变。

不要把 `scenario_id` 拼进 aCLI 命令，也不要让 Terminal Bridge 解析 KBD。Terminal Bridge 只需要像处理普通 SSH 凭据一样透明传输 lease credential，并记录非敏感的 `lease_id`/mode。

### 21.2 命令路由键

认证完成后，hci-sim 使用下列权威复合键：

```text
test_run_id
+ scenario_id
+ virtual_node_id
+ container
+ acquisition_key
+ canonical_command_fingerprint
+ fixture_variant
```

其中 `canonical_command_fingerprint` 基于解析后的 argv、标准化参数和受控环境计算，不直接对原始字符串做模糊包含匹配。命令未知、参数越界、manifest hash 不一致或 fixture 未发布时返回结构化 `fixture_not_found`/`policy_denied`，exit code 非 0。

### 21.3 Trace 跨 SSH 传播

SSH 没有自动传播 W3C Trace Context。为保证 hci-sim Span 成为 Bridge SSH Span 的子节点，Terminal Bridge 在建立 exec channel 时通过 SSH `env` request 发送 allowlist 字段：

```text
TRACEPARENT
TRACESTATE（可选）
HTP_EXEC_ID
HTP_TEST_RUN_ID
```

hci-sim 只接受这些字段并严格校验格式，从 `TRACEPARENT` 提取父上下文。该改动是通用可观测性协议，不包含 KBD 业务逻辑，并会自然复用到 Windows EXE 和 K3s Pod 的同一套 Terminal Bridge 代码。

## 22. 100+ 并发的真实实现

### 22.1 不是“把 100 个 KBD 合并为一个全局环境”

一个全局命令表同时满足 100 个 KBD 会产生歧义：两个 KBD 可能对同一命令要求相反输出，变量名和 PID/HOST 也会冲突。正确做法是把同一 hci-sim 集群划分成 100 个逻辑 universe；它们共享只读程序和 fixture corpus，但不共享路由 namespace、变量、状态、会话、Trace 或结果。

每个逻辑 universe 至少包含：

```text
ScenarioContext
├── test_run_id / scenario_id
├── immutable fixture_manifest_hash
├── virtual nodes / containers
├── variable profile
├── selected fixture variant
├── state overlay（仅有状态场景）
├── SSH connection/session quota
├── trace/exec/artifact namespace
└── deadline / output / command budgets
```

### 22.2 Pod 内并发模型

每个 SSH connection 可以由轻量 goroutine 处理协议，但命令执行必须进入有界调度器：

```text
SSH session
  -> auth/lease validation
  -> parse + policy check
  -> per-run quota
  -> per-scenario semaphore
  -> bounded global queue
  -> fixed worker pool
  -> fixture stream/chunk scheduler
  -> stdout/stderr + exit-status
```

强制约束：

- bounded queue，满时返回明确 `sim_overloaded`，不无限堆积；
- global semaphore 限制 Pod 总执行数；
- per-scenario semaphore 防止单场景饿死其他场景；
- per-run command/output/time budget 防止失控 Agent 消耗整个集群；
- 每命令 context deadline 和取消传播；
- stdout/stderr 流式发送，不把大输出一次性加载进内存；
- 每个 chunk 有顺序号，可注入可重复的 delay，但随机种子写入 run；
- 所有队列长度、拒绝数、执行时延和在途数可观测。

### 22.3 多副本与状态

P0 单副本只验证契约。P1 采用 2～4 副本：

- Stateless fixture：任何 Pod 均可处理，Service 正常负载均衡；
- 同一 SSH connection：TCP 天然固定在同一 Pod；
- 需要跨重连的轻量状态：以 `test_run_id/scenario_id` 为 key 存入 Redis/CAS，使用 TTL 和 compare-and-set；
- 大型或强状态场景：Scheduler 分配 `hci-sim-shard-N`，Terminal Bridge 在 lease 创建时得到固定 shard target；
- 不依赖仅存在于某个 Pod 内存且无法恢复的隐式状态。

### 22.4 初始容量假设

以下是需要压测验证的起始配置，不是已经验收的事实：

| 项目 | P0 | P1 起始值 |
|---|---:|---:|
| hci-sim replica | 1 | 3 |
| 每 Pod worker | 4 | 8～16 |
| 每 worker 同时流式 command | 16 | 16～32 |
| active scenario | 1 | 100～200 |
| 每场景并发 command | 1 | 默认 1，最大 4 |
| Pod CPU request/limit | 250m / 1 | 500m / 2 |
| Pod memory request/limit | 256Mi / 512Mi | 512Mi / 1Gi |
| 单命令输出预算 | 由 fixture 声明 | 默认 4MiB，专项上调 |

验收按 1、10、50、100、200 梯度进行，记录 p50/p95/p99、队列深度、拒绝率、内存峰值、Go GC、SSH session、命令吞吐和串线数。目标容量必须以压测结果修订，不能用理论 goroutine 数宣称完成。

### 22.5 100+ Agent 测试不等于 100 个浏览器窗口

hci-sim 可以承载 100+ SSH 场景，但整链路并发还受到 Agent Pod、conversation-service、Terminal Bridge、LLM 配额和浏览器协议中继限制。建议拆成两类：

1. **Golden UI E2E**：1～10 个真实浏览器/Playwright context，验证 Custom UI 渲染、WebSocket 和交互；
2. **Bulk Agent E2E**：headless test runner 实现与 Custom UI 相同的命令中继/`exec-result` 协议，驱动 100+ conversation，通过同一 cluster Terminal Bridge 和 hci-sim，验证 Agent 与执行链路容量。

如果验收目标明确要求 100 个真实浏览器上下文，可用 Playwright 共享浏览器进程并创建隔离 context，但这属于前端负载测试，不能把浏览器资源消耗归因于 hci-sim。两类结果分别报告，不能用 headless 结果宣称 100 浏览器 UI 已通过。

## 23. KBD 如何快速转为可并发场景

### 23.1 编译而不是手工复制

Fixture Compiler 的输入是不可变 KBD revision、`signals_json`、工具 schema 和经过审查的真实 Artifact。输出是内容寻址的 fixture bundle：

```text
KBD revision
+ signal acquire/matcher/requires/produces
+ 真实 HCI Artifact（可选但 positive-realistic 推荐）
+ 脱敏规则版本
+ fixture policy 版本
  -> compile
  -> positive-minimal
  -> positive-realistic
  -> negative / near-miss
  -> timeout / permission / unknown
  -> manifest hash
```

发布后，100 个场景只需引用不同的 manifest hash/variant 和 variable profile，无需复制运行时。大型输出在多个场景间按内容 hash 只读复用。

### 23.2 P0 Golden Scenario

当前 dev 环境最适合的首个 Golden KBD 是 `support_id=27123`，而不是尚未在 dev 发布数据中找到的概念示例 `too many file`。它已经具备三段 producer/consumer 信号：

```text
sig_001: qkv_task                  -> VM / HOST / END
sig_002: qfk_system, acli lsof     -> PID
sig_003: qfk_system, acli ps       -> CMD
```

已经完成真实链路验收的 Trace `c6acbb7bdf5faffaedf5a6faa04eeb97` 可作为 Golden Fixture provenance，来源命令覆盖 task、node list、lsof、ps，并包含 stdout/stderr 和 exit code。生成 fixture 时仍需脱敏、参数化、记录 Artifact hash，并补充 negative、near-miss、unknown 和 timeout 变体；不能直接把生产 Artifact 当成可编辑文本复制。

P0 成功标准不是“命令返回了一段看起来像真的文本”，而是同一 Agent/CDD 在 real 与 sim 两次独立 run 中得到契约等价的 acquisition、变量生产、Matcher 状态和证据链，且 sim run 可以稳定重复。

## 24. 真实 HCI 的 Oracle 校准职责

真实 HCI 不参与 100+ 日常回归，只承担以下低频职责：

- 采集 HCI/aCLI 新版本的真实 stdout、stderr、exit code、PTY、quote 和 chunk 特性；
- 验证模拟命令 fingerprint 与真实命令接受范围是否漂移；
- 生成或更新 positive-realistic 的受控来源 Artifact；
- 对 sim 与 real 的结果 envelope、filter、Matcher 和变量提取做 differential comparison；
- 在 fixture 发布前进行少量只读 smoke。

校准过程创建两个独立 run：

```text
calibration_group_id
├── run A: execution_mode=ssh, target=hci-real
└── run B: execution_mode=sim-ssh, target=hci-sim
     -> normalize nondeterministic fields
     -> contract diff
     -> approved / stale / blocked
```

真实 HCI 地址和凭据只能存在于受控 Secret/凭据管理系统，不写入文档、Git、fixture、日志或 hci-sim。Oracle 采集默认只读、命令 allowlist、人工确认、有超时和 Artifact 脱敏。hci-sim 的 Pod 和 ServiceAccount 永远不获得真实凭据。

## 25. 安全与可观测性落地

### 25.1 Pod 安全基线

- `runAsNonRoot: true`，监听 2222 而非特权 22；
- `readOnlyRootFilesystem: true`；
- `allowPrivilegeEscalation: false`；
- `capabilities.drop: [ALL]`；
- `seccompProfile: RuntimeDefault`；
- 不挂载宿主 Docker socket、containerd socket、SSH key 或 hostPath；
- fixture volume 只读，本地缓存使用独立、有配额的 `emptyDir`；
- 不提供任意 Bash、scp、sftp 或端口转发。

### 25.2 网络策略

默认 deny all，并只允许：

- 从带指定 namespace/pod label 的 dev/test Terminal Bridge 到 TCP/2222；
- hci-sim 到 DNS、OTLP receiver、只读 fixture API/对象存储；
- Prometheus/Alloy 按既有采集方式读取 metrics/logs；
- Scenario API 与内部数据库/Redis 的最小端口。

显式阻断真实 HCI 网段和其他 RFC1918 网段，避免模拟器成为内网 SSH 跳板。

### 25.3 观测字段

除真实链路已有字段外，模拟链路增加：

```text
simulation=true
execution_mode=sim-ssh
simulation_backend=hci-sim
test_run_id
scenario_id
lease_id（非 token）
fixture_id / fixture_variant / fixture_manifest_hash
canonical_command_fingerprint
virtual_node_id / container
queue_wait_ms / worker_id / shard_id
fixture_match_status
```

端到端关联必须达到：

```text
trace_id
  -> exec_id
    -> hci-sim command span
      -> fixture_id / artifact_id
        -> signal_id / KBD revision
          -> evaluation_id
            -> candidate state / conclusion
```

指标至少包含 active SSH sessions、auth/lease rejection、inflight commands、queue depth、overload rejection、fixture hit/miss、stdout/stderr bytes、timeout/cancel、per-variant latency、cache hit ratio 和 cross-scenario contamination（验收计数应为 0）。日志不记录 lease token、真实凭据和未脱敏完整输出；完整输出继续进入受控 Artifact。

## 26. 为什么不选其他方案

| 备选方案 | 不作为默认方案的原因 | 可接受的使用边界 |
|---|---|---|
| 直接使用真实 HCI 做全部回归 | 故障难构造、不可重复、并发污染、安全与资源成本高 | L3 Oracle、少量只读 smoke |
| 只用 hci-sim，取消真实 HCI | 模拟器会与真实 aCLI/SSH/PTY 漂移，可能产生自洽假通过 | 不可接受，必须保留 Oracle |
| Terminal Bridge 内按 command mock | 跳过 SSH 和远端协议，把 KBD 领域规则污染到 Windows/K3s Bridge | 单元/协议 replay，不计完整 E2E |
| 每个 KBD 一个 Pod | 资源、Service、Secret、启动和清理成本随案例线性增长 | 强状态/版本/网络隔离的少数场景 |
| 每个 KBD 一台 VM/完整 HCI | 保真度高但构造和并发成本极高 | 极少数系统级破坏性测试 |
| Docker-in-Docker/容器内 systemd | 安全边界差、需要特权、并不能真实复现 HCI | 不采用 |
| OpenSSH + 通用 shell + 脚本目录 | 容易产生命令注入、脚本漂移、观测碎片 | 仅一次性 Spike 也不优先 |
| 将 100 个 KBD 输出合并进一个全局命令表 | 同命令的相反期望、变量和状态必然冲突 | 不采用，必须按 Scenario Lease 隔离 |

## 27. 实施顺序与投入产出边界

### First Step：P0-0 契约与 Oracle Spike（先做，1～2 人日）

在写完整模拟器前先冻结一份 Golden Scenario Contract：

1. 选择 KBD 27123 和已验收真实 Trace；
2. 固化四条真实命令的 argv、stdout/stderr、exit code、变量 produces/requires、Matcher 预期和 Artifact hash；
3. 定义 `ScenarioLease v1`、`FixtureManifest v1`、unknown/timeout/error envelope；
4. 定义 Terminal Bridge 通过 SSH env 传播 `traceparent/exec_id` 的协议；
5. 产出脱敏的 positive-realistic、negative、near-miss fixture；
6. 写出 real/sim differential comparison 的允许差异和禁止差异。

这一阶段不部署 100 个场景，也不先做管理 UI。它验证“我们模拟的到底是哪个真实契约”，能最低成本消除后续最大返工风险。

### P0 Runtime Spike（随后 3～5 人日）

- Go 自定义 SSH Server；
- 单副本 K3s Pod 和独立 namespace；
- 固定/短期签名 lease；
- KBD 27123 三信号路由；
- stdout/stderr/exit/timeout/unknown；
- Windows 与 cluster Terminal Bridge 各一次 smoke；
- `trace_id -> exec_id -> fixture_id -> evaluation_id` 闭环；
- real/sim 两次独立运行的 contract diff。

### P1 MVP（30～45 人日）

- Fixture Compiler、Registry、不可变 bundle；
- Scheduler、headless runner、配额和 backpressure；
- 2～4 hci-sim replicas、Redis/CAS overlay；
- 10～20 个核心 KBD；
- 100 场景验收、200 场景压力测试；
- positive/negative/near-miss/error/mutation；
- Grafana dashboard 和 CI 门禁。

### P2 产品化（累计 55～80 人日）

- KBD 管理 UI、fixture 编辑/审批/stale 提示；
- Golden 数据治理、版本漂移检测；
- 强状态 shard、更多 HCI/aCLI profile；
- 100～1000 持续回归、趋势和成本报表。

投入产出判断：若 P0 不能证明 KBD 27123 在 real/sim 上 contract 等价且 Trace 完整，应停止扩展并修正协议；若 P0 通过，P1 的共享运行时会把新增 KBD 的边际成本从“构造一套真实故障环境”降低为“审核一组 fixture 与差异变体”，才具备继续投入的价值。
