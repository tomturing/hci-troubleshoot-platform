# hci-sim (P0 Spike)

`hci-sim` 是方案《Agent 并发测试与 KBD 驱动 HCI 仿真环境方案》的 P0 落地骨架：
一个**可被 SSH 登录的 HCI 仿真运行时**。它接收来自 `terminal_bridge` 的命令、解析成
canonical fingerprint、匹配 fixture，并返回 stdout/stderr/exit code。`terminal_bridge`
保持完全透明（只做 SSH 传输），不感知 KBD 领域知识。

## 在链路中的位置

```
KBD (signals_json v2)            # 未来由 Fixture Compiler 生成 fixture
  → hci-sim  (本服务, SSH:2222)  # 命令解析 + fixture 路由 + 输出
  → terminal_bridge (透明 SSH)   # 复用完整 Bridge→SSH 路径
  → Agent / CDD 真实执行链路
```

## 运行

```bash
# 本地（Go 1.20+）
cd hci-troubleshoot-platform/hci-sim
go run . --listen 0.0.0.0:2222 --fixtures ./fixtures --accept-any-password

# 或容器
docker build -t hci-sim .
docker run -p 2222:2222 -v "$PWD/fixtures:/fixtures" hci-sim
```

首次启动会生成 `./hci-sim-hostkey` 并持久化（避免重启后 known_hosts 校验失败）。

## 把 terminal_bridge 指向 hci-sim（sim-ssh 接入点）

`terminal_bridge` 无需改动执行逻辑，只要 SSH 目标地址指向 hci-sim，并通过环境变量标记
模拟目标即可让结果打上 `simulation=true`：

```bash
# terminal_bridge 进程
export HCI_BRIDGE_SIM_TARGETS="hci-sim,127.0.0.1:2222"   # 命中即标记 simulation
export HCI_BRIDGE_HOST_KEY_POLICY="insecure"              # 或 accept-new
```

前端/后端把某条 exec 请求的目标 host 设为 `hci-sim:2222`，Bridge 执行后用
`ssh_exec_process`（`session.Start(command)`）连上 hci-sim，拿到 stdout/stderr/exit；
结果 `exec_result` 里会带：

```json
{
  "exec_result": "...",
  "exit_code": 0,
  "simulation": true,
  "execution_mode": "sim-ssh",
  "simulation_backend": "hci-sim"
}
```

> 注意：Bridge 把用户名 `admin` 转成 `root`，并把密码追加 `sangfornetwork` 后缀。
> hci-sim 的 `PasswordCallback` 已兼容该后缀，P0 也可用 `--accept-any-password` 跳过校验。

## 场景隔离（P0 演示）

fixture 通过 `scenario_id` 强制隔离：当 SSH 客户端发送环境变量 `HCI_SIM_SCENARIO_ID`
时，只有绑定同一 `scenario_id` 的 fixture 才会命中，否则 fail closed。

| fixture | scenario_id | 行为 |
|---|---|---|
| `kbd27123-positive.json` | `""` (默认) | 返回含 `too many file descriptors` 的错误日志 |
| `kbd27123-negative.json` | `scenario-0002` | 返回正常日志（Agent matcher 不应命中） |
| `kbd27123-timeout.json`  | `scenario-0003` | `timeout=true`，挂起直到对端超时 |

演示：
```bash
# 默认 → positive
ssh -p 2222 root@127.0.0.1 'acli log get --keyword "too many file"'

# 指定 scenario → negative
ssh -p 2222 -o SendEnv=HCI_SIM_SCENARIO_ID root@127.0.0.1 \
  'acli log get --keyword "too many file"'   # 连接前需 export HCI_SIM_SCENARIO_ID=scenario-0002
```

## fail closed 原则

未命中任何 fixture 时，hci-sim **绝不**返回空 stdout + exit 0，而是返回
`exit_code=127` + stderr `fixture_not_found`，避免 Agent 把「模拟器缺配置」误判为「HCI 正常」。

## fixture 格式（最小子集）

见 `fixtures/*.json`。关键字段：`tool`/`acquisition_key`、`command_match`（正则匹配原始命令）、
`scenario_id`、`variant`、`exit_code`、`stdout`/`stderr`、`delay_profile_ms`、`chunk_profile_ms`、
`timeout`、`status`（仅 `published` 可路由）。

## 后续（P1）

- Fixture Compiler：从 KBD `signals_json` v2 自动生成场景与 witness
- Scenario Scheduler：scenario lease 租约 / 配额 / 100+ 逻辑隔离
- Stateful Scenario Worker：跨轮变量链（`$PID` / `$HOST`）
- mutation testing 防止自洽假通过
- 与 OTel/Prometheus 对接（目前仅结构化日志）

## 测试

```bash
go test ./...
```
