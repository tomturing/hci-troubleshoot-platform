---
status: proposed
category: solution
audience: architect, developer, tester, operator
last_updated: 2026-07-28
owner: team
---

# Agent 并发测试与 KBD 驱动 HCI 仿真环境方案

> 对应需求：[Agent 并发测试与 KBD 驱动 HCI 仿真环境需求](../../requirement/events/2026-07-28-Agent并发测试与KBD驱动HCI仿真环境需求.md)

## 1. 决策摘要

当前 Terminal Bridge 已支持 Windows desktop 和 WSL/K3s cluster 两种运行形态，并具备 WebSocket、SSH、trace、stdout/stderr、超时、Artifact 和结果回传能力。现有方案可以扩展为 100+ 并发 Agent 测试，但必须增加“测试场景调度层”和“模拟 HCI 目标层”，不能仅在 terminal_bridge 中按命令返回固定文本。

推荐采用四层结构：

```text
KBD revision
  -> Fixture Compiler
  -> Scenario Registry / Scheduler
  -> hci-sim 多租户运行时
  -> terminal_bridge（保持透明）
  -> Agent/CDD 真实执行链路
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
┌────────────────────┐
│ KBD Revision       │
│ signals_json v2    │
└─────────┬──────────┘
          │
          ▼
┌────────────────────┐
│ Fixture Compiler   │
│ command fingerprint│
│ matcher witness    │
└─────────┬──────────┘
          │ manifest
          ▼
┌────────────────────────────┐
│ Scenario Registry/Scheduler │
│ 100+ logical scenarios      │
│ queue, quota, retry, shard  │
└─────────┬──────────────────┘
          │ scenario lease
          ▼
┌────────────────────────────┐
│ hci-sim Runtime             │
│ SSHD + fixture router       │
│ virtual files/commands      │
│ tenant/scenario namespace   │
└─────────┬──────────────────┘
          │ SSH
          ▼
┌────────────────────────────┐
│ terminal_bridge             │
│ WebSocket + SSH + OTel     │
└─────────┬──────────────────┘
          │
          ▼
┌────────────────────────────┐
│ Agent / Conversation / CDD │
│ matcher / evidence         │
└────────────────────────────┘
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
