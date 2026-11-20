---
status: active
category: solution
audience: developer
last_updated: 2026-11-20
owner: terminal_bridge
---

# terminal_bridge 重构审查与优化（2026-11-20，两轮对抗性审查归并）

> 本文归档 2026-11-20 对 terminal_bridge 重构线的**两轮深入审查**结论：
> 第一轮覆盖 `#1548`–`#1581`（方向、架构、复用、耦合），第二轮覆盖 `#1582`–`#1586`
> （U1 telemetry、logpush L1-L4、门禁与文档对账），并记录两项 P0 缺陷的修复。
>
> 与《2026-11-18-terminal_bridge架构重构设计》的关系：该文定义**目标**，本文记录
> **目标与落地之间的差距**、差距的业界定位、以及按 ROI 排序的收敛路线。两者不冲突，
> 本文是其实施后的独立审计。
>
> 全部数据实测自 `origin/main`（审查基线 `25f2e9bb`，修复分支 `fix/bridge-logpush-p0`），
> 不引用文档自述数字。

---

## 变更历史

| 日期 | 版本 | 变更摘要 |
|------|------|---------|
| 2026-11-20 | v1.0 | 首版：归并两轮审查；修复 P0-1（预算超限自死锁）与 P0-2（persistOK 单向闩锁 / 磁盘满静默丢弃），各附生产接线回归测试 |

---

## 一、审查范围与方法

### 1.1 覆盖范围

| 轮次 | PR | 主题 |
|------|----|------|
| 第一轮 | `#1548` `#1553` `#1554` `#1557` `#1566` `#1569` `#1573` `#1575` `#1577` `#1580` `#1581` | P0 止血、D1 插件回归浏览器、S0/S1 节点面原语、R0 目录搬迁、S1 云面 uplink、U0 UPX |
| 第二轮 | `#1582` `#1584` `#1585` `#1586` | U1 telemetry 门面、L1-L4 logpush 完备性、自动 exe、符号门禁与文档对账 |

### 1.2 方法

- **第一性原理**：不从"业界都这么拆"出发，而是先把桥拆到不可再分的约束——
  ① 交付物是客户机双击运行的单个 exe，零依赖、无灰度；② 对端是两个世界
  （平台 HTTPS/WS、客户 HCI 极简 SSH shell）；③ 历史事故全部是"该不该做/做坏了怎么办"，
  不是"做不到"。再从约束正向推导应有结构。
- **对抗性审查**：不验证"是否按设计写了"，而是构造失败条件——磁盘满、进程崩溃、
  平台长期不可达、并发配置、门禁空转——检查承诺是否仍成立。
- **取证纪律**：以代码事实为准。文档声称的数字（如"544 测试"）与实测（540/539）不符时，
  以实测为准并记录偏差。关键缺陷要求**可复现**，不接受静态推断结案。

### 1.3 本次实测基线

```
go test ./...              → 540 个测试全部通过（noop 构建）
go test -tags telemetry    → 539 个测试全部通过（集群构建）
go test -race ./internal/logpush → 通过
GOOS=windows/linux go build → 双平台通过
python3 scripts/check_bridge_imports.py → 11 条域间依赖，未新增
go tool nm（桌面构建）      → otel 符号 0，logpush 符号 124
go tool nm（telemetry 构建） → otel 符号 2099
分发产物                   → terminal_bridge.exe 3,130,368 B（UPX 压缩后）
```

---

## 二、业界范式对标

本节回答"这套设计在业界处于什么位置"，为后续判断提供参照系，而非用类比替代推导。

| 本设计的决策 | 业界对应范式 | 出处 / 同构系统 | 对照结论 |
|---|---|---|---|
| 单 exe 内做模块化，不拆服务 | **模块化单体**（Modular Monolith） | Spring Modulith、Shopify 单体拆分实践 | 方向正确：约束 ① 下唯一可行；但模块化单体的核心是**模块边界 + 状态所有权**，不是分目录 |
| 云面 / 节点面"两张脸" | **端口与适配器**（Ports & Adapters） | Alistair Cockburn 六边形架构 | 边界真实（对端协议/信任模型/失败模式不同），优于按"技术层"横切 |
| 四象限 pull/push/check/action | **声明式配置管理的 execution modules** | Ansible `copy`/`fetch`/`stat`/`command` | 语义分界正确；但本设计把 push 按"两张脸"再拆一次，比 Ansible 更严格 |
| reconcile 骨架 | **控制器收敛循环** | K8s controller、Salt state、Terraform plan→apply | 范式正确；本项目只落地了骨架，消费者未迁入（见 §5） |
| `Runner` 接口缝 | **依赖倒置的最小端口** | `io.Reader`、`http.RoundTripper`、`database/sql/driver` | 接口设计正确，但只被 `remote` 包自身与一处包装使用（见 §4-A4） |
| 注册式 WS 路由 | **微内核 + 插件注册** | `http.ServeMux`、Eclipse 插件注册表 | 只做了"表"，没做"分权"：路由表仍在装配层（见 §4-A5） |
| import 禁令 CI 扫描 | **架构约束即测试** | ArchUnit（Java）、import-linter（Python）、go-arch-lint | 方向正确，但规则集只覆盖一半（见 §10） |
| outbox 分段 + cursor | **WAL 分段 + checkpoint** | Kafka log segment、RocksDB WAL、etcd WAL | 结构同构；但本实现缺少"checkpoint 与段生命周期"的严格不变式（见 §8） |
| 至少一次 + event_id 幂等 | **at-least-once + idempotent consumer** | Kafka 消费语义、SQS FIFO 去重 | 语义选择正确；缺口在"丢失侧"而不是"重复侧" |
| gap 标记事件 | **丢失可观测化** | Kafka consumer lag / OpenTelemetry `dropped_*` 计数 | 思路正确（宁可知洞不静默），但 gap 自身的可达性未闭环（见 §7-P1-1） |
| telemetry 门面 + build tag | **门面 + no-op 实现** | OTel no-op provider、Go `log/slog` handler | 本轮做得最好的部分：签名零泄漏、双向符号门禁、阳性对照 |
| 门禁加阳性对照 | **变异测试思想** | mutation testing、canary assertion | 值得推广到其它门禁：只断言"禁止项不存在"容易被空转绕过 |

**范式层面的核心结论**：设计选型没有偏离业界主流，问题不在"选错范式"，而在
**范式只落地了骨架、没落地约束**——端口建了没走、微内核建了没分权、模块化了没交出状态。

---

## 三、大方向判断

### 3.1 五个正确决策

1. **进程内模块化而非拆服务**：由约束 ① 唯一决定，拆服务会让客户机多一个部署物。
2. **按"两张脸"切分而非按技术层**：云面（HTTP/WS）与节点面（SSH）的对端、协议、
   信任模型、失败模式完全不同，是真实边界。
3. **声明式 / 命令式分层**：历史事故（无备份 rm -rf、回滚误伤、探测伪装成功）都不是
   能力缺失，而是决策错误，必须由能力之上的层回答。
4. **R0 行为等价搬迁与 S 阶段逻辑收口二维分离**：在无灰度通道的前提下，混做会让任何
   回归无法归因。执行到位（测试数 528→531→540，零删除）。
5. **先止血再重构**：`#1548` 的 P0 修复（回滚前提、非原子写、幂等三态）先于架构调整。

### 3.2 一处根本性偏差

> **把"目录归位"当成了"边界建立"。**

模块化单体的边界由三件事共同定义，缺一不成立：

| 边界要素 | 状态 | 证据 |
|---|---|---|
| 谁不能 import 谁 | 部分成立 | 11 条域间依赖被"冻结基线"放行；业务域反向 import `internal/app` 11 处未被扫描 |
| 谁持有状态 | **不成立** | `app.Bridge` 仍是全局状态宿主，52 个导出函数以其为首参；`SftpTasks map[string]any`、`AcliCache any` 用类型擦除规避循环 |
| 谁负责路由 | **不成立** | 18 条路由注册表在 `cmd/bridge/ws_routes.go`（524 行，含 handler 实现），域不自注册 |

### 3.3 阶段完成度

| 阶段 | 状态 | 说明 |
|---|---|---|
| P-1 止血 | 完成 | `#1548` |
| U0 UPX | 完成 | `#1581`，16.47MB → 4.72MB 未压缩基线；当前分发 3.13MB |
| R0 搬迁 | 完成 | `#1575`，测试零丢失 |
| S1 原语 | **部分** | 节点面 `remote` 已收口；云面 `uplink.Push` 已统一；但 delivery 仍持具体 SSH 类型、logcollect 仍外借 50+ 符号 |
| S2 / S3 reconcile 消费者 | **未完成** | 骨架 + 77 例测试已建，`kinds/` 目录不存在，消费者逻辑内联在 logcollect/acli |
| S4 acli 两阶段 | **部分** | 两阶段协议已落地，声明式胶水未迁 kinds |
| U1 telemetry | 完成 | `#1582`，桌面 otel 符号 0 |
| L1 logpush 完备性 | 完成（**含 P0 缺陷，本次修复**） | `#1584` |
| D1 删除 relay | 完成 | `#1553` `#1554` |

---

## 四、架构层问题

### A1 Shared Kernel：`app.Bridge` 仍是上帝对象

**证据**

```
内部域反向 import internal/app：11 个文件
以 *app.Bridge 为首参的导出函数：logcollect 27 + acli 11 + sftp 10 + delivery 4 = 52
类型擦除缝：app/bridge.go 的 SftpTasks map[string]any、AcliCache any
裸类型断言：acli_package.go 的 b.AcliCache.(*acliPackageCache)
```

**业界定位**：这是典型的 **Shared Kernel** 反模式（Eric Evans, DDD）。模块化单体要求
"每个模块拥有自己的状态"（Spring Modulith 的模块自有数据、Shopify 的组件边界），
而 Shared Kernel 让所有模块共享一个可变状态容器——耦合度与拆分前完全相同，只是多了
一层包边界和一层类型断言。

**根因**：R0 是"纯搬迁"，而状态搬迁需要改函数签名（属于逻辑改动），因此被推后；
推后之后没有设定收敛期限，基线冻结机制又把"现状"固化成"合法"。

**影响**：任一域无法独立测试（16 处测试需构造完整 `Bridge`）、无法独立演进、
`Bridge` 字段改名波及 52 个函数；`any` 缝把编译期错误推迟成运行期 panic。

**修复范式**：状态所有权下沉。会话簿记抽 `sshx.SessionRegistry`（`Sessions`/`lastAuth`/
`sessionRefs`/`sessionIdleAt`），`SftpTasks`/`AcliCache` 归还各域自持（域内 `Handler`
结构体）。这是后续所有解耦的**总钥匙**。

### A2 横切关注点做成了具体依赖：logpush 被 20 个文件 import

**证据**：`internal/app`(3)、`sshx`(1)、`terminal`(2)、`acli`(2)、`logcollect`(5)、
`delivery`(3)、`sftp`(1)、`cmd`(3) 直接 import `internal/logpush`；缺一个 `Publisher`
端口。11 条冻结基线中 5 条由此产生。

**业界定位**：横切关注点必须依赖**接口**而非实现（Go 谚语 "accept interfaces"；
OTel 用 no-op provider 解耦，本项目的 `shared/telemetry` 已经做对了这件事）。同一个仓库里
telemetry 走了门面，logpush 却让 7 个包直接依赖——**同一范式在两条链路上执行不一致**。

**影响**：域单测必须构造 `logHub`；logpush 签名变更波及 7 个包；日志级别/采样等策略
无法按域替换。

### A3 `logcollect` 变成伪公共库

**证据**（非测试代码的实际引用）

| 消费方 | 借用符号 |
|---|---|
| `delivery` | `ShellQuote` `PullArchive` `FileDigest` `ErrString` `FirstNonEmpty` `EvidenceFor` `ProbeLogCollectArchive` `ResolveLogCollectSession` `ParseLogCollectProgress`（20+ 处） |
| `acli` | `ErrInstall*` 错误码表、`SpcNodeList` `SpcLocalAddresses` `SpcFanoutConcurrency` `RunSpcOnHostStreams` `BuildSpcSSHCommand`、`ParseAcliBinary`（30+ 处） |
| `sftp` | `ResolveLogCollectSession` `FirstNonEmpty` `ShellQuote` |

**业界定位**：这是"半拉子收口"——原语抽取从业务域往外抽，抽到一半停在 logcollect，
剩余一半让别的域反向依赖它。结果比不抽更糟：logcollect 既承担业务编排，又被迫承担
公共库职责，且它是四个域里体量最大的（7414 行）。设计文档 §3.5 明确否决"utils 垃圾抽屉"，
但垃圾抽屉只是换了宿主。

**修复范式**：把借出去的符号物归原主——会话解析 → `sshx`/`terminal`；摘要/探测/拉取 →
`remote`/`uplink`；错误码表 → 独立契约包（它是跨域契约，不是 logcollect 私有）；
SPC 逻辑 → `reconcile` 消费者（对应设计里的 `kinds/spc`）。

### A4 端口建了没走：delivery 全程持 `*sshx.SSHSession`

**证据**：`delivery_runner.go` 的 `sessionFor`/`requireSessionForPhase`/`waitCollect`/
`prepareDirect`/`ensureSourceDigest`/`pull` 等 10+ 处签名直接使用 `*sshx.SSHSession`，
自行拼命令、探测、拉包。

**业界定位**：Ports & Adapters 的价值在于**策略层只依赖端口**，这样可用 Fake 适配器离线
测试（`net/http` 的 `RoundTripper` 正是为此存在）。`remote` 包自己已经用 `fakeRunner`
做到了（47 例单测），但 delivery 没接。

**影响**：delivery（1888 行、81 例测试）无法离线验证编排逻辑，只能靠集成测试；
这也是它必须反向依赖 logcollect 的原因（借它的 sshx 包装）。

### A5 微内核退化成"搬家"：路由仍在装配层

**证据**：`cmd/bridge/ws_routes.go` 524 行，`init()` 里注册 18 条路由，**handler 实现
也在该文件**，并直接调用 `otel.Tracer`。设计 §3.2 要求 `cmd/bridge` "不含任何业务逻辑"。

**业界定位**：微内核 + 注册表的价值是"新增能力不改核心"。当前形态只是把 `main.go` 的
中央 switch 换成另一个中央文件——新增消息仍须改这个 524 行文件，收益只剩"路由表可单测"。

**根因**（代码自述）：域反向 import `app` 会成环，`app` 又 import `logpush`/`terminal`，
所以只能在装配层注册。**A1 解不开，A5 就解不开**。

---

## 五、模块层

| 域 | 目标职责（设计） | 实际状况 | 耦合出口 |
|---|---|---|---|
| `app` | 微内核，不认识业务语义 | 仍是状态宿主 + 生命周期 + 指标，`bridge.go` 524 行 | → terminal, logpush |
| `sshx` | 端口适配器，不含 WS 知识 | 1657 行，**0 测试**，含会话租约/keepalive/端口恢复/SFTP channel | → logpush |
| `terminal` | 交互终端、EXEC、VM console | 1469 行 / 5 例测试；仍被 logcollect/acli 借 `FormatOutputPreview`、PTY 常量 | → logpush |
| `acli` | 两阶段协议边缘，零编排 | 3119 行，编排仍内联；借 logcollect 错误码与 SPC 五件套 | → app, logcollect, logpush, terminal |
| `logcollect` | 采集编排 + 错误码 + 证据 | 7414 行（未拆），同时充当公共库 | → app, logpush, terminal, reconcile |
| `delivery` | 作业状态机，传输全委托原语 | 3439 行，仍持 `*sshx.SSHSession` | → app, logcollect, logpush |
| `sftp` | 下载，文件名主权 | 1030 行，单文件未三拆 | → app, logcollect, logpush |
| `logpush` | 证据通道 | 2354 行（本轮 +840） | → app 反向被依赖 7 包 |
| `shared/remote` | 节点面原语，只认识 Runner | 1339 行 / 47 例 | 健康 |
| `shared/uplink` | 云面原语 | 561 行 / 9 例；`Push` 3 处调用点 | 健康 |
| `shared/reconcile` | 声明式骨架 | 2279 行 / 77 例；消费者未迁入 | 健康但空转 |
| `shared/telemetry` | 门面 + 双实现 | 367 行 / 5 例；符号门禁双向 | **本轮标杆** |

**测试分布（关键空洞）**：`sshx` 0 例；`terminal` 5 例；`models`/`runtime` 无测试（可接受）；
其余域测试充足。**承载最高风险（会话生命周期）与被依赖最广（5 个域）的包，测试数为 0。**

---

## 六、目录与文件层

### 6.1 目标树与实际树差异

| 设计目标 | 实际 | 评估 |
|---|---|---|
| `shared/reconcile/kinds/{acli,spc,config}/` | **不存在**；`kinds.go` 只是 Kind 枚举，消费者内联 | 设计意图（骨架不认识消费者）靠 import 禁令部分成立，但"新增链路只加 DesiredState"未验证过 |
| `app/config.go` | `app/cfg.go` | 命名漂移，无害 |
| `delivery/store.go` | `delivery/delivery_local_store.go` | 搬迁未改名，与设计不一致 |
| 目标树未列 `shared/runtime` | 存在（30 行 env 助手） | 新增包未回写设计；"utils 不建"的判据被绕开 |
| 根目录无 Go 文件 | **5 个 `package main` 孤儿测试**（`remote_convergence_test.go` 等，无对应实现） | 跨域守卫归属不清；`sshx` 的行为测试滞留此处，掩盖了"sshx 零测试" |

### 6.2 巨型文件 TOP（实现）

| 文件 | 行数 | 问题 |
|---|---|---|
| `logcollect/log_collect.go` | 2255 | handler / 编排 / 进度解析混装 |
| `delivery/delivery_runner.go` | 1888 | prepare / pull / push / finalize 四相位混装 |
| `logcollect/log_collect_spc_fanout.go` | 1600 | 扇出 + 场景语义 + 证据 |
| `sshx/session.go` | 1298 | 连接 + 租约 + keepalive + 重连，**零测试** |
| `sftp/sftp.go` | 1030 | 设计要求三拆（handler/download/sanitize），未拆 |

### 6.3 命名与职责漂移

- `logcollect` 以业务域命名，实际被当作公共库（见 A3）；
- `app.Bridge` 以"桥"命名，实际是全局状态容器（见 A1）；
- `shared/runtime` 名为 runtime，实际是 env 读取助手——若继续扩张会变成新的 utils 抽屉。

---

## 七、代码细节缺陷清单

> 状态标记：【已修】本次修复并附回归测试；【未修】留待后续阶段。

### P0-1【已修】磁盘预算超限路径的自死锁

**位置**：`terminal_bridge/internal/logpush/outbox.go`（`enforceDiskBudgetLocked`）

**缺陷链**

```
enqueue → persistLocked（持 u.Mu）→ enforceDiskBudgetLocked（持 u.Mu）
  → Blog("WARN","log_upload.gap")
  → DefaultHub.publish → LogUpload.enqueue
  → u.enabled() → u.Mu.Lock()        ← 同一 goroutine 二次加锁，sync.Mutex 不可重入
```

`LogUpload` 是包级单例，`publish` 末尾无条件回归 `enqueue`；`levelEnabled` 默认放行 WARN，
无旁路。

**触发条件**：outbox 累计 > 8MB 且被丢段含未消费数据——即平台久不可达、弱网积压、
panic 风暴，**恰恰是最需要日志的场景**。

**后果**：不是丢日志，是桥整体挂死。`publish` 是所有域写日志的必经路径（20 个文件 import），
锁一挂，SSH 交互、日志收集、交付、插件同步全部阻塞。

**为何既有测试未发现**：`equivalence_test.go` 全部使用局部 `newLogUploader()`，而 `Blog`
走全局 `LogUpload`（测试中未配置、`enabled()` 直接返回）——锁不是同一把。**测试绕开了
生产接线**，这是比缺陷本身更值得记录的教训。

**修复**：`enforceDiskBudgetLocked` 不再调用 `Blog`，改 `fmt.Fprintf(os.Stderr, ...)`。
stderr 在桌面形态由 `redirectStderrToFile` 落进 `bridge-stderr.log`，再由 L3 补传通道
包装上报——既消除重入，又避免产生重复 gap 事件（Blog 那条会再占 outbox 预算）。
本地与平台两侧都留痕，链路不闭合也不丢。

**回归测试**：`internal/logpush/outbox_p0_test.go::TestGapReportDoesNotDeadlockOnDiskBudget`，
刻意使用**全局 `LogUpload` + 生产接线**（而非局部实例），5 秒超时判定死锁。
已做反向验证：把 `Blog` 改回去，该测试在 40 秒超时失败；改回修复后 0.10s 通过。

### P0-2【已修】`persistOK` 单向闩锁 → 无限重放；磁盘满时静默丢弃

**位置**：`terminal_bridge/internal/logpush/outbox.go`、`uploader.go`

**缺陷 1：单向闩锁**。`persistOK` 有 4 处置 `false`、0 处置回 `true`。一次瞬时
`ENOSPC`/`EIO`/句柄耗尽后：`advanceCursorLocked` 永久不执行（游标冻结），而
`refillFromDiskLocked` 不看 `persistOK` → 队列清空后从冻结游标把同一批数据反复读回、
反复发送，形成**无限重放**直到进程重启。磁盘满时桥进入"持续上传同一批 + CPU 打满"的活锁。

**缺陷 2：静默丢弃**。写盘失败且内存队列已满时，条目既没落盘也没进队列，代码只做
`u.spilled++`，既不计 `dropped` 也不发 gap——"零静默丢弃"在该分支上不成立。

**修复**

| 文件 | 改动 |
|---|---|
| `outbox.go` | `persistLocked` 改为返回 `persistOutcome`（`skipped`/`written`/`failed`），区分"未启用落盘"与"落盘失败" |
| `outbox.go` | 写成功后调用 `markPersistedLocked`：`persistOK` 复位，并把游标**钳回本条起点**（仅当游标仍指向活动段），避免游标越过故障期间的空洞而误删后续段 |
| `outbox.go` | `refillFromDiskLocked` 在 `!persistOK` 时直接返回（杜绝无限重放） |
| `outbox.go` | `enforceDiskBudgetLocked` 在 `!persistOK` 时跳过（磁盘已不可写，删段只会加剧错位） |
| `uploader.go` | 落盘失败且队列满 → `dropped++` + `reportLostLocked`（stderr，首条及每 100 条节流，经 L3 补传） |
| `uploader.go` | `/status` 新增 `persist_ok` 字段，现场可直接判断落盘链路健康度 |

**回归测试**（`outbox_p0_test.go`）
- `TestPersistFailurePausesRefill`：故障期间 refill 读回 0 条；写成功一次后 `persistOK`
  复位且能继续从游标读回未消费数据。
- `TestWriteFailureCountsAsDroppedWhenQueueFull`：已关闭句柄模拟磁盘满 + 队列填满，
  断言 `dropped+1` 且 `persistOK=false`。

### P1【未修】

| # | 位置 | 缺陷 | 后果 | 修复方向 |
|---|---|---|---|---|
| 1 | `outbox.go` `gaps.jsonl` | 只写不读，无轮转、不占预算、无上限 | 崩溃窗口内的 gap 永不上报；文件无限增长 | `recoverOutbox` 重放 gaps 并归档截断；纳入预算 |
| 2 | `seed.go` `SeedFromLocalLogs` | 无互斥：`configureWithSource` 的 `go` 与 worker 每轮可并发 | 重复入队；进度 `.tmp` 竞态写可能读到半截 JSON 导致全量重扫 | 加 `seedMu`；进度文件按 instance+pid 分文件 |
| 3 | `outbox.go` `persistLocked` | 无 `fsync` | 断电/硬崩溃丢最后若干条，"重启后仍在磁盘"只在 OS 刷盘后成立 | 关键点 fsync，或明确降级语义并写入文档 |
| 4 | `outbox.go` `enforceDiskBudgetLocked` | 每条日志一次 `ReadDir` + N 次 `Stat` + 读 `cursor.json` | publish 热路径 IO 放大，慢盘客户机被拖慢 | 节流（每 N 条/每 T 秒） |
| 5 | `app/lifecycle.go` stderr | `bridge-stderr.log` 永久 `O_APPEND`，无轮转、不计预算 | 长期运行无限增长，与 outbox 8MB 预算不共享 | 纳入磁盘预算或按大小轮转（轮转后水位与 EventID 需重算） |
| 6 | `seed.go` `mergeSeedLocked` | 逐条 `sort.Search` + `copy`，O(n²) | 5000 条补传时明显耗时，挤占 2s 预算 | 批量收集后一次排序合并 |
| 7 | `uploader.go` `/status` | 缺 `gaps`/`seed_pending`/`cursor` | 现场无法判断"gap 是否已上报、补传是否卡住" | 补齐字段（`persist_ok` 本次已加） |

### P2【未修】

| # | 问题 | 说明 |
|---|---|---|
| 1 | `#1586` docs-governance 红 | 27 条阻断级失效引用，全部来自 `docs/plans/2026-11-18-节点同步链路统一收口设计.md` 指向 R0 已删除的旧路径。该 PR 自称"两文档终局对账、逐条核对代码事实"，实际只追加版本行 |
| 2 | 部署文档失真 | `docs/deploy/部署设计.md` 仍写"默认只扫 2 个文件、最多 5000 条"，已被 L1 推翻 |
| 3 | 新旋钮未纳契约 | `HCI_BRIDGE_LOG_OUTBOX_MAX_BYTES` 仅在代码与 README，未登记 `deploy/config/config-contract.yaml` 与 Helm（同批 seed 旋钮已登记） |
| 4 | 测试数口径 | 文档称 544，实测 540（noop）/539（telemetry） |
| 5 | U0 加壳风险未闭环 | 文档承诺"发布前抽样验证主流杀软"，无验证记录；企业 EDR/AppLocker 是真实拦截面 |
| 6 | `kinds/` 未建的解释 | v2.6 将"未建 kinds"表述为"设计意图均满足"，属事后合理化；应显式记为债务 |

---

## 八、可靠性设计专项（L1-L4 语义审查）

### 8.1 承诺 vs 实际

| 设计承诺 | 实际 | 结论 |
|---|---|---|
| 本地保留窗口内条目至少一次必达 | 正常路径成立；**磁盘满 + 队列满时静默丢**（P0-2，本次已修） | 修复后仅在"落盘与网络同时不可用"时丢，且有 gap/stderr 留痕 |
| 零静默丢弃 | 修复前不成立（两处静默）；修复后成立 | 需保留回归测试 |
| 平台可知洞 | gap 走内存队列可上报；但 **gaps.jsonl 崩溃后不可重放** | 部分成立（P1-1） |
| 重启按游标恢复不 truncate | 成立；但游标与内存的字节对应依赖"重新序列化 == 原写入字节" | 脆弱：`Extra` 中大整数经 `any` 往返可能改变字节长度，导致游标轻微错位（概率低，建议改为写盘时记录字节数） |

### 8.2 失败模式矩阵

| 失败场景 | 数据是否丢 | 平台是否可知 | 系统是否可用 |
|---|---|---|---|
| 平台短暂不可达 | 不丢（内存+磁盘） | 是（恢复后补发） | 可用 |
| 平台长期不可达（>8MB 积压） | 丢最旧段 | 是（gap） | **修复前：整桥挂死；修复后：可用** |
| 进程被 kill | 不丢（磁盘段+游标） | 是 | 可用 |
| 断电 | 可能丢最后若干条（无 fsync） | 否 | 可用 |
| 磁盘满 | **修复前：无限重放 + 静默丢；修复后：有界丢弃 + 留痕** | 修复后是 | 修复前：活锁 |
| 并发 configure（WS+env） | 重复上传（幂等去重） | 是 | 可用 |
| stderr 文件无限增长 | 磁盘压力 | 否 | 可用（长期风险） |

### 8.3 与业界 outbox 实现的差距

Kafka/RocksDB 的 WAL 有两条本实现缺失的不变式：

1. **段文件只在 checkpoint 之后删除**，且 checkpoint 单调推进、崩溃后可重放——本实现
   近似成立，但 `advanceCursorLocked` 在"段文件意外消失"时会跳到下一段（放弃该段剩余数据），
   属于静默降级。
2. **写入即持久（fsync 或 group commit）**——本实现没有，属于"性能换持久性"的知情取舍，
   但文档未写明，容易被误读为"掉电也不丢"。

---

## 九、可观测性审查

| 信号 | 归属 | 评价 |
|---|---|---|
| Traces | `shared/telemetry` 门面 + build tag 双实现 | **标杆**：签名零 otel 类型、nm 双向门禁、传播解析用纯标准库保证 logpush 不随 tag 剥离 |
| Metrics | `app/metrics.go` | 可用；logpush 侧缺"重放/丢弃/游标"指标 |
| Logs·本地 | `logpush/hub.go` | 可用；`bridge-stderr.log` 无轮转 |
| Logs·证据 | `logpush` 直传 + L1-L4 补传 | 本轮补强；缺口见 §7-P1 |

**`/status` 现状**：`enabled`/`queued`/`dropped`/`spilled`/`instance`/`outbox`/`source`，
本次新增 `persist_ok`。仍缺：`gaps`（已上报缺口数）、`seed_pending`、`cursor`（当前
段与偏移）、`replayed`（重放次数）。这些是判断"投递是否真的健康"的必要信号。

**一致性小问题**：`dropped` 在持锁路径用普通 `+=` 写，`LogUploadStats` 用
`atomic.LoadUint64` 读——混用 atomic 与 mutex 不构成 happens-before，严格说是数据竞争，
建议统一为锁内读或全原子。

---

## 十、质量门禁审查

### 10.1 已有门禁（有效）

| 门禁 | 位置 | 有效性 |
|---|---|---|
| gofmt / vet / test / race | CI terminal-bridge job | 有效 |
| import 禁令（4 条规则 + 11 条冻结基线） | `scripts/check_bridge_imports.py` | 部分有效 |
| 桌面零 otel + 集群阳性对照 | CI | **范本**：双向断言防门禁空转 |
| 桌面含 logpush 符号 | `#1586`（待合并） | 有效（防证据通道被误剥） |
| UPX ≤5MB fail-closed + 清单 sha256 | CI | 有效 |
| docs-governance 失效引用 | CI | 有效（`#1586` 正被它拦下） |

### 10.2 盲区

1. **未禁止"业务域 → app"**：11 处反向依赖全部通过扫描。
2. **未禁止"业务域 → sshx 具体类型"**：delivery 的 10+ 处不被拦。
3. **基线无 sunset**：11 条冻结边只减不增，但没有到期机制，现状容易被永久合法化。
4. **测试可绕过生产接线**：P0-1 正是这样逃逸的。门禁只保证"函数级测试绿"，不保证
   "经装配层的集成路径绿"。
5. **文档门禁只查"引用的文件是否存在"，不查"描述是否过时"**：部署文档的"2 文件/5000 条"
   仍能通过。

---

## 十一、度量基线（修复后）

| 指标 | 数值 | 备注 |
|---|---|---|
| 实现代码 | 43,427 行（含测试） | 修复前 43,427，本次净增约 90 行（含测试） |
| 测试函数 | 540（noop）/ 539（telemetry） | 本次 +3 |
| 分发 exe | 3,130,368 B | 16.47MB → 3.13MB，-81% |
| 桌面 otel 符号 | 0 | 集群 2099 |
| 桌面 logpush 符号 | 124 | 证据通道保留 |
| 域间依赖边 | 11 | 未新增 |
| 最大实现文件 | 2255 行（`log_collect.go`） | 未变 |
| 零测试包 | `sshx`（1657 行）、`models`、`runtime` | 未变 |

---

## 十二、优化路线图

### 阶段 0：止血（本次已完成）

- 修 P0-1 自死锁（含生产接线回归测试 + 反向验证）
- 修 P0-2 `persistOK` 复位 / refill 暂停 / 静默丢弃留痕
- `/status` 暴露 `persist_ok`

**验收**：双 tag 全量测试绿、race 绿、双 GOOS 绿、禁令 11 条未新增；缺陷回退时新测试必失败。

### 阶段 1：可靠性补齐（下一 PR，1 周内）

1. `gaps.jsonl` 重放 + 归档截断 + 纳入预算（P1-1）
2. `SeedFromLocalLogs` 互斥（P1-2）
3. `enforce` 节流（P1-4）
4. stderr 轮转/预算（P1-5）
5. `/status` 补齐 gaps/seed_pending/cursor；`dropped` 读写口径统一

**验收**：四个崩溃场景（进程 kill / 断电模拟 / 磁盘满 / 平台长不可达）下，
"收到的集合 ∪ gap 区间"覆盖本地窗口；`/status` 能独立判定链路健康。

### 阶段 2：架构解耦（真正的总钥匙）

1. **拆 `app.Bridge`**：会话簿记 → `sshx.SessionRegistry`；`SftpTasks`/`AcliCache` 归还各域；
   消除 52 个函数首参与 11 处反向 import。
2. **logpush 接口化**：定义最小 `Publisher` 端口，装配层注入；基线从 11 条降到 ≤3 条。
3. **logcollect 瘦身**：符号物归原主 + 错误码独立契约包 + SPC 迁 reconcile 消费者。
4. **delivery 接 Runner**：`*sshx.SSHSession` 换成端口 + FakeRunner 编排单测。
5. **门禁加固**：新增"域→app""域→sshx 具体类型"两条规则；基线加 sunset。

**验收**：`grep internal/app internal/*/` 为 0；`grep internal/logpush` 在域内为 0；
delivery 无 sshx 具体类型；禁令基线 ≤3 且每条有到期标签。

### 阶段 3：结构收敛

1. `sshx` 补四组测试（租约 / keepalive / 端口恢复 / SFTP channel），回收根目录孤儿测试包
2. 巨文件二拆（`log_collect.go` 2255、`delivery_runner.go` 1888）
3. 修 `#1586` 的 27 条失效引用（其合并前提）+ 订正部署文档
4. `kinds/` 要么建、要么在设计文档里明确废弃并说明替代守卫
5. U0 杀软抽样验证并留档

---

## 十三、审查发现索引

| 编号 | 层 | 位置 | 状态 |
|---|---|---|---|
| P0-1 | 代码 | `logpush/outbox.go` 预算超限路径 | 已修（含回归） |
| P0-2 | 代码 | `logpush/outbox.go` / `uploader.go` persistOK、静默丢 | 已修（含回归） |
| A1 | 架构 | `internal/app/bridge.go` Shared Kernel | 未修 |
| A2 | 架构 | logpush 被 20 文件 import | 未修 |
| A3 | 架构 | logcollect 伪公共库 | 未修 |
| A4 | 架构 | delivery 持 `*sshx.SSHSession` | 未修 |
| A5 | 架构 | `cmd/bridge/ws_routes.go` 路由未分权 | 未修 |
| P1-1~7 | 代码 | 见 §7 | 未修 |
| P2-1~6 | 文档/门禁 | 见 §7 | 未修 |

---

## 十四、关联文档

| 文件 | 角色 |
|---|---|
| `docs/plans/2026-11-18-terminal_bridge架构重构设计.md` | 架构总纲（目标态） |
| `docs/plans/2026-11-18-节点同步链路统一收口设计.md` | 四条链路收口子方案（含 27 条待修失效引用） |
| `docs/solution/events/2026-11-17-云端桥日志直传补全.md` | logpush 直传的引入背景 |
| `terminal_bridge/internal/logpush/outbox.go` | L2 分段 outbox 实现（P0 修复点） |
| `terminal_bridge/internal/logpush/uploader.go` | 发送/游标/预算（P0 修复点） |
| `terminal_bridge/internal/logpush/outbox_p0_test.go` | P0 回归守卫 |
| `scripts/check_bridge_imports.py` | 架构禁令扫描（门禁加固落点） |
