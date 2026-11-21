---
status: active
category: solution
audience: developer
last_updated: 2026-11-21
owner: team
---

# 阶段 2 A3：logcollect 伪公共库收口

## 文档信息

- **版本**: 1.1
- **创建日期**: 2026-11-21
- **状态**: 已全部实施，域间依赖边 6→0，门禁基线置空
- **关联代码**: `terminal_bridge/internal/logcollect`、`internal/shared/logcontract`、`internal/shared/cluster`、`internal/shared/aclibin`、`internal/shared/acliout`、`internal/shared/remote/archive.go`、`internal/shared/uplink/file.go`、`scripts/check_bridge_imports.py`、`scripts/gen_log_collect_display.py`

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-11-21 | v1.1 | 第二批收尾：命令输出呈现下沉 `shared/remote`、传输证据改端口，域间边 4→0；原「未修」章节改写为处置记录 | 本文件 |
| 2026-11-21 | v1.0 | 首版：记录 A3 的归位口径、验收证据与剩余依赖的判定理由 | 本文件 |

---

## 一、问题

审查文档 §4-A3 的结论是「`logcollect` 变成伪公共库」：它既承担采集编排，又被迫承担公共库职责，
于是 `acli`（30+ 处）、`delivery`（20+ 处）、`sftp`（3 处）都必须反向 import 它。
门禁 `BASELINE_DOMAIN_EDGES` 因此冻结了 6 条域间边。

第一性拆解后，"借用"其实是四类不同性质的符号被混在同一个包里：

| 性质 | 例子 | 正确归属 |
|---|---|---|
| 跨域契约（与平台 JSON 同源） | `LC-INSTALL-*`、`LC-TRANSFER-*` | 独立契约包 |
| 两域共用的事实层 | acli PATH/解析、集群节点清单 | shared 事实包 |
| 节点面/云面原语 | 摘要探测、归档存在性、URL 校验 | `remote` / `uplink` |
| 无语义小工具 | `ErrString`、`FirstNonEmpty` | **不进任何工具包**（各域私有副本） |

最后一行是本次唯一"反直觉"的决定，理由见第四节。

---

## 二、落地清单

| 原符号 | 新位置 | 说明 |
|---|---|---|
| `ErrEnv*` `ErrInstall*` `ErrExec*` `ErrPlug*` `ErrSpc*` `ErrScript*` `ErrTransfer*` `ErrPlat*`、`errorMeta`、`ValidationErrorCode`、`ExecErrorCode`、`LogCollectErrorFields` | `shared/logcontract` | 契约包只依赖标准库；`acli` 不再为拿安装类错误码而依赖采集域 |
| `contractErrors` / `ContractText` / `ContractFormat`（生成物） | `shared/logcontract/display_generated.go` | 生成器 `gen_log_collect_display.py` 的 `GO_OUT` 与 package 名同步改为 `logcontract`，`contractTitle` 导出为 `Title` |
| `SpcNodeList` `SpcLocalAddresses` `SpcFanoutConcurrency` `RunSpcOnHostStreams` `BuildSpcSSHCommand` `SpcDeployDir` | `shared/cluster` | 改名揭示真实语义：集群拓扑与跨节点扇出，不是"场景清单" |
| `BuildAcliVersionCommand` `ParseAcliBinary` + PATH/解析脚本 | `shared/aclibin` | `__ACLI_BIN__`/`__ACLI_ABSENT__` 协议与解析同源，避免"面板显示 1.0.1、采集命中旧版" |
| `ParseLogCollectProgress` + 节点态/主机态归一化 + 标签解析 | `shared/acliout` | 与 `aclibin` 成对：一个"怎么调 acli"，一个"怎么读 acli 说的话" |
| `ProbeSourceDigest` `ProbeLogCollectArchive` `NormalizeArchivePath` `ArchiveProbeToolMissingRe` | `shared/remote/archive.go` | 三态存在性（stat/ls 两级降级）原样迁移，判据顺序未改 |
| `ValidatePlatformURL` `FileDigest` | `shared/uplink/file.go` | 云面/本地文件面 |
| `ShellQuote` | 删除包装 | 调用方直连 `remote.ShellQuote`（安全原语唯一实现） |
| `ErrString` `FirstNonEmpty` `TruncateForDiag` | 各域私有副本（`acli_text.go` / `delivery_text.go` / `sftp_text.go`） | 见第四节 |

`logcollect` 对上述符号保留同名薄包装（类型别名或一行委托），采集编排与既有测试因此零改动；
`logcollect` 内原来的 17 处 `ErrString`、23 处 `FirstNonEmpty` 改为域内私有名，不再对外暴露。

---

## 三、验收证据

| 项 | 结果 |
|---|---|
| 域间边 | 6 → 4（`acli -> logcollect`、`sftp -> logcollect` 已消除） |
| `grep -r "logcollect\." internal/acli internal/sftp` | 仅剩注释文字 |
| 门禁 | `python3 scripts/check_bridge_imports.py` 通过（4 条既有依赖，无新增） |
| 契约生成物 | `gen_log_collect_display.py --check` 一致；其余 6 个产物字节未变 |
| 测试 | 19 包全量 `go test ./...` 绿；`go vet ./...` 无输出；`gofmt -l` 空 |
| 双构建 | `go build -tags telemetry ./...` 与 `GOOS=windows go build ./cmd/bridge` 均通过 |
| 回归守卫迁移 | `remote/archive_test.go`（摘要命令三条 + 解析九例）、`remote/archive_probe_test.go`（三态穷举 + ls 兜底解析）随实现搬迁，断言未放宽 |

---

## 四、为什么 `ErrString` 一类不进 shared 工具包

设计文档 §3.5 的判据是：**"删掉某函数若会让链路产生安全/正确性风险，它就不是 util，禁止进任何工具包"**。
`errString(nil)` 返回空串而不是 `"nil"` 或 panic，属于正确性；因此它既不该做工具包，
也不该由业务域互相借用（借用正是"伪公共库"的成因）。各域四行私有副本是刻意选择：

- 成本：每域 4~10 行，且这两个函数十年不会变语义；
- 收益：`acli`/`sftp` 对 `logcollect` 的依赖边直接消失，且不会因"顺手复用"再次长回来；
- 纪律：Go 谚语 "a little copying is better than a little dependency"。

---

## 五、第二批：剩余两条边（2026-11-21 收尾）

### 5.1 `* -> terminal`（acli 4 处 + logcollect 1 处）

审查文档原判断是"先补跨域行为测试再下沉"。补测时发现前置条件已具备：`acli_sync_output_test.go`
里的 GBK 解码与尾保留断言本来就是跨域行为测试。于是直接下沉：

- `NormalizeOutputText` / `FormatOutputPreview` / `truncateKeepTail` → `shared/remote/output.go`；
- `terminal` 保留同名导出包装（VM 控制台与终端回显口径不变）；
- `delivery_reconnect_test.go` 里的 `On_output_start` nil-ws 用例迁到 `terminal` 包
  —— 它守护的是终端输出泵不 panic，与交付业务无关，留在原地会让边永久存在。

### 5.2 `delivery -> logcollect`（证据回传）

交付域对 `EvidenceConfig` 的用法全程是"不透明句柄透传"：`EvidenceFor(taskID)` 取出后直接交给
`ReportLocalArchive` / `PostEvidence`，一次都没读过字段。所以正确做法不是搬结构，而是收成端口：

```go
// shared/evidence
type TransferReporter interface {
    Enabled(taskID string) bool
    ReportArchive(ctx context.Context, taskID, path string)
    ReportTransferMetric(ctx context.Context, taskID string, metric TransferMetric)
    MetricInterval() time.Duration
    StallSeconds() int
}
```

- 实现：`logcollect.NewTransferReporter()`（票据头、`tasks/{id}/metrics` 路径、TLS 放宽、失败告警全在域内）；
- 注入：装配层 `cmd/bridge/evidence_wire.go`（与 obslog Sink 同一处，A2 已确立"装配层是唯一适配器"）；
- `TransferMetric` DTO 归 `shared/evidence`（上报契约两端共用）；
- 未注入时 `nopTransfer` 兜底：`Enabled=false`，交付域不空转采样。

对抗性审查抓到的一个坑：`atomic.Value` 直接存接口会存下"装箱后的具体类型"，测试里注入 `*captureTransfer`
再 `ResetTransfer` 换成 `nopTransfer{}` 会 panic（`inconsistently typed value`）。改为统一存 `*transferHolder` 包装，
并给测试提供 `ResetTransfer()` 保证用例隔离。

### 5.3 测试重新分工

| 层 | 验证内容 | 位置 |
|---|---|---|
| 交付域 | 采样算法：EMA 平滑、零增量速率归零、ETA 不倒数、未开启时不采样 | `delivery_evidence_metrics_test.go`（端口替身） |
| 采集域 | 投递链路：票据头、任务路径、内容、无凭据不投递、节奏常量 | `log_collect_evidence_transfer_test.go`（httptest） |

原用例两头混在一起，靠 `import logcollect` 才能跑——正是那条要消除的边。

### 5.4 最终指标

| 项 | 第一批后 | 收尾后 |
|---|---|---|
| 域间依赖边 | 4 | **0**（`BASELINE_DOMAIN_EDGES = set()`） |
| 门禁语义 | 冻结 4 条 + sunset | 空集，任何新边直接失败 |

## 六、回滚

- 代码：单分支 `refactor/stage2-arch`，按文件级 `git revert` 即可（`logcontract`/`cluster`/`aclibin`/`acliout`
  是新增包，删除即回退）；
- 契约产物：`python3 scripts/gen_log_collect_display.py` 会同时重写 7 个产物，回退时需一并还原 `GO_OUT` 配置；
- 无数据库、无部署配置变更，不涉及线上数据。
