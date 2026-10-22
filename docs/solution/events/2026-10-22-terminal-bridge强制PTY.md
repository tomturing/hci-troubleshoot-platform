# terminal_bridge 强制 PTY（acli 插件命令可执行性修复）

## 一、问题

`acli` 的插件命令（`log_collect` / `asys` / `performance_tools` 等）在 acli 内部通过容器
exec 执行，**要求 stdin 是 tty**。在 terminal_bridge 的隔离执行通道（`ssh_exec_process`）
下没有分配伪终端，实测表现：

```
$ ssh root@<hci> 'acli log_collect progress'         # 无 PTY
time="..." level=fatal msg="execing command in container: input is not a terminal"
$ echo $?
0
```

两个必须正视的后果：

1. 命令必然失败，但**退出码仍为 0**，上游会把失败当成功；
2. HCI 宿主机是精简系统，实测**没有 `script` 命令**（`curl/wget/nc` 有，`python` 无），
   无法用 `script -q -c` 包一层伪终端兜底。

结论：PTY 只能由 terminal_bridge 在 SSH 层分配。

## 二、设计

### 2.1 判定：单一入口，按"被执行程序是否需要 tty"决定

```go
func shouldUsePty(command string, explicit bool) bool {
    if explicit { return true }
    trimmed := strings.TrimSpace(command)
    return trimmed == "acli" || strings.HasPrefix(trimmed, "acli ")
}
```

- **默认对 `acli` 前缀命令开启**：调用方（agent-service 的 `acli_exec`、custom-ui）无需逐个修改；
- `pty: true` 显式字段用于覆盖（非 acli 命令也可强制）；
- 其他命令保持 pipe 语义，不破坏既有 `stdout`/`stderr` 双通道。

### 2.2 终端模式：关闭回显与换行改写

```go
ssh.TerminalModes{ssh.ECHO: 0, ssh.OPOST: 0, ssh.ONLCR: 0}
```

避免输入回显污染 stdout，并从源头抑制 `\n → \r\n`。

### 2.3 输出归一化：必须处理跨读边界

即使关闭 `ONLCR`，服务端仍可能输出 CRLF，且 `\r\n` 可能被拆分到两次 `Read`。
`crlfNormalizingReader` 保留末尾 `\r` 的待定状态，跨块合并为单个 `\n`；孤立 `\r`
与 EOF 悬空 `\r` 原样保留，保证不丢字节。

归一化发生在 **capture 之前**，因此 `stdout` 内容与其 `stdout_sha256` 语义一致；
`exec_result` 侧再做一次幂等归一化兜底。

### 2.4 语义声明：PTY 下 stderr 合并

PTY 模式下服务端把 stderr 合并进同一终端流，`StderrPipe` 不再产生独立数据。
因此：

- PTY 模式**不创建** stderr 读取协程；
- `exec_result` 显式携带 `pty_mode: "pty"` 与 `stderr_pty_merged: true`；

下游不得把"stderr 为空"解释为"没有错误输出"。

### 2.5 失败不降级

`RequestPty` 失败直接返回 `error_type=pty_request_failed`，**不允许降级为无 PTY 执行**：
降级会重现"退出码 0 + 命令未执行"的静默失败，比显式失败危险得多。

## 三、协议变更（向后兼容）

| 字段 | 方向 | 缺省 | 说明 |
|---|---|---|---|
| `pty` | 入站 | `false` | 显式强制 PTY；旧客户端不传时，acli 前缀命令仍自动开启 |
| `pty_mode` | 出站 | `pipe` | `pipe` / `pty` |
| `stderr_pty_merged` | 出站 | `false` | PTY 模式下为 `true` |

`bridge_hello` 暂未增加 capabilities 字段；前端如需探测能力，可依据 `exec_result` 是否
携带 `pty_mode` 判定（后续 PR-3 再补齐握手能力声明）。

## 四、验证

```bash
cd terminal_bridge
gofmt -l .          # 无输出
go build ./...      # 通过
go vet ./...        # 通过
go test -run "Pty|CRLF|Normalize" -v ./...
```

新增单测覆盖：

| 用例 | 断言 |
|---|---|
| `TestShouldUsePty` | acli 前缀/裸命令/前导空白/换行 → true；`aclix`/空命令 → false；显式 true 覆盖 |
| `TestCRLFNormalizingReader` | 单块、跨块、孤立 CR、EOF 悬空 CR、多段跨块 |
| `TestNormalizePtyOutputIdempotent` | 二次归一化结果不变 |
| `TestPtySemanticsSerialization` | pty 模式输出 `stderr_pty_merged`；pipe 模式不输出 |
| `TestInMessagePtyField` | 缺省 `pty=false`，保持旧客户端行为 |

已知环境限制：`TestFindPortHoldersDoesNotPanic` 在本机失败（缺 `lsof`），main 基线同样失败，
与本变更无关。

## 五、风险与回滚

| 风险 | 缓解 |
|---|---|
| PTY 导致 stderr 合并，依赖 stderr 判错的逻辑 | 显式 `stderr_pty_merged` 标记；错误分类同时参考 stdout |
| 输出体积因回显/宽字符增加 | 关闭 ECHO；输出仍受 `max_output_bytes` 与 `output_filters` 约束 |
| 旧 bridge 不支持 `pty` 字段 | 字段缺省 false；新前端对 acli 命令本就由 bridge 自动判定，不依赖前端传参 |
| 个别设备 sshd 忽略 terminal modes | CRLF 归一化在读取层兜底 |
| 回滚 | 移除 `shouldUsePty` 的自动分支即可恢复 pipe 语义，协议字段可保留 |

## 六、后续（PR-3）

1. 日志收集功能接入：`acli log_collect create/cancel/progress` 全部经此通道，退出码与输出需双判定；
2. `bridge_hello` 增加 `capabilities`，前端可提示升级旧 bridge；
3. 现场端到端验证：无 PTY 失败 → 有 PTY 成功，并核对 `acli log_collect progress` 解析。
