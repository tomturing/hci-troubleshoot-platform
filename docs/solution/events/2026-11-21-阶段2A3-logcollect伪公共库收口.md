---
status: active
category: solution
audience: developer
last_updated: 2026-11-21
owner: team
---

# 阶段 2 A3：logcollect 伪公共库收口

## 文档信息

- **版本**: 1.0
- **创建日期**: 2026-11-21
- **状态**: 第一批已实施；两项剩余依赖已登记在门禁注释里
- **关联代码**: `terminal_bridge/internal/logcollect`、`internal/shared/logcontract`、`internal/shared/cluster`、`internal/shared/aclibin`、`internal/shared/acliout`、`internal/shared/remote/archive.go`、`internal/shared/uplink/file.go`、`scripts/check_bridge_imports.py`、`scripts/gen_log_collect_display.py`

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
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

## 五、未修与理由（不做静默遗漏）

| 剩余边 | 性质 | 为什么本轮不动 |
|---|---|---|
| `delivery -> logcollect` | 证据凭据注册表（`EvidenceConfig`/`EvidenceFor`/`ReportLocalArchive`）+ 节点侧包清单上报 | 证据子系统与 obslog 门面（A2 刚落地）和自签证书放宽策略强耦合，近两周刚连续修复 S7 回传事故（404/500/422）。搬迁须在具备真实节点与平台的环境下单独验收，不能与纯符号搬迁混做 |
| `* -> terminal` | `FormatOutputPreview`（GB18030 解码 + 头尾保留截断） | terminal 是命令输出处理的行为宿主；下沉 shared 前必须先补跨域行为测试，否则会把"呈现策略"变成无主代码 |

两条边均已写入 `scripts/check_bridge_imports.py` 的基线注释，并受 `BASELINE_SUNSET=2026-12-31` 约束，
不会被当成"现状合法"永久冻结。

---

## 六、回滚

- 代码：单分支 `refactor/stage2-arch`，按文件级 `git revert` 即可（`logcontract`/`cluster`/`aclibin`/`acliout`
  是新增包，删除即回退）；
- 契约产物：`python3 scripts/gen_log_collect_display.py` 会同时重写 7 个产物，回退时需一并还原 `GO_OUT` 配置；
- 无数据库、无部署配置变更，不涉及线上数据。
