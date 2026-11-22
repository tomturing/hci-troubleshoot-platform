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
> （U1 telemetry、obslog L1-L4、门禁与文档对账），并记录两项 P0 缺陷的修复。
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
| 2026-11-22 | v1.9 | **A5 地基**：连接级上下文下沉 `internal/wsctx`（`ctx.B` → `Hub`/`Life`），落点避开 `shared`（禁令 1）；§4-A5 补现状与四步剩余工作，§7 债务表状态改「进行中」 |
| 2026-11-21 | v1.8 | **状态对账**：S1 改完成、A1/A2/A4 改已修、S4 重述剩余工作、A5 与 P1-3/P1-6 标注复核证据、P2 逐项归位、债务 4 测试数订正为 590/589。所有结论均以实测命令为据，不采信旧标注 |
| 2026-11-21 | v1.7 | 阶段 3 第 5 项交付静态准备：U0 杀软抽样验证方案（矩阵/判定/回退/留档表）+ Defender 取证脚本；实机结论未出，债务 5 保持 open |
| 2026-11-21 | v1.6 | 阶段 3 第 2/3 项完成：两个巨文件二拆（函数数零丢失）、outbox 旋钮入契约、seed 语义与失效引用订正；§4 影响、§6.1 表格、§7.2 债务 2/3 同步 |
| 2026-11-21 | v1.5 | 阶段 3 第 1 项完成：sshx 四组测试补齐、4 个孤儿测试归位（仅留 `remote_convergence_test.go` 并在文件头说明理由）；§6.1 表格同步 |
| 2026-11-21 | v1.4 | 阶段 2 S2/S3 收口：建 `reconcile/kinds` 登记册（四条链路声明接入程度，未登记即守卫失败），SPC 的 intent-only 缺口显式登记；§6.1 目标树差异、§7.2 债务 6、阶段 3 第 4 项同步更新 |
| 2026-11-21 | v1.3 | 阶段 2 A3 收尾：命令输出呈现下沉 `shared/remote`、传输证据改 `shared/evidence.TransferReporter` 端口，域间依赖边 4→0；`BASELINE_DOMAIN_EDGES` 置空，§3.3 与 §13 同步更新 |
| 2026-11-21 | v1.2 | 阶段 2 A3 第一批落地：五类符号归位（`logcontract`/`cluster`/`aclibin`/`acliout`/`remote`/`uplink`），域间边 6→4；补记剩余两条边的性质与不修理由（证据注册表、输出呈现），并更新 §3.3 阶段完成度与 §13 发现索引 |
| 2026-11-20 | v1.1 | 采纳 ADR-001：`internal/logpush` → `internal/shared/obslog`、`Blog()` → `Emit()`，并明确"不并入 telemetry"的三条判据与业界范式（§15）；路线图阶段 1（gaps 重放 / seed 互斥 / enforce 节流 / stderr 轮转）与本批同交付 |
| 2026-11-20 | v1.0 | 首版：归并两轮审查；修复 P0-1（预算超限自死锁）与 P0-2（persistOK 单向闩锁 / 磁盘满静默丢弃），各附生产接线回归测试 |

---

## 一、审查范围与方法

### 1.1 覆盖范围

| 轮次 | PR | 主题 |
|------|----|------|
| 第一轮 | `#1548` `#1553` `#1554` `#1557` `#1566` `#1569` `#1573` `#1575` `#1577` `#1580` `#1581` | P0 止血、D1 插件回归浏览器、S0/S1 节点面原语、R0 目录搬迁、S1 云面 uplink、U0 UPX |
| 第二轮 | `#1582` `#1584` `#1585` `#1586` | U1 telemetry 门面、L1-L4 obslog 完备性、自动 exe、符号门禁与文档对账 |

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
# 2026-11-21 复核：现为 590（noop）/ 589（telemetry）——A3 与阶段 3 补测后增加，
# 上面两行是当时（#1584 前后）的基线，保留作历史对照
go test -race ./internal/shared/obslog → 通过
GOOS=windows/linux go build → 双平台通过
python3 scripts/check_bridge_imports.py → 11 条域间依赖，未新增
go tool nm（桌面构建）      → otel 符号 0，obslog 符号 124
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
| S1 原语 | **完成（2026-11-21 复核）** | 节点面 `remote` 收口、云面 `uplink.Push` 统一；实测 `delivery` 生产代码 import sshx=0、`*sshx.SSHSession`=0；`logcollect` 仅被装配层 `cmd/bridge` import，域间依赖边 0 |
| S2 / S3 reconcile 消费者 | **完成（2026-11-21）** | 建 `reconcile/kinds` 登记册：四条链路必须声明接入程度，未登记由守卫测试判失败；SPC 的"只租意图、写入自管"从隐债变显式声明 |
| S4 acli 两阶段 | **部分（复核后重述）** | 两阶段协议已落地；`internal/acli` 内无 `InstallSpec` 类声明结构，安装/校验仍是指令式串联。原表述"声明式胶水未迁 kinds"已不准确——kind 改成了登记册，**剩余工作是"把安装动作声明化"，不是搬目录** |
| U1 telemetry | 完成 | `#1582`，桌面 otel 符号 0 |
| L1 obslog 完备性 | 完成（**含 P0 缺陷，本次修复**） | `#1584` |
| D1 删除 relay | 完成 | `#1553` `#1554` |
| 阶段 2 A1/A2 | 完成 | `app.Bridge` 拆分、obslog 门面化 |
| 阶段 2 A3 | **完成（2026-11-21）** | 五类符号物归原主 + 证据端口化 + 输出呈现下沉，域间依赖边 6→0，基线置空 |
| 阶段 2 A4/A5 | 完成 | delivery 端口化、门禁加固（禁令 6/7 + sunset） |

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

### A2 横切关注点做成了具体依赖：obslog 被 20 个文件 import

**证据**：`internal/app`(3)、`sshx`(1)、`terminal`(2)、`acli`(2)、`logcollect`(5)、
`delivery`(3)、`sftp`(1)、`cmd`(3) 直接 import `internal/shared/obslog`；缺一个 `Publisher`
端口。11 条冻结基线中 5 条由此产生。

**业界定位**：横切关注点必须依赖**接口**而非实现（Go 谚语 "accept interfaces"；
OTel 用 no-op provider 解耦，本项目的 `shared/telemetry` 已经做对了这件事）。同一个仓库里
telemetry 走了门面，obslog 却让 7 个包直接依赖——**同一范式在两条链路上执行不一致**。

**影响**：域单测必须构造 `logHub`；obslog 签名变更波及 7 个包；日志级别/采样等策略
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

**进展（2026-11-21，阶段 2 A3 第一批）**：已按上述范式完成五类符号归位，域间边从 6 条降到 4 条。

| 原归属 | 符号 | 新归属 | 依据 |
|---|---|---|---|
| `logcollect` | `Err*` 常量 + 元数据 + 展示文案 | `shared/logcontract` | 跨域契约，与平台 JSON 同源 |
| `logcollect` | `ErrString` `FirstNonEmpty` `TruncateForDiag` | 各域私有副本 | §3.5 判据：nil 安全属正确性，不进工具包 |
| `logcollect` | `ShellQuote` | 删除包装，调用方直连 `remote.ShellQuote` | 安全原语唯一实现 |
| `logcollect` | acli PATH/解析/版本/`__ACLI_BIN__` | `shared/aclibin` | 两域共用的事实层 |
| `logcollect` | `Spc*` 集群扇出 + 节点清单 + 本机地址 | `shared/cluster`（端口 `remote.PtyRunner`/`QuickRunner`） | 名字错位：这是集群拓扑，不是"场景清单" |
| `logcollect` | `ProbeSourceDigest` `ProbeLogCollectArchive` `NormalizeArchivePath` `ArchiveProbeToolMissingRe` | `shared/remote/archive.go` | 节点面探测原语 |
| `logcollect` | `ValidatePlatformURL` `FileDigest` | `shared/uplink/file.go` | 云面/本地文件面 |
| `logcollect` | `ParseLogCollectProgress` + 节点态归一化 | `shared/acliout` | "怎么读 acli 说的话" |

`logcollect` 保留同名薄包装（类型别名/委托），使采集编排与既有测试零改动；
`acli`、`sftp` 已完全不再 import `logcollect`。

**第二批（2026-11-21，A3 收尾）**：剩余两条边也消除，域间依赖边 **4 → 0**。

| 剩余边 | 处置 | 为什么不是简单搬文件 |
|---|---|---|
| `* -> terminal` | `NormalizeOutputText` / `FormatOutputPreview` 迁 `shared/remote/output.go`；terminal 保留同名包装 | 这是"命令输出的字节呈现"，与终端会话/PTY/VM 控制台无关；原先住在 terminal 才产生三条边。`delivery` 的 `On_output_start` 用例改为在 terminal 包内验证（它守护的是终端输出泵） |
| `delivery -> logcollect` | 新增 `shared/evidence.TransferReporter` 端口（4 方法 + 指标 DTO）；`logcollect.NewTransferReporter()` 实现，装配层 `evidence_wire.go` 注入 | 交付域一次都没读过 `EvidenceConfig` 字段，只当不透明句柄透传；把整结构交给它等于把票据/TOS/TLS 放宽策略变成跨域契约 |

配套：`evidence.TransferMetric`（上报契约 DTO）归 shared；采集侧投递测试（票据头、任务路径）随实现迁回 logcollect；交付域只测采样算法（EMA 归零、停摆、ETA）。
`BASELINE_DOMAIN_EDGES` 已置空——任何新跨域边直接失败，不再有"遗留可冻结"的借口。

### A4 端口建了没走：delivery 全程持 `*sshx.SSHSession`

**证据**：`delivery_runner.go` 的 `sessionFor`/`requireSessionForPhase`/`waitCollect`/
`prepareDirect`/`ensureSourceDigest`/`pull` 等 10+ 处签名直接使用 `*sshx.SSHSession`，
自行拼命令、探测、拉包。

**业界定位**：Ports & Adapters 的价值在于**策略层只依赖端口**，这样可用 Fake 适配器离线
测试（`net/http` 的 `RoundTripper` 正是为此存在）。`remote` 包自己已经用 `fakeRunner`
做到了（47 例单测），但 delivery 没接。

**影响**：delivery（1888 行、81 例测试）无法离线验证编排逻辑，只能靠集成测试；
这也是它必须反向依赖 logcollect 的原因（借它的 sshx 包装）。

> **现状（2026-11-21）**：已按 A4 端口化（`NodeSession`），并由阶段 3 二拆为
> `delivery_runner.go`(968) + `delivery_transfer.go`(991)；81 例测试随文件移动，断言未改。

### A5 微内核退化成"搬家"：路由仍在装配层

**证据**：`cmd/bridge/ws_routes.go` 524 行，`init()` 里注册 18 条路由，**handler 实现
也在该文件**，并直接调用 `otel.Tracer`。设计 §3.2 要求 `cmd/bridge` "不含任何业务逻辑"。

**业界定位**：微内核 + 注册表的价值是"新增能力不改核心"。当前形态只是把 `main.go` 的
中央 switch 换成另一个中央文件——新增消息仍须改这个 524 行文件，收益只剩"路由表可单测"。

**根因**（代码自述）：域反向 import `app` 会成环，`app` 又 import `obslog`/`terminal`，
所以只能在装配层注册。**A1 解不开，A5 就解不开**。

> **现状（2026-11-22）**：A1/A2/A3 已解环，上述根因**已不成立**——`ws_routes.go` 头部那段
> 「域反向 import app 会成环」的注释属过期理由，随分权一并删除。
> 地基已落地：连接级上下文从 `*app.Bridge` 单字段 `B` 拆为 `Hub`（会话表）+ `Life`（生命周期端口），
> 类型与端口定义下沉到 `internal/wsctx`（**不能放 `shared`**：禁令 1 禁止 shared 依赖 sshx，
> 否则 `*sshx.SSHSession` 得全部接口化，域签名跟着改，属过度设计）。
> 剩余工作：① 路由注册表从 `app` 下沉 `wsctx`（否则域自注册必须 import app，撞禁令 6）；
> ② 19 个 handler 按域归位（delivery/logcollect/sftp/acli → terminal → 会话族）；
> ③ `ssh_connect` 内 `logcollect.RecoverSpcRestoreIntents` 需先端口化，否则归位即产生域间边；
> ④ `bridge_log_token` 判定留在装配层（桥自身日志通道，不属任何业务域）。

---

## 五、模块层

| 域 | 目标职责（设计） | 实际状况 | 耦合出口 |
|---|---|---|---|
| `app` | 微内核，不认识业务语义 | 仍是状态宿主 + 生命周期 + 指标，`bridge.go` 524 行 | → terminal, obslog |
| `sshx` | 端口适配器，不含 WS 知识 | 1657 行，**0 测试**，含会话租约/keepalive/端口恢复/SFTP channel | → obslog |
| `terminal` | 交互终端、EXEC、VM console | 1469 行 / 5 例测试；仍被 logcollect/acli 借 `FormatOutputPreview`、PTY 常量 | → obslog |
| `acli` | 两阶段协议边缘，零编排 | 3119 行，编排仍内联；借 logcollect 错误码与 SPC 五件套 | → app, logcollect, obslog, terminal |
| `logcollect` | 采集编排 + 错误码 + 证据 | 7414 行（未拆），同时充当公共库 | → app, obslog, terminal, reconcile |
| `delivery` | 作业状态机，传输全委托原语 | 3439 行，仍持 `*sshx.SSHSession` | → app, logcollect, obslog |
| `sftp` | 下载，文件名主权 | 1030 行，单文件未三拆 | → app, logcollect, obslog |
| `obslog` | 证据通道 | 2354 行（本轮 +840） | → app 反向被依赖 7 包 |
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
| `shared/reconcile/kinds/{acli,spc,config}/` | 已建为**登记册**（`kinds/registry.go`）：不搬 DesiredState 构建，只登记"链路—实现域—接入程度" | "新增链路只加 DesiredState"改为可验证：`AllKinds()` 的每个值必须在装配层登记，缺失即 CI 红；构建逻辑仍留在各域，避免把产品语义搬进 shared |
| `app/config.go` | `app/cfg.go` | 命名漂移，无害 |
| `delivery/store.go` | `delivery/delivery_local_store.go` | 搬迁未改名，与设计不一致 |
| 目标树未列 `shared/runtime` | 存在（30 行 env 助手） | 新增包未回写设计；"utils 不建"的判据被绕开 |
| 根目录无 Go 文件 | 1 个例外：`remote_convergence_test.go`（全仓 import 图守卫，需要仓库视野） | 其余 4 个孤儿已于 2026-11-21 归位：`probe_and_classify`/`wrap_container` → `internal/sshx`、`ssh_input_routing` → `internal/sshx/session`、`session_busy` → `internal/app`；`sshx` 从零测试补齐四组 |

### 6.2 巨型文件 TOP（实现）

| 文件 | 行数 | 问题 |
|---|---|---|
| `logcollect/log_collect.go` | ~~2255~~ → 702（2026-11-21 二拆） | 编排留在 `log_collect.go`，命令构建/探测/解析归 `log_collect_probe.go`(939) |
| `delivery/delivery_runner.go` | ~~1888~~ → 968（2026-11-21 二拆） | 队列/阶段推进/错误分类留在 `delivery_runner.go`，节点阶段+拉取上传+证据归 `delivery_transfer.go`(991) |
| `logcollect/log_collect_spc_fanout.go` | 1600 | 扇出 + 场景语义 + 证据 |
| `sshx/session.go` | 1298 | 连接 + 租约 + keepalive + 重连，**零测试** |
| `sftp/sftp.go` | 1030 | 设计要求三拆（handler/download/sanitize），未拆 |

### 6.3 命名与职责漂移

- `logcollect` 以业务域命名，实际被当作公共库（见 A3）；
- `app.Bridge` 以"桥"命名，实际是全局状态容器（见 A1）；
- `shared/runtime` 名为 runtime，实际是 env 读取助手——若继续扩张会变成新的 utils 抽屉；
- ~~`obslog` 与 `logcollect` 都缺少"日志主体"维度，导致"桥自身日志"被误读成"客户日志推送"~~
  → 已按 §15 决策改为 `internal/shared/obslog`（`Blog` 同步改为 `Emit`）。

---

## 七、代码细节缺陷清单

> 状态标记：【已修】本次修复并附回归测试；【未修】留待后续阶段。

### P0-1【已修】磁盘预算超限路径的自死锁

**位置**：`terminal_bridge/internal/shared/obslog/outbox.go`（`enforceDiskBudgetLocked`）

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

**回归测试**：`internal/shared/obslog/outbox_p0_test.go::TestGapReportDoesNotDeadlockOnDiskBudget`，
刻意使用**全局 `LogUpload` + 生产接线**（而非局部实例），5 秒超时判定死锁。
已做反向验证：把 `Blog` 改回去，该测试在 40 秒超时失败；改回修复后 0.10s 通过。

### P0-2【已修】`persistOK` 单向闩锁 → 无限重放；磁盘满时静默丢弃

**位置**：`terminal_bridge/internal/shared/obslog/outbox.go`、`uploader.go`

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

### P1（阶段 1 已修 P1-1/2/4/5/7；P1-3/6 仍未修）

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
| 2 | 部署文档失真 | ~~已订正（2026-11-21）~~：事件文档补"单轮预算"订正标注，Helm values 注释同步；部署设计 v1.91 记录 |
| 3 | 新旋钮未纳契约 | ~~已修（2026-11-21）~~：登记 `helm_only_runtime` + `terminalBridge.logOutboxMaxBytes`（字符串避免科学计数法），`verify_config_contract.py` 通过 |
| 4 | 测试数口径 | ~~已订正（2026-11-21）~~：实测 **590（noop）/ 589（telemetry）**，口径 `go test ./... -list '^Test' \| grep -c '^Test'`。原"544/540/539"是 A3 与阶段 3 补测之前的数字 |
| 5 | U0 加壳风险未闭环 | **方案就绪、待实机执行（2026-11-21）**：`docs/verify/U0-杀软抽样验证方案.md`（必测矩阵/判定标准/回退路径）+ `scripts/verify/u0_antivirus_probe.ps1`（Defender 指纹与扫描留档）。企业 EDR/AppLocker 仍须人工，结论出来前本条保持 open |
| 6 | `kinds/` 未建的解释 | v2.6 将"未建 kinds"表述为"设计意图均满足"，属事后合理化；**2026-11-21 已收口**：建登记册 + 守卫，SPC 的接入缺口写进 `Descriptor.Note` 并由守卫打印 |

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
| Traces | `shared/telemetry` 门面 + build tag 双实现 | **标杆**：签名零 otel 类型、nm 双向门禁、传播解析用纯标准库保证 obslog 不随 tag 剥离 |
| Metrics | `app/metrics.go` | 可用；obslog 侧缺"重放/丢弃/游标"指标 |
| Logs·本地 | `obslog/hub.go` | 可用；`bridge-stderr.log` 无轮转 |
| Logs·证据 | `obslog` 直传 + L1-L4 补传 | 本轮补强；缺口见 §7-P1 |

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
| 桌面含 obslog 符号 | `#1586`（待合并） | 有效（防证据通道被误剥） |
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
| 测试函数 | 540（noop）/ 539（telemetry）〔2026-11-21 复核：590 / 589〕 | 本次 +3 |
| 分发 exe | 3,130,368 B | 16.47MB → 3.13MB，-81% |
| 桌面 otel 符号 | 0 | 集群 2099 |
| 桌面 obslog 符号 | 124 | 证据通道保留 |
| 域间依赖边 | 11 | 未新增 |
| 最大实现文件 | 2255 行（`log_collect.go`） | **已降至 991 行**（`delivery_transfer.go`）；`log_collect.go` 702 / `log_collect_probe.go` 939 |
| 零测试包 | `sshx`（1657 行）、`models`、`runtime` | 未变 |

---

## 十二、优化路线图

### 阶段 0：止血（本次已完成）

- 修 P0-1 自死锁（含生产接线回归测试 + 反向验证）
- 修 P0-2 `persistOK` 复位 / refill 暂停 / 静默丢弃留痕
- `/status` 暴露 `persist_ok`

**验收**：双 tag 全量测试绿、race 绿、双 GOOS 绿、禁令 11 条未新增；缺陷回退时新测试必失败。

### 阶段 1：可靠性补齐（本轮完成）

1. `gaps.jsonl` 重放 + 归档截断 + 纳入预算（P1-1）
2. `SeedFromLocalLogs` 互斥（P1-2）
3. `enforce` 节流（P1-4）
4. stderr 轮转/预算（P1-5）
5. `/status` 补齐 gaps/seed_pending/cursor；`dropped` 读写口径统一

**验收**：四个崩溃场景（进程 kill / 断电模拟 / 磁盘满 / 平台长不可达）下，
"收到的集合 ∪ gap 区间"覆盖本地窗口；`/status` 能独立判定链路健康。

> **落地说明**（本阶段与 §15 的命名调整同批交付，路径统一为 `internal/shared/obslog`）：
>
> | 项 | 实现 | 边界 / 权衡 |
> |---|---|---|
> | gaps 重放 | `replayGapsLocked` 在 `recoverOutbox` 时读取 `gaps.jsonl`，插到内存队首；原文件归档为 `gaps.replayed.jsonl`（仅保留最近一次），损坏行跳过 | 重放后、入队前若再次崩溃，归档仍在但不自动重放（`/status` 的 `replayed` 可发现停滞） |
> | gaps 预算 | 计入总占用但**不参与** drop-oldest，落盘失败仍返回内存队列 | 避免"预算满了先删证据" |
> | seed 互斥 | `SeedFromLocalLogs` 加 `seedMu`，configure（env/WS）与 worker 两路串行 | 最坏让 worker 等一个 2s 预算窗口 |
> | enforce 节流 | `checkDiskBudgetLocked` 按 `maxBytes/8`（下限 1KB）与 5s 双阈值 | 生产 8MB 预算 ≈ 每 1MB 检查一次；测试小预算下退化为近乎每条检查，既有断言语义不变 |
> | stderr 轮转 | **启动时**检查 60MB，超限轮转为 `.1` 并写 `bridge-stderr.gen`；`event_id` 改为 `stderr:<inst>:<gen>:<offset>` | 运行中不轮转（Windows 不允许 rename 被 `os.Stderr` 占用的文件）；`.1` 中未补传的旧尾部不自动补传，属已知残余风险，见 §7-P1-5 |
> | `/status` | 新增 `gaps`/`replayed`/`seed_pending`/`cursor_segment`/`cursor_offset`；`dropped` 改为锁内读写（消除 atomic 与互斥混用） | `cursor_offset` 长时间不变 = 平台不可达或鉴权失败 |

### 阶段 2：架构解耦（真正的总钥匙）

1. **拆 `app.Bridge`**：会话簿记 → `sshx.SessionRegistry`；`SftpTasks`/`AcliCache` 归还各域；
   消除 52 个函数首参与 11 处反向 import。
2. **obslog 接口化**：定义最小 `Publisher` 端口，装配层注入；基线从 11 条降到 ≤3 条。
3. **logcollect 瘦身**：符号物归原主 + 错误码独立契约包 + SPC 迁 reconcile 消费者。
   - 已完成（2026-11-21）：错误码 → `shared/logcontract`；集群扇出 → `shared/cluster`；
     acli 事实层 → `shared/aclibin`/`shared/acliout`；探测摘要 → `shared/remote`；
     文件/URL 校验 → `shared/uplink`；通用小工具按 §3.5 改为域内私有副本。
   - 未完成：证据凭据注册表（`delivery -> logcollect`）与命令输出呈现（`* -> terminal`）。
4. **delivery 接 Runner**：`*sshx.SSHSession` 换成端口 + FakeRunner 编排单测。
5. **门禁加固**：新增"域→app""域→sshx 具体类型"两条规则；基线加 sunset。

**验收**：`grep internal/app internal/*/` 为 0；`grep internal/shared/obslog` 在域内为 0；
delivery 无 sshx 具体类型；禁令基线 ≤3 且每条有到期标签。

### 阶段 3：结构收敛

1. ~~`sshx` 补四组测试，回收根目录孤儿测试包~~ → **已完成（2026-11-21）**：租约（`hub_lease_test.go`：Release 幂等刷新、保护期推起点、无簿记不回收、TTL≤0 关闭）、keepalive（默认/覆盖/关闭、无 client 报错、测试会话短路、PTY 归一化与终端模式）、端口恢复（`session_busy_test.go` 迁入 `internal/app`）、SFTP channel（缓存复用、丢弃关闭并清空、幂等、并发安全、错误原样冒泡）
2. ~~巨文件二拆~~ → **已完成（2026-11-21）**：`log_collect.go` 2255→702+939、`delivery_runner.go` 1888→968+991；函数数 28/28、59/59 零丢失，切分依据是"编排 vs 与节点/平台搬字节"，不是按行数等分
3. ~~修 `#1586` 的 27 条失效引用 + 订正部署文档~~ → **已完成（2026-11-21）**：27 条现均为"删除事实记录"语境豁免（`check_docs_rot --dir docs/plans` 无阻断项）；订正两处失真——`HCI_BRIDGE_LOG_OUTBOX_MAX_BYTES` 登记 `helm_only_runtime` 并注入 Helm，事件文档里"只扫 2 个文件、最多 5000 条"改为单轮预算语义
4. ~~`kinds/` 要么建、要么明确废弃~~ → **已建（2026-11-21）**：`reconcile/kinds` 登记册 + 守卫测试，替代"目录形状"的实质约束
5. ~~U0 杀软抽样验证并留档~~ → **部分完成（2026-11-21）**：验证方案与取证脚本已交付（样本矩阵、判定口径、未压缩回退、留档表）；实机抽样需 Windows + 杀软环境，**留档表仍为空，债务 5 保持 open**

---

## 十三、审查发现索引

| 编号 | 层 | 位置 | 状态 |
|---|---|---|---|
| P0-1 | 代码 | `shared/obslog/outbox.go` 预算超限路径 | 已修（含回归） |
| P0-2 | 代码 | `shared/obslog/outbox.go` / `uploader.go` persistOK、静默丢 | 已修（含回归） |
| A1 | 架构 | `internal/app/bridge.go` Shared Kernel | **已修（#1594）**：域 import app = 0（实测） |
| A2 | 架构 | obslog 被 23 文件 import | **已修（#1594）**：域 import obslog = 0，改走 `shared/evidence` 门面 |
| A3 | 架构 | logcollect 伪公共库 | **已修（两批）**：五类符号归位 + 证据端口化 + 输出呈现下沉，域间边 6→0，基线置空 |
| A4 | 架构 | delivery 持 `*sshx.SSHSession` | **已修（#1594）**：`NodeSession` 端口化，生产代码 import sshx = 0、`*sshx.SSHSession` = 0 |
| A5 | 架构 | `cmd/bridge/ws_routes.go` 路由未分权 | **进行中（2026-11-22）**：上下文缝已下沉 `internal/wsctx`（`ctx.B` → `Hub`/`Life`），handler 实现仍在装配层、域仍不自注册；分权按域分批推进 |
| P1-1/2/4/5 | 代码 | gaps 重放、seed 互斥、enforce 节流、stderr 轮转 | 已修（阶段 1） |
| P1-3/6/7 | 代码 | 无 fsync、seed O(n²)、观测字段 | P1-7 已修；**P1-3 未修**（`obslog` 全包 `grep Sync()` 为 0）；**P1-6 未修**（`mergeSeedLocked` 仍逐条 `sort.Search` + `copy`）——2026-11-21 复核 |
| P2-1~6 | 文档/门禁 | 见 §7 | P2-2/3/6 已修；P2-1 复核为"删除事实记录"语境豁免、无阻断项；P2-5 方案就绪待实机；**P2-4 本次订正**（见 §7.2） |

---

## 十四、关联文档

| 文件 | 角色 |
|---|---|
| `docs/plans/2026-11-18-terminal_bridge架构重构设计.md` | 架构总纲（目标态） |
| `docs/plans/2026-11-18-节点同步链路统一收口设计.md` | 四条链路收口子方案（含 27 条待修失效引用） |
| `docs/solution/events/2026-11-17-云端桥日志直传补全.md` | obslog 直传的引入背景 |
| `terminal_bridge/internal/shared/obslog/outbox.go` | L2 分段 outbox 实现（P0 修复点） |
| `terminal_bridge/internal/shared/obslog/uploader.go` | 发送/游标/预算（P0 修复点） |
| `terminal_bridge/internal/shared/obslog/outbox_p0_test.go` | P0 回归守卫 |
| `scripts/check_bridge_imports.py` | 架构禁令扫描（门禁加固落点） |

---

## 十五、决策记录：obslog 命名（ADR-001）

> 状态：已采纳（2026-11-20）｜影响面：23 个 import、306 处包前缀、207 处调用点

### 15.1 问题

`logcollect`（采集**客户节点**日志）与 `logpush`（推送**桥自身**日志）都以 `log + 动作` 命名，
**缺少"日志主体"这一维**。首次接触者（含本项目的长期维护者）会把两者混为一谈，
误以为 `logpush` 也是客户日志链路。这不是名字难听，而是命名丢失了区分度最高的一维。

### 15.2 备选方案

| 方案 | 语义 | 关键取舍 |
|---|---|---|
| **`shared/obslog`（采纳）** | 可观测性日志（含操作审计事件） | 顺应"这是可观测性"的正确直觉；与 `logcollect` 词形差异大 |
| `shared/selflog` | 桥自身日志 | 直白，但不体现投递形态，也不像基建术语 |
| `shared/bridgelog` | 桥日志 | 最无歧义，但产品名进入包名，组件更名需二次返工 |
| 并入 `shared/telemetry` | "都是可观测性" | **否决**，见 15.3 |
| 不改名，只加文档 | — | 否决：正确理解依赖"先查文档"，误解仍是默认路径 |

### 15.3 为什么不是并入 `shared/telemetry`

按包内聚的三条硬判据（变化原因 / 依赖方向 / 开关语义），两者均不满足同包条件：

| 判据 | `telemetry` | `obslog` | 结论 |
|---|---|---|---|
| 变化原因 | 追踪后端、采样、传播格式（随 OTel 规范） | 投递链路、事件 schema、outbox 策略（随平台接口） | 两条独立变化轴 |
| 依赖方向 | **叶子包**：本体纯标准库，otel 只在 `//go:build telemetry` 文件里 | 依赖 `x/net/websocket`、`models`、`runtime`、HTTP 与文件系统，**并消费 telemetry 取 Traceparent** | 合并会污染稳定层 |
| 开关语义 | build tag 控制"是否导出 trace" | 桌面**必须常驻**（CI 有"桌面产物含 obslog 符号"门禁） | 两个正交开关不可绑死 |

合并还会让两条 nm 门禁失效：`桌面零 otel` 与 `桌面必须含日志管道符号` 依赖**两个不同包路径**
才能分别表达，合并后符号层面无法区分。

**业界范式佐证**：

- OpenTelemetry 自身四层严格分离：API（`otel`）→ SDK（`otel/sdk`）→ Exporter → Collector pipeline/WAL；
  其中 API 层可 no-op，Exporter 与队列是重 I/O 层，从不合并。
- OTel Collector 的 "internal telemetry"（采集器观测自己）与其处理的业务日志管道是两套实现。
- Go 标准库：`log/slog`（日志 API）、`runtime/trace`、`expvar` 分属不同包。
- Promtail：`clients`（推送）、`positions`（游标）、`targets`（发现）分属不同包。
- Go 包设计原则：包是**变化与复用单元**，不是**主题单元**；被依赖多的包必须保持薄（Stable Dependencies Principle）。

**反向判据（何时可以合并）**：若 `obslog` 退化到几十行、只做"把 TraceID 拼进一行 JSON"，
即可并入 `telemetry`。当前它是 2354 行、有本地环形文件、WS 推送、直传/outbox/补传、游标与
崩溃恢复的**有状态 I/O 管道**，与"薄门面"是两类东西。

### 15.4 决策

```
internal/shared/
  telemetry/   trace 门面（API 层，可 noop，叶子）
  obslog/      桥自身事件日志（管道层，依赖 telemetry）
```

依赖单向：`obslog → telemetry`，禁止反向。业务域按需分别依赖，互不传递。

### 15.5 不动的跨端契约

| 契约 | 值 | 原因 |
|---|---|---|
| 事件名前缀 | `log_upload.*` | 平台证据表与前端按此过滤 |
| 前端消息类型 | `type === "bridge_log"` | 前端契约 |
| `/status` 字段 | `enabled`/`queued`/`dropped`/`spilled`/`persist_ok` 等 | 运维脚本与排障手册 |
| 环境变量 | `HCI_BRIDGE_LOG_*` | 已写入部署契约与现场手册 |

改名只作用于**代码标识符与目录**，不触碰线上协议。

### 15.6 后果

- 正面：新人默认理解正确；`obslog.Emit(...)` 表达"产生一条自身事件"，语义自洽；
  与 `logcollect`/`delivery` 的边界在目录树下即可区分。
- 负面：一次性 300+ 处机械改动，与在途分支冲突（本次已确认仅 release PR 在途）；
  文档与门禁脚本需同步。
- 缓解：纯重命名不做逻辑变更，编译器与全量测试兜底；单独 PR，便于 review 与回滚。
