---
status: active
category: solution
audience: developer
last_updated: 2026-11-19
owner: team
---

# terminal_bridge 端口不可绑定被误判为致命错误导致双击闪退修复

## 文档信息

- **版本**: 1.0
- **创建日期**: 2026-11-19
- **状态**: 已实施，待现场验收
- **关联代码**: `terminal_bridge/main.go`、`terminal_bridge/port_conflict_windows.go`、`terminal_bridge/port_conflict_other.go`

---

## 变更历史

| 日期 | 版本 | 变更内容 | 关联事件文档 |
|------|------|---------|------------|
| 2026-11-19 | v1.0 | 首版：记录 WSAEACCES(10013) 被误判为不可恢复错误的物理事实、errno 优先判定修复与验收标准 | 本文件 |

---

## 一、问题与可验证的物理事实

客户 Windows 桌面双击 `terminal_bridge.exe`（`v2.36.0-68-gd9ab7f1e`）后窗口一闪即退。本机日志给出的事实链：

| 事实 | 证据 |
|------|------|
| 进程已经走完初始化：交付执行器启动、`bridge.startup` 记录 `mode=desktop`、`listen 127.0.0.1:9999` | `terminal_bridge.log` 的 `delivery.runner_started` / `bridge.startup` |
| 随后立即出现 `bridge.port_conflict`，错误文案为 `bind: An attempt was made to access a socket in a way forbidden by its access permissions.` | 同一日志文件 `level=ERROR` 行 |
| 紧接着 `bridge.shutdown` 落盘，进程退出（有 startup 有 shutdown，排除被强杀） | 同一日志文件 |
| 该地址确实已被占用：`netstat -ano \| findstr "9999"` → `TCP 127.0.0.1:9999 LISTENING 6376` | 现场 PowerShell 输出 |

**结论**：不是崩溃、不是缺依赖，而是"端口绑不上 → `log.Fatalf` → 进程退出"，用户感知为"闪退"。

## 二、根因：判定依据用了错误的信号源

`acquireListener` 的自愈链路（存活 Bridge 幂等退出 → 备用端口 47324 回退 → fail-fast）只有一个入口判据：

```text
isAddressInUse(err) → 文案包含 "address already in use" / "only one usage of each socket address"
```

该判据有两个物理缺陷：

1. **信号源选错**：Windows 的 bind 文案来自 `FormatMessage`，随系统语言变化。中文 Windows 上文案是
   「通常每个套接字地址(协议/网络地址/端口)只允许使用一次。」，英文子串恒不命中——自愈在中文现场等于死代码。
2. **errno 覆盖不全**：本次是 `WSAEACCES(10013)`，文案里没有 "address already in use"。
   它的语义同样是"这个地址绑不上"（占用者 `SO_EXCLUSIVEADDRUSE` 独占绑定、端口被 http.sys 注册、
   或落在 Hyper-V/WinNAT 动态保留段），但被归入"不可恢复"，`acquireListener` 在第 292 行直接返回错误，
   三条自愈路径全部跳过。

即：**端口自愈功能（#1120）在 10013 与中文 Windows 两种最常见的现场形态下都不生效**。

## 三、修复

| 层级 | 改动 | 位置 |
|------|------|------|
| 判定 | 新增 `isPortBindUnavailableErrno`，以 socket errno 为准；文案仅兜底 | `terminal_bridge/port_conflict_windows.go`（10048/10013）、`terminal_bridge/port_conflict_other.go`（`EADDRINUSE`） |
| 入口 | `isAddressInUse` 先走 errno，再退回文案（并补上 10013 的英文文案） | `terminal_bridge/main.go` |
| 可观测性 | `bridge.port_conflict` 事件补 `backup_port`；fail-fast 时**无条件**打印三条处置指引（netstat / excludedportrange / `--port 47324`） | `terminal_bridge/main.go` |
| 回归 | 新增 errno 用例（Unix/Windows 各一）与 10013 文案用例；钉住"中文文案不靠文本匹配"这一事实 | `terminal_bridge/port_recovery_test.go`、`port_conflict_other_test.go`、`port_conflict_windows_test.go` |

修复后的行为：9999 绑不上（10048/10013）→ 先 `GET /health/live` 判定是否已有存活 Bridge（是则 exit 0 幂等退出）→ 否则回退 47324（前端探测契约已含该端口）→ 主备都绑不上才 fail-fast，且必须带上可执行的处置指引。

## 四、验收标准

| 编号 | 场景 | 期望 |
|------|------|------|
| V1 | 9999 空闲 | 正常监听 9999，日志有 `bridge.listening` |
| V2 | 9999 被陌生进程占用（10048 或 10013） | 回退 47324，日志有 `bridge.backup_port_activated`，浏览器能连上 |
| V3 | 9999 上是另一个存活 Bridge | `bridge.already_running` + exit 0，不报错 |
| V4 | 9999 与 47324 均不可用 | fail-fast，exit≠0，日志含占用地址、`holder_pids`、三条指引 |
| V5 | 中文 Windows | V2 仍成立（errno 判定，不依赖文案语言） |

本地已验证：`gofmt` / `go vet` / `go test ./...`（Linux）全通过；`GOOS=windows GOARCH=amd64 go build|go test -c` 交叉编译通过。
Windows 侧 errno 分支需在客户机/Windows 环境实际执行 V2/V5。

## 五、失效边界（对抗性审查）

- 修复只解决"判定"，不解决"为什么 9999 被占"。占用者是业务进程时仍需要现场决定换端口或释放端口。
- 47324 若同时落在 WinNAT 保留段，回退同样会失败并 fail-fast——此时只能用 `--port <其他>` 或 `netsh int ipv4 set dynamicport tcp` 收窄保留段（需管理员）。
- 桌面 exe 不会自动更新：合并 main 后由 `Build terminal_bridge` 工作流重建并回写下载目录，**客户机仍需人工重装 exe** 才会带上本修复。
- 中文 locale 的文案匹配仍不生效，这是刻意保留的边界：本地化文案靠 errno 覆盖，不再往匹配表里堆语言。
