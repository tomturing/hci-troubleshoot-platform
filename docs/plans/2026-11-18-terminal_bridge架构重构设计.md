---
status: active
category: solution
audience: developer
last_updated: 2026-11-18
owner: terminal_bridge
---

# terminal_bridge 架构重构设计（2026-11-18，v2.0）

> 本文是 terminal_bridge 的**架构总纲**：定义整体架构范式、业界最佳实践参照、
> 改进后的完整目录结构与逐文件说明、现有代码迁移映射、体积优化路线与落地顺序。
>
> 与《2026-11-18-节点同步链路统一收口设计》（下称"收口设计"，v2.0）的关系：
> 收口设计是本文在"四条正向同步链路逻辑收口"上的**子方案**，其 P-1/S0-S4/D1 阶段
> 在本文的目录骨架内执行。本文对收口设计有三处结构性修订（见 §3.4）：
> ① 云面传输原语从 remote/transfer 拆出，独立为 shared/uplink；
> ② 公共模块统一落位 internal/shared/ 下，对齐 backend/shared 仓库惯例；
> ③ WS 协议边缘归功能域（internal/acli 承担两阶段协议边缘，**插件不独立成域**），kinds 零 WS 知识。
>
> 所有现状数据（行数、体积、符号归因）均实测自 origin/main `bf8361bd` 源码构建。

---

## 变更历史

| 日期 | 版本 | 变更摘要 |
|------|------|---------|
| 2026-11-20 | v2.8 | 命名与可靠性收敛：① `internal/logpush` → `internal/shared/obslog`、`Blog()` → `Emit()`（ADR 见《2026-11-20-terminal_bridge重构审查与优化》§15：`log+动作` 缺"日志主体"维度导致与 logcollect 混淆；经三条判据确认不并入 telemetry——变化原因、依赖方向、build tag 开关语义均不同）；事件名 `log_upload.*`、前端 `type=bridge_log`、`/status` 字段、`HCI_BRIDGE_LOG_*` 四类跨端契约不变；obslog 下沉 shared 后域间依赖基线 11 条 → 6 条；新增包文档 `obslog/doc.go` 明确"桥自身事件"与"客户节点日志采集"边界。② 阶段 1 可靠性补齐：gaps.jsonl 重启重放 + 归档 + 纳入总占用；补传扫描 seedMu 串行化；预算检查按 maxBytes/8 与 5s 双阈值节流；stderr 启动时按 60MB 轮转一代并写代数标记，event_id 带代数避免偏移复用被幂等误丢。③ 新增回归测试 3 例（gaps 重放、损坏行跳过、代数读取） |
| 2026-11-20 | v2.7 | 实施后审查与 P0 修复（详见《2026-11-20-terminal_bridge重构审查与优化》）：L2 outbox 预算超限路径 `enforceDiskBudgetLocked` 在持 `u.Mu` 时调 `Blog`，经 `publish → enqueue` 同 goroutine 重入 `u.Mu` 造成自死锁（平台久不可达、弱网积压时触发，桥整体挂死），改为 stderr 留痕经 L3 补传上报，不再产生重复 gap 事件；`persistOK` 由单向闩锁改为可复位（写成功即复位并把游标钳回本条起点，避免越过故障期空洞误删后续段），`refillFromDiskLocked` 在故障期暂停（杜绝无限重放），落盘失败且内存队列满时计入 `dropped` 并留痕（"零静默丢弃"在磁盘满分支补全），`/status` 新增 `persist_ok`；新增 3 个生产接线回归测试（全局 `LogUpload`，非局部实例），并做缺陷回退的反向验证。审查同时记录 5 项架构债（Shared Kernel、横切 logpush、logcollect 伪公共库、delivery 未接 Runner、路由未分权）与 7 项 P1，留待后续阶段收敛 |
| 2026-11-20 | v2.6 | 终局对账（两文档逐条核对代码事实）：全部阶段完成——P-1 止血、U0（#1581）、R0（#1575）、S1-S4/D1（收口设计 v2.0-s4b/e2e，uplink 部分 #1580）、U1（#1582）、L1（#1584）。目标树与落地的结构差异留痕（设计意图均满足，守卫钉死）：① kinds/ 目录未建——reconcile 为纯数据驱动骨架（kinds.go Kind 枚举+pipeline），消费者声明式胶水住功能域（acli/logcollect），新增链路只加 DesiredState 值+Provider 不改骨架，remote_convergence_test.go golden 逐字+形态扫描守卫；② 注册式路由由装配层 cmd/bridge/ws_routes.go 统一注册（v2.1 ③，域 init 自注册会成环），18 种消息守卫冻结；③ shared/runtime 新增（env/mode 公共助手，目标树未列）；④ uplink/token.go 未独立成文件——令牌约定住唯一消费者 logpush；⑤ models 仅 ws.go（delivery 视图 DTO 住 delivery 域，无跨进程需求）；⑥ cmd/bridge/main.go 236 行（目标 <200，超出为启动注释与装配接线，无业务逻辑）；⑦ 模块根留 5 个 package main 守卫测试（golden/静态形态扫描，跨域守卫不属任何单域）。CI 补桌面产物 logpush 符号门禁（风险 #19，实测 124 符号/otel 0）；U2 可选未做（U0 ≤5MB fail-closed 门禁已防体积回归）；手动上传入口退役为 gated 后续（等价性已过，待版本覆盖+≥2 周观察期，不占 bridge 阶段）。全量验收：544 测试函数零丢失、race/双 tag/双 GOOS 绿、import 禁令 11 条冻结未新增 |
| 2026-11-20 | v2.5 | L1-L4 落地（§3.6）：L1 全量补传——扫全部 bridge-*.log/.log.1（含当前文件，排除 bridge-stderr.log），单轮字节 8MB/时间 2s/条数预算，进度落盘 bridge-log-seed-<instance>.json 断点续扫（DoneBefore 跳已扫旧文件、当前文件只读增量尾部），env/WS 双路径 configure 后异步 seed，worker 每轮续扫；L2 分段 outbox——seg-<纳秒>.jsonl 单段 1MB 滚段，cursor.json 只在批次确认后前进（跨段进位+删已消费段），内存满仅落盘不丢弃（spilled），队空从游标 refill，重启不 truncate 按游标恢复，旧单文件 outbox 自动迁为最旧分段；总磁盘预算（默认 8MB）超限 drop-oldest 段（活动段不碰），被丢段数据条目按全局索引从内存队列真剔除，gap 标记（log_upload.gap，携带 seq 区间/条数/段名）写独立 gaps.jsonl 不占预算不被丢——真丢真报；L3 stderr 包装——非 JSON 非空行包装 bridge.stderr/ERROR，字节水位 bridge-log-uploaded-<instance>.stderr 只进不退，确认送达才推进，尾部不完整行不消费；L4 当前文件纳入只读完整行；双队列（实时 queue 占游标/补传 seedQueue 仅内存）+补传批不推 seq 水位+落盘失败 persistOK=false 游标停进重复交后端幂等；hubLogLocationMu 快照保护 LogDir/LogPath 读侧（测试缝并发改写不竞态）；四场景等价性+gap/水位/续扫/迁移守卫共 8 新测试，race/双 tag/双 GOOS 全绿，测试函数名零丢失（本分支 +8，全量清单 544，对基线零删除），import 禁令 11 条未新增 |
| 2026-11-20 | v2.4 | U1 落地：shared/telemetry 门面（Start/StartClient/Span/Attr/SpanInfo，签名零 otel 类型）+ build tag 双实现（noop 默认/otel 带 telemetry tag）；W3C 传播解析归门面本体（纯标准库，logpush 证据通道不随 tag 剥离，桌面日志 trace 关联不变）；7 处调用点迁门面（main initTelemetry→Init、ws_routes/exec/vmconsole/session/hub/main_test）；Dockerfile 集群镜像带 -tags telemetry；CI 双 tag 测试/vet + nm 门禁（桌面零 otel 符号，实测 0；集群阳性对照 2099）；无效上下文全零 ID 口径对齐 OTel 零值，日志/回执字段形状两实现一致；桌面 stripped 产物 17.7→10.5MB（压缩后待 CI 实测，预估 ~3MB）；测试无 tag 清单 532（528−1 随迁 tagged+5 门面守卫，合计 533 零丢失） |
| 2026-11-20 | v2.3 | U0 落地：构建脚本加 upx --lzma --best（本地无 upx 告警跳过，CI 装 upx-ucl 并以 ≤5MB 门禁 fail-closed）；bridge-version.json 新增 sha256 字段（按压缩后产物计算，CI 实测对齐门禁；前端只比 version 串，新增字段向后兼容）；未压缩备选收进 CI artifact（30 天，不进仓库，§6.3 兜底）；压缩产物 -version 冒烟用同参数 linux/amd64 产物验证（windows exe 无法在 ubuntu 直接执行）；源码目录 4 个历史二进制清出（3 个 git rm + 1 个本地件归档），.gitignore 兜底拦截；build_windows.bat 同步 --lzma 口径 |
| 2026-11-20 | v2.2 | S1 uplink 部分落地：shared/uplink/{chunk.go,pace.go} 统一云面原语——Push（并发/单片共享预算/fence 闸门/OnPart 落盘回调/Classify 错误分类钩子，覆盖 delivery 与 logcollect 两域语义）+ Pace/NextPace/RoundSample 通用化；delivery uploadOnePart/putPartBytes 与 logcollect putPart/uploadArchiveParts 退役；DirectUploadPrefix/PartRetries/DefaultPartTimeout 归位 uplink（logcollect 保留别名）；测试函数名守恒随迁 |
| 2026-11-20 | v2.1 | R0 实施留痕（不改设计结论，只记实施口径）：① R0a/R0b/R0c 完成——main.go 包内拆分、六域整域搬迁（terminal/app/acli/logcollect/delivery/sftp）、薄入口 cmd/bridge；② 消息清单口径 17→18：bridge_log_token 在本文定稿后加入，守卫测试按 18 种冻结；③ 注册式路由暂由装配层 cmd/bridge/ws_routes.go 统一注册（app 已 import logpush/terminal，域 init() 自注册会成环；待 S1-S4/uplink 解环后迁回各域）；④ kinds 拆分（kinds/acli/plugin.go）延后至 S4 两阶段接入时落地；⑤ ldflags 事实：链接器对 main 包恒用 main.* 别名（与目录位置无关），构建脚本只改目标 ./cmd/bridge，-X main.* 不变；⑥ 域间既有互 import 11 条以冻结基线管理（scripts/check_bridge_imports.py 接入 CI，只减不增，解环属逻辑收口阶段）；⑦ contract 生成器归位 internal/logcollect（ContractText/ContractFormat 导出），再生成逐字节一致 |
| 2026-11-18 | v2.0 | 重写定稿：插件收口为 acli_sync 第二阶段（internal/acli 两阶段协议边缘 + kinds/acli/plugin.go，不独立成域）；D1 直接删除 relay 推送链路与 admin-ui 下发入口；落地顺序与验收同步收口设计 v2.0；保留范式/目录/迁移/体积/logpush 全部既有结论 |

---

## 一、现状与问题定义

### 1.1 现状结构

> 本节为 R0 实施前（2026-11-18）的现状盘点；R0 已于 2026-11-20 落地（见变更历史 v2.1），下述扁平结构已成历史。

terminal_bridge 重构前是**单一 `package main`、零子目录**的扁平结构：

- 非测试 Go 文件约 40 个，共 **19,576 行**；加测试共 29,119 行；
- 旧路径 `terminal_bridge/main.go`（R0 后已删除，拆分至 cmd/bridge 与 internal/*）一个文件 **6,480 行**，同时承担进程骨架、WS 消息分发、
  SSH 交互终端、VM console、acli_sync 全链路、死代码；
- 文件命名前缀（`delivery_*`、`log_collect_*`）是唯一的逻辑分层手段，
  **编译器不强制任何边界**，任何文件可调用任何文件的任何函数；
- 源码目录混放 4 个编译产物二进制（exe ×3、darwin ×1）与构建脚本。

### 1.2 七个功能域（现状盘点）

| 域 | 主要文件 | 职责 |
|----|---------|------|
| 进程骨架 | main.go（部分）、lifecycle.go、tls_trust.go | 双形态（desktop exe / cluster pod）、HTTP/WS 服务、17 种 WS 消息分发、生命周期事件、自签证书信任 |
| SSH 交互终端 | main.go（主体） | ssh_connect/input/resume/disconnect、PTY、keepalive、ssh_exec_command/process、vm_console_op |
| SFTP 下载 | sftp.go | HCI 远端 → 桥所在机器磁盘，list/download/cancel |
| 日志收集 | log_collect*.go（~7000 行） | probe/create/progress/stat/transfer/cancel/restore_spc/capabilities；spc 场景覆写与扇出；-c 配置；制品回传；证据上报 |
| 日志包交付 | delivery_*.go（~4000 行） | submit/status/resume/cancel；落盘状态机；断点拉包 + 分片上传；pace 自适应 |
| acli 工具同步 | main.go handleAcliSync、acli_package.go | 探测官方源 → 比对 → 下载 → 上传 → 安装 → 验证 → 回执 |
| 桥日志直传 | log_uploader.go | 只写令牌 + outbox 落盘 + 批量 gzip 直传平台 |

### 1.3 三个结构性问题

1. **无编译期边界**：功能域之间、原语与编排之间没有 import 约束，
   耦合只能靠自觉维持——main.go 膨胀到 6480 行是机制性结果，不是纪律问题。
2. **中央 switch 耦合**：新增任何一种 WS 消息都必须改 main.go 的中央分发 switch，
   所有功能域的入口知识集中在一个文件里。
3. **原语重复实现**：远端写文件 ×4、远端探测 ×3、分片传输 ×4、shell 转义 ×4（含 Python）、
   设备标记 ×4——同构逻辑各写一遍，质量参差（config 直写非原子是 P0，
   插件无备份 rm -rf 是 P0）。详见收口设计 §1。

### 1.4 体积问题（实测归因）

分发的 exe 为 **16.47MB**（17,265,152 字节），CI 已做满 strip
（`-trimpath -s -w -buildid=`）。对未 strip 构建做 `go tool nm` 逐符号归因：

| 构成 | 大小 | 占比 | 重构可消除？ |
|------|------|------|-------------|
| Go runtime（含 pclntab 4.8MB 函数元数据） | ~7.1 MB | 46% | 否，语言固定成本 |
| Go 标准库（crypto/tls、net/http、json、regexp…） | ~3.9 MB | 25% | 否，功能必需 |
| 字符串池/链接元数据 | ~1.4 MB | 9% | 微量 |
| OTel 栈（otel+gRPC+protobuf+gateway，5633 函数占全部 29%，含元数据分摊） | ~3.6 MB | 22% | **可剥离**（§6） |
| SSH/SFTP/websocket 依赖 | ~0.45 MB | 3% | 否，核心功能 |
| **全部业务代码（main 包 19576 行）** | **~0.67 MB** | **4.3%** | 上限即此 |

**结论：拆解/复用/砍死代码对体积几乎无效（业务代码总共 0.67MB）。**
体积杠杆在压缩与依赖剥离（§6），重构的指标是可维护性与正确性，不背体积指标。

---

## 二、架构范式与业界最佳实践参照

### 2.1 第一性原理：桥是"两张脸"的代理进程

terminal_bridge 的本质是联通云端与内网：

```
云端（htp 平台）  ←── 云面 ──→  桥（terminal_bridge）  ←── 节点面 ──→  内网（HCI）
```

桥有且只有两张脸，每张脸有自己的协议、信任模型与原语集：

| | 云面（uplink） | 节点面（remote） |
|---|---|---|
| 协议 | HTTPS / WebSocket | SSH（exec channel + sftp subsystem） |
| 对端 | 平台服务（api-gateway、case-service） | 客户 HCI 宿主机（极简系统：无 base64/stat） |
| 信任模型 | 令牌绑定工单/任务；自签证书显式放宽 | 用户凭据 SSH 认证；前端透传内容不可信需二次校验 |
| 原语 | 分片上传、pace、TLS 信任、令牌、WS 信封 | 写文件、探测、执行、转义、标记、断点拉取 |

再叠加**数据面 / 控制面**区分（通信网络的标准范式）：

- **数据面**：字节搬运——日志包拉取（节点面 Pull）、制品/日志分片上传（云面 Push）、SFTP 下载；
- **控制面**：意图与事实——期望状态下发（Provider）、状态探测（probe）、回执（receipt）、证据（evidence）。

**范式参照**：SSH 协议本身只有 exec + sftp 两种 channel，印证"桥 = 远端执行 + 文件传输"
的本质抽象；Envoy/Istio 的 data plane / control plane 分离是本区分的直接来源。

### 2.2 能力四象限 + 五个补全项（命令式层）

节点面原语的能力清单可用四象限表达，与业界成熟代理系统同构：

| 象限 | 本设计模块 | Ansible 核心模块 | Salt | K8s/Terraform |
|------|-----------|-----------------|------|---------------|
| push（流出） | remote/fs.WriteFile | copy / template | execution modules | — |
| pull（流入） | remote/transfer.Pull、sftp | fetch | — | — |
| check（状态检查） | remote/probe | stat / setup(facts) | grains | readiness probe / data source |
| action（执行动作） | remote/exec | command / shell | execution modules | CRI / provider CRUD |

**两个范畴纪律**（对抗性审查得出）：

1. **方向是操作的参数，不是模块划分轴**。fs 与 transfer 的区别不是方向而是语义：
   fs 把文件当**状态**（KB 级、原子替换、校验自洽、退出码 20-23）；
   transfer 把文件当**字节流**（百 MB 级、断点、分片、pace）。
   transfer 同时含 Pull（流入）与 Push（流出），按语义必须同模块。
   业界同例：Ansible copy/fetch 是两个模块，共享同一套 connection + 临时文件 + checksum 机制。
2. **四象限只覆盖节点面一条腿**，云面原语独立成 uplink（§2.1）。

四象限之外还有五类不可省略的补全项：

| 补全项 | 模块 | 为什么象限装不下 |
|--------|------|-----------------|
| 安全渲染（quote） | remote/quote.go | 不是动作，是动作的注入面控制层 |
| 结果译码（marker） | remote/marker.go | check 是"主动去问"，marker 是"听懂回答" |
| 并发控制（guard） | reconcile/guard.go | 事故发生在"多次动作之间"，不是单次动作内 |
| 持久化（state） | reconcile/state.go | 断点恢复与对账基线 |
| 事后证明（verify） | reconcile/activate.go | 事前探测（决策输入）≠ 事后校验（成功证明）；"任务成功但少收了"缺的就是这一环 |

### 2.3 声明式 / 命令式两层分离（本设计的核心范式）

四象限回答"**能做什么**"（能力），但四条链路的既有事故没有一个是能力缺失，
全部是"**该不该做、做坏了怎么办**"（决策）：无备份 rm -rf、写不全也下发、
回滚误伤未写节点、探测失败伪装最新。决策必须由能力之上的独立层回答：

```
声明式层（reconcile）：期望 → 比对 → 决策 → 编排 → 证明 → 回执
命令式层（remote/uplink）：pull / push / check / action
```

业界同构范式（本设计逐一对标）：

| 系统 | 命令式层 | 声明式层 |
|------|---------|---------|
| **Salt** | execution modules | **state modules**（同名同构：声明期望状态，幂等收敛） |
| **Ansible** | modules | playbook + changed_when/check_mode |
| **Terraform** | provider CRUD | **plan → apply**（先比对出 diff，再执行） |
| **Kubernetes** | CRI / client-go | **controller reconcile loop**（期望 vs 观测，持续收敛） |

reconcile 骨架即 K8s controller 思想在 SSH 桥上的实例化：
`Provider.Desired → state.Load + probe → plan.Compare → guard.Acquire →
fanout.WriteAll → activate.Install → Smoke/Rollback → state.Save → receipt`。
详细规格见收口设计 §2-§3，本文不重复。

### 2.4 六边形架构（Ports & Adapters）：接口缝范式

公共模块禁止 import main（否则循环依赖），依赖倒置通过**最小接口缝**实现。
Go 标准库的两处同构范式：

| 范式 | 端口（契约，零逻辑） | 策略层（消费端口） | 适配器 |
|------|--------------------|-------------------|--------|
| net/http | RoundTripper | http.Client（超时/重试/重定向） | Transport |
| database/sql | driver.Driver | sql.DB（连接池/事务） | 各数据库驱动 |
| **本设计** | **remote.Runner** | remote/exec（超时/分片/逐片 exit_code） | sshx.SSHSession |

```go
// shared/remote/runner.go —— 端口定义
type Runner interface {
    RunQuickCommand(ctx context.Context, command string) (stdout string, exitCode int, err error)
    WriteStdin(ctx context.Context, command string, r io.Reader) (stdout string, exitCode int, err error)
}
```

三重收益：解循环依赖；FakeRunner 注入使全部编排逻辑可离线单测；
接口（十年不变）与策略（随现场演进）分文件隔离变化频率。

### 2.5 Go 工程范式

| 范式 | 出处 | 本设计落点 |
|------|------|-----------|
| `cmd/` + `internal/` 布局 | Go 社区标准项目布局（golang-standards/project-layout） | cmd/bridge 薄入口；internal 编译器强制模块外不可见 |
| 按资源域命名（名词，非动词） | Go 标准库 net / os / io/fs / crypto/tls | remote/fs.go 的 fs = filesystem，直接对标 io/fs 的 ReadFile/WriteFile/Stat |
| 类型属于拥有其行为的包 | Go 谚语 "accept interfaces, return structs"；领域驱动设计 | DesiredState 住 reconcile，不进 models（防贫血模型） |
| 平台差异文件后缀 | Go build constraints（_unix.go / _windows.go） | delivery 的 disk/mem 平台文件保留后缀约定 |
| 构建标签分离可选依赖 | Go build tags；OTel 社区 no-op 实践 | telemetry 双实现（真实/noop），桌面 exe 剥离 OTel |

### 2.6 可观测性范式：三信号分治

业界事实标准 **OpenTelemetry** 把可观测性分为 traces / metrics / logs 三信号。
terminal_bridge 现状盘点与归属裁决：

| 信号 | 现状实现 | 消费者 | 目标归属 | 可剥离？ |
|------|---------|--------|---------|---------|
| Traces | initTelemetry + otel.Tracer 散布调用（main.go） | 运维 collector（OTLP HTTP） | shared/telemetry（门面 + build tags） | 桌面可剥离（从未启用） |
| Metrics | promMetrics 自定义结构 + /metrics 手渲染 Prometheus 文本 | 运维抓取 | app/metrics.go | 否（进程运维） |
| Logs·本地 | logHub 本地 JSONL 落盘/轮转 + bridge-stderr.log | 现场排障 | obslog/hub.go | 否 |
| Logs·证据 | logUploader 直传（令牌/outbox/水位补传）+ WS bridge_log 实时推送 | **平台诊断 DB**（工单绑定、event_id 幂等） | obslog | **否（核心诊断功能）** |

**裁决：obslog 独立保留为功能域，不并入 telemetry。** 三条理由：

1. **消费者不同**：telemetry 的 traces 去运维 collector；obslog 的事件是工单绑定的
   诊断证据，进平台业务库、参与诊断流程——后者是业务功能，不是运维附属。
2. **剥离性冲突**：telemetry 必须对桌面 build-tag 可剥离（§6）；obslog 若并入一起被剥，
   桌面就丢了证据直传 = 功能回归。两者生命周期相反，必须分包。
3. **协议非通用**：obslog 携带平台专有契约（X-Bridge-Upload-Token、event_id 幂等、
   实例水位），不是 OTel logs 的通用语义；硬套 OTel logs 反而引入新依赖负重，与体积目标矛盾。

包名取 **telemetry**（OTel SDK 自身术语，短名词），不逐字对齐 backend 的 observability——
对齐的是"独立可观测性模块"的结构，不是字符串。

**门面前提**：现状代码直接调用 `otel.Tracer(...)` 散布 main.go 各处，剥离的前提是先立门面：
telemetry 包暴露最小 API（Start/AddEvent/SetStatus/End），调用方只依赖门面；
真实实现（build tag `telemetry`）包装 OTel，默认实现为零成本 noop。
桌面 exe 从不设置 `HCI_BRIDGE_OTEL_ENDPOINT`（仅集群 Pod 的 Helm deployment 配置），
即桌面形态 tracing 导出从未启用——剥离对桌面是**零功能损失**（§6）。

### 2.7 范式 → 模块映射总表

| 范式 | 来源 | 对应模块 |
|------|------|---------|
| 两张脸代理 + 数据面/控制面 | SSH 协议本质；Envoy/Istio | shared/uplink（云面）vs shared/remote（节点面） |
| 能力四象限 | Ansible/Salt execution modules | remote/{fs,probe,exec,transfer} + sftp 域 |
| 声明式收敛 | K8s controller / Salt state / Terraform plan-apply | shared/reconcile |
| 端口与适配器 | 六边形架构；net/http、database/sql | remote/runner.go + sshx 适配 |
| 资源域命名 | Go io/fs | remote/fs.go、probe.go |
| 编译期可见性边界 | Go internal/ | internal/ 全树 |
| 注册式路由（微内核） | 插件式架构；http.ServeMux | app/dispatch.go |
| 门面 + 构建标签 | OTel noop 实践 | shared/telemetry |
| 线上契约与领域类型分离 | DTO vs Domain Model | shared/models（仅 DTO） |

---

## 三、目标目录结构（完整真实结构 + 逐文件说明）

### 3.1 目录总树

```
terminal_bridge/
├── cmd/
│   └── bridge/
│       └── main.go                  # 薄入口：flag 解析 + 依赖装配 + 启动（<200 行）
├── internal/                        # 编译器强制：模块外不可 import
│   ├── app/                         # 【进程骨架域】微内核
│   │   ├── server.go                # HTTP/WS 服务、/live /ready /status /metrics、双形态监听
│   │   ├── config.go                # runtimeConfig、desktop/cluster 模式、Origin 策略
│   │   ├── dispatch.go              # 注册式 WS 消息路由表（取代中央 switch）
│   │   ├── lifecycle.go             # conn.*/session.* 生命周期事件、stderr 重定向
│   │   ├── metrics.go               # promMetrics 与 /metrics Prometheus 文本渲染
│   │   └── version.go               # Version/CommitID/BuildTime 注入与 getVersion
│   ├── sshx/                        # 【SSH 连接域】Runner 的适配器
│   │   ├── session.go               # SSHSession：dial、认证、会话生命周期
│   │   ├── keepalive.go             # 保活与断连判定
│   │   └── busy.go                  # 会话 busy 互斥、端口恢复
│   ├── terminal/                    # 【交互终端域】
│   │   ├── interactive.go           # ssh_connect/input/inject_command/resume/pong/disconnect/list_sessions
│   │   ├── pty.go                   # PTY 分配与窗口尺寸
│   │   ├── input_router.go          # 输入路由（交互 shell vs 注入命令）
│   │   ├── exec.go                  # ssh_exec_command / ssh_exec_process 的 WS 域逻辑
│   │   └── vmconsole.go             # vm_console_op（转义调 shared/remote/quote）
│   ├── acli/                        # 【acli 两阶段域】acli_sync 的 WS 协议边缘（工具+插件）
│   │   ├── handler.go               # acli_sync WS 入口、两阶段串联、retry_stage（all/upload/install）语义
│   │   └── progress.go              # acli_sync_progress/result 信封（对外契约冻结，仅增可选字段）
│   ├── sftp/                        # 【SFTP 下载域】
│   │   ├── handler.go               # sftp_op：list/download/cancel 的 WS 入口
│   │   ├── download.go              # SFTP channel 下载 + tail -c 降级续拉（复用 remote/transfer）
│   │   └── sanitize.go              # 文件名清洗：路径穿越/控制字符/Windows 保留名
│   ├── logcollect/                  # 【日志收集域】
│   │   ├── handler.go               # log_collect 八个 action 的 WS 入口
│   │   ├── collect.go               # 采集编排（create/progress/stat/cancel/capabilities）
│   │   ├── errors.go                # LC-* 错误码表
│   │   ├── evidence.go              # 证据上报（zip 中央目录只读、网络分量测量、票据透传先例）
│   │   ├── hostresults.go           # 逐主机结果归并
│   │   └── contract.go              # 契约生成代码（保留生成器路径约定）
│   ├── delivery/                    # 【日志包交付域】
│   │   ├── handlers.go              # delivery_submit/status/resume/cancel
│   │   ├── job.go                   # 落盘状态机（已拉字节/已传分片/阶段）
│   │   ├── runner.go                # 作业执行器（传输调 remote/transfer + uplink）
│   │   ├── store.go                 # 本地作业存储
│   │   ├── disk.go / disk_unix.go / disk_windows.go   # 磁盘空间探测（平台差异）
│   │   ├── mem_unix.go / mem_windows.go               # 内存探测（平台差异）
│   │   ├── concurrency.go           # 作业并发控制（与 reconcile/guard 主机键对齐）
│   │   └── stall.go                 # 停滞检测
│   ├── obslog/                     # 【桥事件日志域】自动直传+补传（工单证据通道，不可剥离）
│   │   ├── hub.go                   # logHub：publish/本地 JSONL 落盘/轮转/WS 实时订阅
│   │   ├── outbox.go                # 落盘 outbox（分段、字节预算、补传）
│   │   ├── uploader.go              # 单 worker 批量 gzip 直传（调 uplink）
│   │   └── seed.go                  # 水位补传（含 stderr 包装，L1-L4 完备性增强见 §3.6）
│   └── shared/                      # 【公共复用层】对齐 backend/shared 仓库惯例
│       ├── remote/                  # 横向原语·节点面（严格只认识 Runner）
│       │   ├── runner.go            # Runner 接口缝（端口定义，零逻辑）
│       │   ├── fs.go                # WriteFile：tmp+校验+mv+退出码 20-23（文件-as-状态）
│       │   ├── probe.go             # ProbeFile/ProbeVersion：事前探测（事实，非结论）
│       │   ├── exec.go              # Run/RunChunked：超时、分片逐条 exit_code（策略层）
│       │   ├── transfer.go          # Pull：节点→桥断点续拉（文件-as-流，仅节点面方向）
│       │   ├── quote.go             # shellQuote 唯一实现（安全渲染层）
│       │   └── marker.go            # SYNC_* 设备标记协议 + 过渡期旧格式适配（结果译码层）
│       ├── uplink/                  # 横向原语·云面（HTTP/WS，不依赖 Runner）
│       │   ├── chunk.go             # Push：桥→平台分片上传、断点续传、逐片回执
│       │   ├── pace.go              # 弱网自适应速率（delivery 与 logcollect 共用）
│       │   ├── trust.go             # 自签证书信任策略（授权放宽、留痕、不放松鉴权）
│       │   └── token.go             # 只写令牌/任务令牌的携带与校验约定
│       ├── reconcile/               # 纵向骨架（声明式层，数据驱动）
│       │   ├── descriptor.go        # DesiredState/FileSpec/InstallSpec/SmokeSpec/Policy
│       │   ├── provider.go          # Provider 接口 + LatestProbe/InlineMessage/PluginManifest
│       │   ├── plan.go              # Compare：Skip/Install/BlockedDowngrade（账本+节点事实双对齐、差量三态）
│       │   ├── activate.go          # 三钩子 + written-set + 无备份不删（构造性不变量）
│       │   ├── fanout.go            # WriteAll/RollbackWritten（集合性写入，只回滚已写入）
│       │   ├── guard.go             # 主机键互斥 + 租约 fencing（exec_id 入分片路径）
│       │   ├── state.go             # NodeFact（探测，不持久化）vs AppliedState（账本，持久化）
│       │   ├── receipt.go           # Result{Status,Stage,ErrorCode,Retryable,Evidence[]}
│       │   └── kinds/               # 消费者：差异全部在 descriptor 字段取值
│       │       ├── acli/            # acli_sync 两阶段：package.go（下载/缓存）、version.go（判序）、
│       │       │                    #   descriptor.go（阶段一）、plugin.go（阶段二差量 descriptor）
│       │       ├── spc/             # 场景覆写：descriptor.go、restore.go（持久还原意图）
│       │       └── config/          # -c 配置：descriptor.go、validate.go（二次校验）
│       ├── telemetry/               # 可观测性（门面 + 双实现）
│       │   ├── telemetry.go         # 门面 API：Start/AddEvent/SetStatus/End（调用方唯一依赖）
│       │   ├── otel.go              # //go:build telemetry —— OTel 真实实现（集群镜像）
│       │   └── noop.go              # //go:build !telemetry —— 零成本空实现（桌面 exe）
│       └── models/                  # 仅线上契约 DTO（准入规则见 §3.5；utils 定论不建）
│           ├── ws.go                # WS InMessage/OutMessage 信封结构
│           └── delivery.go          # delivery job 视图等跨进程 DTO
├── go.mod / go.sum
├── Dockerfile
└── README.md
（4 个历史二进制产物移出源码目录；构建脚本统一在 scripts/）
```

> **插件不独立成域**：插件差量同步是 acli_sync 的第二阶段，协议边缘归 `internal/acli/`
> （两阶段串联），声明式胶水归 `kinds/acli/plugin.go`。不设 `internal/plugin/` 域、
> 不设 executor 回退开关——旧 relay 推送链路与 admin-ui 下发入口由 D1 直接删除（收口设计 §六 D1）。

### 3.2 逐包说明与范式对应

| 包 | 范式对应（§2） | 职责边界 | 禁止事项 |
|----|---------------|---------|---------|
| cmd/bridge | Go 标准布局 | 只做装配：解析 flag → 构造各域 → 注入依赖 → 启动 | 不含任何业务逻辑 |
| app | 微内核 + 注册式路由 | 进程骨架、双形态、消息路由、生命周期 | 不认识任何具体消息的业务语义 |
| sshx | 端口适配器（六边形） | SSH 连接与会话管理；实现 remote.Runner | 不含 WS 协议知识 |
| terminal | 功能域 | 交互终端/命令执行/VM console 的 WS 域逻辑 | 不直接拼 shell 转义（调 quote） |
| acli | 功能域（两阶段） | acli_sync 的 WS 协议边缘（retry_stage、progress 信封、两阶段串联） | 声明式逻辑下沉 kinds/acli，域内零编排 |
| sftp | 功能域 + 四象限 pull | 面向用户的下载（文件名主权、落盘主权） | 不把用户交互语义塞进原语层 |
| logcollect | 功能域 | 采集编排、错误码、证据；spc/config 差异下沉 kinds | 不自实现写文件/探测/传输 |
| delivery | 功能域 | 作业状态机与执行权；传输全部委托原语 | 不自实现分片/pace |
| obslog | 功能域 + 证据通道（§2.6） | logHub 事件管道：本地落盘/直传/补传/WS 实时推送 | 不自实现 TLS 信任/分片；**不得 build-tag 剥离**（核心诊断功能） |
| shared/remote | 四象限 + 端口适配器 + io/fs 命名 | 节点面原语，只认识 Runner | 禁止期望值语义；禁止 import main/sshx/各域 |
| shared/uplink | 两张脸·云面 + 数据面 | 云面传输原语，不依赖 Runner | 禁止 import remote（两脸互不认识） |
| shared/reconcile | 声明式收敛（K8s/Salt/Terraform） | 编排骨架 + 消费者（acli 两阶段/spc/config） | 禁止命令字符串；禁止 import main/各域 |
| shared/telemetry | OTel 门面 + build tags | **仅 traces**（spans/propagation）；logs 归 obslog、metrics 归 app（§2.6） | 门面签名禁止出现 otel 类型 |
| shared/models | DTO/领域分离 | 跨进程线上契约 | 禁止领域类型（DesiredState 等） |

**delivery 可扩展性边界（备注）**：delivery 的作业引擎（落盘状态机/断点续传/pace/fencing/
代际顶替）与交付物类型无关；唯一的日志专属闸门是源路径白名单 `normalizeArchivePath`
（只接受 `/sf/data/local/acli/tmp/data/log_collect/` 下的 .zip）。
将来若有第二类交付物，只需放宽白名单 + 增加一个前端入口，**作业引擎零改动**。

### 3.3 依赖方向图（编译器强制，单向无环）

```
cmd/bridge
    └── app ──→ {terminal, acli, sftp, logcollect, delivery, obslog, reconcile/kinds}
                    │                │              │
                    ▼                ▼              ▼
                sshx ──→ shared/remote ←── shared/reconcile
                    │                │              │
                    │                ▼              │
                    │        （Runner 接口缝）        │
                    ▼                               ▼
              shared/uplink ←── {delivery, obslog, logcollect}
              shared/telemetry ←── app（门面注入各域）
              shared/models ←── 所有需要 WS/HTTP DTO 的包
```

**硬禁令**（CI 以 import 扫描强制）：

1. shared/remote、shared/reconcile、shared/uplink 禁止 import main 及任何功能域；
2. remote 与 uplink 互不 import（两张脸各自独立）；
3. reconcile 禁止 import delivery/logcollect（骨架不认识消费者）；
4. 功能域之间禁止互相 import（跨域协作经 app 装配或 shared 原语）；
5. telemetry 门面签名禁止出现 otel 类型（否则 noop 实现被迫引 OTel，剥离失效）；
6. reconcile/kinds 零 WS 协议知识：每种 WS 消息的协议边缘归且仅归一个功能域
   （acli/logcollect），kinds 只放声明式胶水（descriptor/provider/verify）。

### 3.4 对收口设计的三处结构性修订

| # | 收口设计原状 | 本文修订 | 理由 |
|---|-------------------|---------|------|
| 1 | remote/transfer.go 含 Pull + Push | **Push 移入 shared/uplink/chunk.go**；remote/transfer.go 仅留 Pull | Push 是桥→平台 HTTP，不用 Runner，属云面；留在 remote 违反"remote 只认识节点面"纪律（两张脸范式审查得出） |
| 2 | 公共模块路径 internal/remote、internal/reconcile | 统一落位 **internal/shared/** 下 | 对齐 backend/shared 仓库惯例（models/observability/utils 同构）；Go internal/ 提供编译期可见性 |
| 3 | handleAcliSync 整体映射到 kinds/acli | **协议边缘拆至 internal/acli/ 域（两阶段）**，kinds/acli 留声明式胶水（含 plugin.go 阶段二）；**插件不独立成域** | 对称性规则：每种 WS 消息的协议边缘归且仅归一个功能域（logcollect 已是此形状）；kinds 不得拖入 WS 知识；插件与工具同触发同骨架，独立成域会重新制造链路分裂 |

收口设计的其余全部结论（Runner 缝、包名 reconcile、golden 分轨、备份隔离、
spc 还原生命周期、PluginManifest 票据拉取、差量三态、P-1①②）不变，继续有效。

### 3.5 models 与 utils 定论：utils 不建，models 保留

utils/models 是业界公认的耦合度腐化高发区（垃圾抽屉反模式：谁都 import、改一行全仓重编译）。
逐一核对现状代码后的定论：

- **utils：不建这个目录。** 现状全部候选函数都有明确域归属：
  `formatOutputPreview/truncateKeepTail` 是命令输出处理（归 terminal/exec）、
  `shellQuote` 是安全原语（归 remote/quote.go）。**不存在无家可归的纯函数**，
  建空目录只会诱发未来的垃圾抽屉。Go 谚语 "a little copying is better than a little dependency"。
  判据留档：删掉某函数若会让链路产生安全/正确性风险，它就不是 util，禁止进任何工具包。
- **models：保留，这是拆包后的编译必然，不是便利。** 已核实：`InMessage/OutMessage`
  现定义于 main.go、被所有 handler 引用；拆成多个功能域后，信封结构必须住在共享叶子包，
  否则每个域都要 import app → 循环依赖。准入规则写死在 CI 评审清单：
  只放跨进程线上契约 DTO（WS 信封、job 视图）；领域类型（DesiredState、AppliedState、
  WriteEntry）的不变量由行为包维护，必须住行为包——Go 谚语
  "types belong to the package that owns their behavior"。
  backend 的 models 集中是 Python/Pydantic 生态惯例，Go 侧只对齐结构不对齐内容。

### 3.6 obslog 完备性增强（L1-L4）与手动上传入口退役路径

**现状结论（代码核实）**：自动直传与本地 HCI-TerminalBridge-Logs **尚未完全一致**，
存在四类系统性缺口。手动兜底入口（前端 BridgeLogUploader.vue → 后端 API，
受 bridgeLogs.uploadEnabled 开关控制，**不经过 bridge 进程**）现阶段不可删：

| # | 缺口 | 代码证据 |
|---|------|---------|
| ① | 令牌启用前的旧历史只补"最近 2 个已轮转文件 + 最多 5000 条"，更早历史永不自动上传 | log_uploader.go defaultSeedMaxFiles=2 / defaultSeedMaxEntries=5000 |
| ② | 队列 ≥5000 或 outbox 超字节上限时 enqueue 静默丢弃（本地文件仍有这些条目） | log_uploader.go enqueue 双重保护 |
| ③ | bridge-stderr.log（panic/runtime fatal 唯一出口）不是带 seq 的 JSONL，补传逐行解析跳过，**panic 现场永不自动上传** | lifecycle.go redirectStderrToFile + readLogFileEntries |
| ④ | 补传排除正在写的当前文件，其中被丢弃/令牌前条目须等轮转后才可能进窗口 | recentBridgeLogFiles 的 exclude 参数 |

**增强设计（L1-L4，落 obslog 域；目标 = 本地保留窗口内条目至少一次必达平台）**：

| # | 增强 | 要点 |
|---|------|------|
| L1 | 水位全量补传 | 废除"2 文件/5000 条"固定窗口，改为"水位之后全部文件 + 字节/时间预算"；预算用尽记录进度、下轮继续 |
| L2 | 溢出落盘代替丢弃 | outbox 分段；内存队列上限保留，溢出部分留在磁盘等 worker 消费；仅总磁盘预算超限时 drop-oldest 段，并**发 gap 标记事件**（log_upload.gap，携带丢弃区间）——平台可知洞，不再静默丢失 |
| L3 | stderr 包装入管道 | seed 扫描 bridge-stderr.log，非 JSON 行包装为 logEntry（event=bridge.stderr、level=ERROR）；水位按**字节偏移**（非 seq）；panic 现场在重启后必达平台 |
| L4 | 当前文件纳入补传 | 去掉 exclude，读到最后一个完整行为止；与实时 enqueue 的重复由后端 event_id 幂等去重（现有机制） |

**验收（等价性测试）**：构造令牌前历史、上限溢出、panic 模拟、进程重启四场景，
断言平台收到的 event_id 集合 ⊇ 本地保留窗口内条目集合（gap 标记区间除外）；
gap 标记事件本身必须可查。

**手动入口退役路径（gated 后续，非 bridge PR）**：

1. L1-L4 等价性验收通过 + 分发版本覆盖达标（无令牌能力的旧 exe 由自动更新自然淘汰）；
2. 观察期（建议 ≥2 周）内"证据缺失"类工单为零；
3. 后端 bridgeLogs.uploadEnabled 开关灰度关闭 → 前端删除 BridgeLogUploader.vue（独立 PR）；
4. 开关保留一个版本周期作为回滚手段。

---

## 四、现有文件 → 目标模块完整迁移映射

### 4.1 main.go（6480 行）拆分映射

| 现有内容 | 目标 | 范式对应 |
|---------|------|---------|
| flag 解析、main()、版本注入 | cmd/bridge/main.go + app/version.go | 薄入口 |
| runtimeConfig、双形态、newHTTPHandler、健康端点、corsWebSocketHandler | app/server.go + app/config.go | 微内核 |
| WS 消息 switch（17 种 type） | app/dispatch.go（注册式路由） | 微内核 + 插件式注册 |
| SSHSession、dial、keepalive、busy 互斥、端口恢复 | sshx/ | 端口适配器 |
| ssh_connect/input/inject/resume/pong/disconnect/list_sessions、PTY、输入路由 | terminal/interactive.go + pty.go + input_router.go | 功能域 |
| ssh_exec_command / ssh_exec_process | terminal/exec.go | 功能域 |
| vm_console_op + vmConsoleShellQuote | terminal/vmconsole.go（转义收口 remote/quote.go） | 功能域 + 安全渲染 |
| handleAcliSync 的 WS 入口/retry_stage/progress 信封/两阶段串联 | internal/acli/ | 功能域协议边缘（两阶段） |
| acli 探测/下载/缓存/安装编排 + compareAcliVersions | shared/reconcile/kinds/acli/（descriptor.go/package.go/version.go） | 声明式消费者·阶段一 |
| 插件差量同步声明式胶水（阶段二） | shared/reconcile/kinds/acli/plugin.go | 声明式消费者·阶段二 |
| logHub（publish/本地落盘/轮转/WS 订阅） | obslog/hub.go | 事件管道 |
| promMetrics 与 /metrics 渲染 | app/metrics.go | 进程指标 |
| initTelemetry + 全部 otel 直接调用 | shared/telemetry/（先立门面再迁调用点） | OTel 门面 |
| uploadStream（死代码） | **删除** | — |
| WS InMessage/OutMessage 结构 | shared/models/ws.go | DTO |

### 4.2 独立文件映射

| 现有文件 | 行数 | 目标 | 备注 |
|---------|------|------|------|
| lifecycle.go | 419 | app/lifecycle.go | 原样迁移 |
| tls_trust.go | 63 | shared/uplink/trust.go | 云面原语 |
| sftp.go | 1120 | sftp/handler.go + download.go + sanitize.go | 按职责三拆 |
| log_collect.go | 2176 | logcollect/handler.go + collect.go | 写/探测改调原语 |
| log_collect_config.go | 193 | shared/reconcile/kinds/config/ | 收口 S2 第一消费者 |
| log_collect_spc.go | 205 | shared/reconcile/kinds/spc/ | 单片原语 |
| log_collect_spc_fanout.go | 1292 | shared/reconcile/fanout.go（泛化）+ kinds/spc/（专属） | 收口 S3 |
| log_collect_transfer.go | 583 | Pull → shared/remote/transfer.go；uploadArchiveParts → shared/uplink/chunk.go | 两张脸分拆 |
| log_collect_errors.go | 441 | logcollect/errors.go | LC-* 码表保留 |
| log_collect_evidence.go | 809 | logcollect/evidence.go | 票据透传先例 |
| log_collect_host_results.go | 35 | logcollect/hostresults.go | |
| log_collect_contract_generated.go | 613 | logcollect/contract.go | 生成代码，保留生成器约定 |
| delivery_handlers.go | 190 | delivery/handlers.go | |
| delivery_job.go | 412 | delivery/job.go | 落盘状态机 |
| delivery_runner.go | 1933 | delivery/runner.go + 传输原语抽出 | pull → remote/transfer；upload → uplink/chunk |
| delivery_pace.go | 223 | shared/uplink/pace.go | 通用化，logcollect 复用 |
| delivery_local_store.go | 370 | delivery/store.go | |
| delivery_disk.go / _unix / _windows | 334 | delivery/disk_*.go | 平台后缀约定保留 |
| delivery_mem_unix.go / _windows | 81 | delivery/mem_*.go | |
| delivery_concurrency.go | 111 | delivery/concurrency.go | 与 guard 主机键对齐 |
| delivery_stall.go | 48 | delivery/stall.go | |
| acli_package.go | 784 | 写入协议 → shared/remote/fs.go；下载/缓存/探测 → kinds/acli/package.go | 收口 S1/S4 |
| log_uploader.go | 661 | obslog/outbox.go + uploader.go + seed.go | 自动直传+补传；**手动上传桌面日志是前端 BridgeLogUploader.vue → 后端 API，不属 bridge**（退役路径见 §3.6） |
| ~9500 行测试 | | 跟随各自包迁移 | **先迁测试再迁实现**，测试是迁移安全网 |
| terminal_bridge*.exe / darwin 二进制 ×4 | | 移出源码目录 | 分发产物只留 frontend/customer/public/downloads/ |

### 4.3 D1 删除映射（relay 推送链路与 admin-ui 下发入口）

| 现有文件/对象 | 处置 | 备注 |
|---------|------|------|
| backend/case-service/app/services/acli_plugin_relay.py | **删除** | 租约/心跳/部署编排 |
| backend/case-service/app/services/acli_plugin_bridge.py | **删除** | WS 分片下发执行器 |
| backend/case-service/app/services/acli_plugin_admin.py 下发部分 | **删除** | 保留规格保存与 hash 计算（manifest 事实源） |
| backend/case-service/app/routes/acli_plugins.py 下发部分 | **删除** | 保留规格维护路由 |
| frontend/admin 插件页「保存并下发」/任务轮询 | **删除** | 保留规格维护（列表/编辑/包上传） |
| database acli_plugin_deployment 表 / Helm relayEnabled | **删除** | 规格表 acli_plugin_spec/acli_plugin 保留 |

### 4.4 迁移的二维分离原则

**目录搬迁（R0）与逻辑收口（S1-S4/D1）是两个独立 PR 维度，绝不混做**：

- R0 只做：建包、移动文件、改 package 子句、修 import、测试同迁。
  验收 = 全量测试绿 + 行为冒烟，**不改任何一行逻辑**；
- S1-S4/D1 在 R0 骨架内做逻辑收口与删除，每阶段独立可回滚（沿用收口设计）。

一次大爆炸（边搬边改）会让任何回归都无法归因——这是迁移本身的第一性原理。

---

## 五、关键接口契约（摘要）

完整规格见收口设计 §2/§4，此处仅列跨包契约的最小集：

```go
// shared/remote/runner.go —— 节点面端口
type Runner interface {
    RunQuickCommand(ctx context.Context, command string) (string, int, error)
    WriteStdin(ctx context.Context, command string, r io.Reader) (string, int, error)
}

// shared/remote/fs.go —— 文件-as-状态
func WriteFile(ctx context.Context, r Runner, spec FileSpec) (WriteResult, error)
// WriteResult.Integrity 必须标注实际校验档位（sha256/md5/size），禁止静默降级

// shared/remote/transfer.go —— 文件-as-流（仅节点面）
func Pull(ctx context.Context, r Runner, spec PullSpec) (PullResult, error)

// shared/uplink/chunk.go —— 云面分片上传（不依赖 Runner）
func Push(ctx context.Context, spec PushSpec) (PushResult, error)

// shared/reconcile/provider.go —— 期望值来源
type Provider interface {
    Desired(ctx context.Context, req SyncRequest) ([]DesiredState, error)
}

// shared/telemetry/telemetry.go —— 可观测性门面（签名零 otel 类型）
func Start(ctx context.Context, name string, attrs ...Attr) (context.Context, Span)
type Span interface { AddEvent(string, ...Attr); SetStatus(code, msg string); End() }
```

---

## 六、体积优化（实测数据与路线）

### 6.1 实测结果（UPX 4.2.4，CI 同参数构建产物）

| 方案 | 体积 | 结论 |
|------|------|------|
| 现状 strip 无压缩 | 17,265,152 B = 16.47 MB | 基线 |
| UPX --best | 5,857,792 B = 5.59 MB | 未达 5MB |
| **UPX --lzma --best** | **4,951,040 B = 4.72 MB** | **达标，零代码改动** |
| 桌面剥离 OTel（预估）+ UPX lzma | ~3.6 MB | 留出增长余量 |

压缩后 exe 已实际运行验证（WSL interop 执行 `-version` 正常输出、exit 0）。
仓库有先例：7 月旧 x86 exe（2.2MB）即 UPX 压缩产物；build_windows.bat 已含可选 upx 逻辑。

### 6.2 OTel 剥离的依据与做法

- 依据：`initTelemetry` 仅在 `HCI_BRIDGE_OTEL_ENDPOINT` 非空时启用导出；
  全仓只有集群 Pod 的 Helm deployment 配置该变量，**桌面 exe 从未启用**，
  但每个客户都下载了 ~3.6MB 的 OTel/gRPC/protobuf 代码（占函数总数 29%）。
- 做法：先立 telemetry 门面（§2.6），调用点从 otel 直调迁到门面；
  CI 桌面产物不带 `telemetry` tag（noop），集群镜像带 tag（完整可观测性）。
- **桌面零功能损失，集群可观测性完整保留**——这不是砍功能，是砍"从未生效的负重"。

### 6.3 代价与知情决策

| 代价 | 说明 | 缓解 |
|------|------|------|
| 杀软误报率上升 | 加壳是恶意软件常用手法，部分 EDR 敏感 | 客户群为运维工程师，发布前抽样验证主流杀软 |
| AppLocker/WDAC 可能拦截加壳程序 | 企业策略环境 | 保留未压缩产物作为备选下载 |
| 启动解压开销 | 几十 ms 级 | 可忽略 |
| 版本清单一致性 | bridge-version.json 的 sha256 必须按**压缩后**产物计算 | CI 同 commit 产出 exe+清单（现有门禁逻辑不变） |

### 6.4 路线

1. **U0（当天）**：CI 构建脚本加 `upx --lzma --best`，16.47 → 4.72MB；
   源码目录 4 个历史二进制清出；
2. **U1**：telemetry 门面 + build tag 分离，桌面 → ~3.6MB；
3. **U2（可选）**：CI 加体积预算告警（如桌面产物 >6MB 报警），防回归。

---

## 七、落地顺序（与收口设计合并的统一路线）

| 阶段 | 内容 | 依赖 | 验收 |
|------|------|------|------|
| **P-1** | 当天止血（收口设计 P-1①②） | 无，独立 PR | config 断链无截断；10MB 上传不 413 |
| **U0** | UPX lzma 进 CI + 二进制清出源码目录 | 无，独立 PR | 产物 ≤5MB；清单 sha256 对齐；压缩产物运行冒烟 |
| **R0** | 目录骨架 + 纯搬迁（§4.4 二维分离）：建包、移文件、测试同迁、注册式路由 | P-1 合入 | 全量测试绿；行为冒烟；import 禁令扫描零违规 |
| **S1** | remote/uplink 原语抽取 + Runner 缝 + 删 uploadStream | R0 | golden（acli 路径逐字）+ 行为级（config/spc）；remote 零 import main |
| **S2** | reconcile 骨架 + config 消费者 | S1 | WS 契约不变；行为等价 |
| **S3** | fanout/activate/guard + spc 消费者 | S2 | spc 测试全绿 + 竞态用例 + 抗崩溃用例 |
| **S4** | acli_sync 两阶段接入（S4a/S4b/S4c）+ 修 B6/B7/B8 + 插件差量三态 + 票据 | S3 | 现有测试全绿 + golden + 三缺陷用例 + 差量三态 + 票据五用例 + 串联语义 |
| **U1** | telemetry 门面 + build tag 分离（可与 S2-S4 并行） | R0 | 桌面产物 noop 编译零 otel 符号；集群 tracing 不回归 |
| **L1** | obslog 完备性增强（§3.6 L1-L4，可与 S2-S4 并行） | R0 | 等价性测试四场景通过；零静默丢弃（gap 标记可查） |
| **D1** | relay 推送链路与 admin-ui 下发入口直接删除（可与 S2-S4 并行） | R0 | grep 零残留；admin-ui 规格维护回归；无 404 |

每阶段独立可回滚；R0 之后任何 S 阶段出问题可 revert 而不影响目录骨架。
手动上传入口退役是 L1 之后的 gated 后续（前端 + 后端开关 PR，前置条件见 §3.6），不占 bridge 阶段。

---

## 八、风险与对抗性审查

| # | 失败模式 | 加固 |
|---|---------|------|
| 1 | 边搬边改的大爆炸迁移，回归无法归因 | R0 纯搬迁与 S 逻辑收口二维分离（§4.4） |
| 2 | 搬迁后 import 环 | Runner 缝 + §3.3 硬禁令 CI 扫描；R0 验收含编译期环检测 |
| 3 | utils/models 腐化成垃圾抽屉 | §3.5 准入规则进评审清单；安全原语/领域类型禁入 |
| 4 | telemetry 门面泄漏 otel 类型，noop 被迫引 OTel | 门面签名零 otel 类型（硬禁令 5）；U1 验收 grep 符号 |
| 5 | uplink/remote 边界漂移（Push 又溜回 remote） | 两脸互不 import（硬禁令 2）；remote 只认识 Runner |
| 6 | 注册式路由丢失消息类型 | R0 迁移时以旧 switch 的 17 种 type 做清单逐一核对 + 路由表单测 |
| 7 | 测试迁移遗漏造成覆盖空洞 | 先迁测试再迁实现；R0 验收对比迁移前后测试函数清单 |
| 8 | 生成代码（contract）再生成路径失效 | 保留生成器路径约定，R0 验证再生成 diff 为空 |
| 9 | Windows 平台文件后缀误改导致交叉编译失败 | _unix/_windows 后缀原样保留；CI 双 GOOS 冒烟构建 |
| 10 | UPX 产物被杀软/策略拦截 | 保留未压缩备选产物；发布前抽样验证（§6.3） |
| 11 | 压缩后清单 sha256 失配误报版本不一致 | 清单按压缩后产物计算，exe+清单同 commit（现有 fail-closed 门禁） |
| 12 | 重构被误期待为体积优化 | §1.4 明示业务代码仅 0.67MB；体积指标归 U0/U1 |
| 13 | 桌面剥离 OTel 误伤集群可观测性 | build tag 只影响桌面产物；集群镜像带 tag；U1 验收集群 tracing 不回归 |
| 14 | 与收口设计文档漂移 | §3.4 明示三处修订；两文同版（v2.0）同步修订（变更历史留痕） |
| 15 | L2 分段 outbox 吃满客户机磁盘 | 总磁盘预算 + drop-oldest + gap 标记事件（平台可知洞，不静默） |
| 16 | L3 stderr 包装产生重复条目 | 字节偏移水位 + 后端 event_id 幂等 |
| 17 | L4 当前文件补传与实时 enqueue 重复 | event_id 幂等去重（现有机制，不新造） |
| 18 | 手动上传入口过早退役 | 三前置（等价性验收 + 版本覆盖 + 观察期），后端开关保留一个版本周期可回滚 |
| 19 | obslog 被误并入 telemetry 后随 build tag 剥离，桌面丢证据直传 | §2.6 裁决 obslog 独立域、不可剥离；CI 检查桌面产物含 obslog 符号 |
| 20 | 插件被重新独立成域/独立链路，制造链路分裂 | 硬禁令：插件协议边缘归 acli 域、胶水归 kinds/acli/plugin.go；不设 internal/plugin、不设回退开关 |
| 21 | D1 删不干净或误删规格维护 | grep 零残留 + 规格维护回归 + 无 404 用例；规格表保留 |

---

## 九、验收标准总表

| 阶段 | 验收 | 自动化 |
|------|------|--------|
| P-1 | 收口设计 P-1 全部用例 | 单测 + 集成 |
| U0 | 桌面产物 ≤5MB；压缩产物 -version 冒烟；清单 sha256 对齐 | CI |
| R0 | go test ./... 全绿；17 种消息路由核对；import 禁令零违规；测试函数清单前后一致 | CI 脚本 |
| S1 | golden（acli 逐字）+ 行为级（config/spc 故障注入无半截文件）；remote/uplink 零 import main | 单测 |
| S2-S3 | 收口设计 S2-S3 验收 | CI |
| S4 | 收口设计 S4 验收（两阶段串联、差量三态、票据五用例、B6/B7/B8 修正） | CI + 单测 |
| U1 | 桌面产物 go tool nm 零 otel 符号；集群 tracing 端到端不回归 | CI + 集成 |
| L1 | 等价性测试（令牌前历史/上限溢出/panic/重启四场景）；gap 标记可查；桌面产物含 obslog 符号 | 单测 + 集成 |
| D1 | grep 零残留；admin-ui 规格维护回归；无 404 | CI 脚本 + 集成 |
| 全程 | gofmt / go vet / 双 GOOS 冒烟构建 | CI |

---

## 十、术语表

| 术语 | 定义 |
|------|------|
| 云面 / 节点面 | 桥的两张脸：对平台（HTTP/WS）与对 HCI（SSH），原语集互不依赖 |
| 数据面 / 控制面 | 字节搬运 vs 意图/事实/回执 |
| 四象限 | pull/push/check/action，节点面命令式能力清单 |
| 声明式收敛 | 期望 vs 观测比对后执行差量，K8s controller/Salt state 同构 |
| 端口与适配器 | 六边形架构：公共模块定义接口（端口），外层提供实现（适配器） |
| Runner | 节点面最小执行接口缝，sshx.SSHSession 为其适配器 |
| 文件-as-状态 / 文件-as-流 | fs（原子替换+校验）与 transfer（断点+分片）的语义分界 |
| 微内核 + 注册式路由 | app 只做骨架，各域自注册消息 handler |
| 门面（Facade） | telemetry 对调用方暴露的最小 API，隔离 OTel 实现 |
| 证据通道（obslog） | 工单绑定的结构化事件直传，消费者是平台诊断 DB；区别于运维可观测性（telemetry/metrics），不可剥离 |
| DTO / 领域类型 | 线上契约结构（models）vs 拥有不变量的行为类型（住行为包） |
| 二维分离 | 目录搬迁（R0）与逻辑收口（S1-S4/D1）独立 PR，不混做 |
| 两阶段 | acli_sync 阶段一工具同步 + 阶段二插件差量同步，串行、同触发、同骨架 |
| PluginManifest | 阶段二拓扑：桥持短期只读票据拉平台插件清单（浏览器触发的拉模型） |
| 差量三态 | 新增→Install、有修改→Install、无修改→Skip |

---

## 十一、关联文档与文件

| 文件 | 角色 |
|------|------|
| docs/plans/2026-11-18-节点同步链路统一收口设计.md | 子方案：四条链路逻辑收口（P-1/S0-S4/D1 详细规格），本文 §3.4 对其三处修订 |
| terminal_bridge/main.go | 现状 6480 行，§4.1 拆分对象 |
| terminal_bridge/acli_package.go | 写入协议模板（退出码 20-23），S1 抽取对象 |
| terminal_bridge/log_collect_spc_fanout.go | fanout 泛化来源（1292 行） |
| terminal_bridge/delivery_runner.go | 传输原语抽取来源（1933 行） |
| terminal_bridge/log_collect_transfer.go | Pull/Push 两张脸分拆来源 |
| terminal_bridge/log_uploader.go | obslog 域来源（自动直传+水位补传，§3.6 增强对象） |
| terminal_bridge/lifecycle.go | stderr 重定向（bridge-stderr.log）出处，L3 包装对象 |
| frontend/customer/src/components/BridgeLogUploader.vue | 手动兜底上传入口（前端→后端，不经 bridge）；退役前置见 §3.6 |
| frontend/customer/src/stores/chat.ts | 连接后 acli 检查与 acli_sync 触发点，S4b 改总是触发 |
| backend/shared | 仓库公共模块惯例参照（models/observability/utils 结构对齐） |
| scripts/build_terminal_bridge.sh | U0 加 upx 步骤、U1 加 build tag 的落点 |
| frontend/customer/public/downloads/terminal_bridge.exe | 分发产物（16.47MB 基线） |
| deploy/helm/hci-platform/templates/terminal-bridge/deployment.yaml | 集群 OTEL endpoint 配置出处（桌面剥离依据） |