# terminal_bridge（Go）

HCI 排障助手 SSH Bridge。Windows 桌面客户端和 Linux/K3s 服务使用同一套 Go 源码、同一套 WebSocket 协议与 SSH 执行逻辑。

## 两种运行形态

| 模式 | 默认监听 | 使用场景 | Origin 策略 |
|---|---|---|---|
| `desktop` | `127.0.0.1:9999` | Windows 10/11 客户端 | `*`，保持现有 localhost 兼容性 |
| `cluster` | `0.0.0.0:9999` | WSL Ubuntu 本地 K3s 端到端联调 | `same-origin`，避免暴露为通用内网 SSH 跳板 |

架构：

```text
Windows 客户端：Custom UI → ws://localhost:9999 → terminal_bridge.exe → SSH → HCI
K3s 调试模式：Custom UI → /terminal-bridge → terminal_bridge Pod → SSH → HCI
```

生产/云端 Helm 默认关闭 Pod 模式，仍由客户 Windows 本地 Bridge 发起 SSH 流量。只有确认 K3s Pod 网络能直连目标 HCI 后台的 dev/local 环境才应启用集群模式。

## 端口冲突自愈（仅桌面模式）

客户 Windows 桌面历史上因 `9999` 被占用而“双击打不开”（`ListenAndServe` 直接 `log.Fatal` 崩溃）。
现在启动时对默认端口做**分类处置**，仅当端口为默认 `9999` 且未显式指定 `--port`/`HCI_BRIDGE_PORT`
时启用（`desktop` 模式）：

```text
bind(9999)
├─ 成功 → 正常提供服务
└─ 被占用(EADDRINUSE)：
    ├─ 9999 上是「存活 Bridge」(GET /health/live 返回 status:ok)
    │     └─ 是 → 幂等退出(exit 0)：本机已有 Bridge 在跑，无需重复启动（双击/没退净场景）
    └─ 否（陌生进程占用）→ bind(备用 47324)
          ├─ 成功 → 回退提供服务，记录 bridge.backup_port_activated
          └─ 失败 → fail-fast：打印占用地址 + best-effort 占用 PID + 处置指引，exit≠0
```

- **备用端口 `47324`**：位于注册端口区（1024–49151）、IANA 未分配、低于 Windows 出站动态端口下界
  `49152`（不会被 OS 临时征用），数字无规律、避开常见已知端口。Go 端与前端探测共用这一对常量。
- **浏览器侧对齐**：customer-ui 桌面模式连接前先探测 `9999 → 47324` 两个**已知端口**，以收到
  Bridge 的 `bridge_hello` 握手回包为准判定“对端确实是 Bridge”（不能只看 WS 能否 open，否则会误连
  占用主端口的陌生进程），命中即缓存端口。因此**无需服务端端口发现通道**。
- **不改动范围**：cluster 同源 `/terminal-bridge`、docker/admin 的 `host.docker.internal:9999`、
  Helm `terminalBridge.port=9999` 等部署期固定值不变；显式自定义端口冲突直接 fail-fast，不扩散回退。

### Windows 现场处置指引

排查端口占用（PowerShell / CMD）：

```bat
:: 1) 查看系统动态端口区间（确认 47324 不在其中，默认下界应为 49152）
netsh int ipv4 show dynamicport tcp

:: 2) 定位占用主/备端口的进程 PID（最后一列为 PID）
netstat -ano | findstr :9999
netstat -ano | findstr :47324

:: 3) 查看被系统/Hyper-V 保留的端口段，确认备用端口可用
netsh int ipv4 show excludedportrange protocol=tcp

:: 4) 按 PID 结束遗留的 terminal_bridge 进程（确认为本应用后再操作）
taskkill /PID <PID> /F
```

fail-fast 退出时，日志与 `bridge.port_conflict` 事件会给出占用地址与尽力探测到的占用 PID，
并提示上述 `netstat`/`lsof` 命令；Linux/WSL 下对应 `lsof -nP -iTCP:9999 -sTCP:LISTEN`。

## hci-sim C3 仿真租约

Custom UI 的“仿真租约”认证会发送 `auth_type=lease`、`execution_mode=sim-ssh`、`password=htp2.*` 和可选 `test_run_id`。Bridge 只透传 Lease，不解析或记录 token；只有完整上下文才会被标记为 simulation。开发验收由 `scripts/hci-sim/two-step-acceptance.sh <KBD_ID>` 生成连接字段，不能将普通密码或主机名当作仿真认证。

## Windows 构建

安装 Go 1.26+，然后执行：

```bat
cd terminal_bridge
build_windows.bat
```

也可在 WSL/Linux 中交叉编译并复制到 customer-ui 下载目录：

```bash
bash scripts/build_terminal_bridge.sh
```

产物为 `frontend/customer/public/downloads/terminal_bridge.exe`，无额外运行时依赖。

## Linux/K3s 构建与部署

构建单个容器镜像：

```bash
docker build --network host \
  -t hci-terminal-bridge:dev \
  -f terminal_bridge/Dockerfile .
```

使用项目 K3s 构建脚本，并通过当前双仓部署入口发布：

```bash
IMAGE_TAG=latest BUILD_ONLY_IMAGES=hci-terminal-bridge,hci-customer-ui \
  bash scripts/ops/k3s-build.sh

bash scripts/ops/k3s-deploy-dualrepo.sh --env dev
```

Helm 本地覆盖配置：

```yaml
terminalBridge:
  enabled: true
  allowedOrigins: "same-origin"
  networkPolicy:
    enabled: true
    ingressNamespace: kube-system
    hciSimNamespace: hci-sim-dev
    hciSimSshPort: 2222
```

该配置应写入环境仓库 `environments/dev/values.yaml`，或本地双仓覆盖文件 `.local/values-dualrepo-local.yaml`。

启用后 Helm 会：

- 部署单副本 `terminal-bridge` Deployment 和 ClusterIP Service；
- 为 Pod 添加 startup/liveness/readiness 探针；
- 添加 Prometheus Pod 抓取注解；
- 创建 `/terminal-bridge` 同源 Ingress 路由；
- 向 customer-ui 注入运行时 Bridge URL，无需为 URL 变化重新构建前端；
- 以 NetworkPolicy 限制入口为 Traefik，出口为 DNS、hci-sim SSH 和 Tempo；
- 将临时回放日志写入受大小限制的 `emptyDir`。

当前必须保持 `replicaCount: 1`。SSH 会话和 WebSocket 连接保存在进程内存中，多副本需要会话亲和或外部会话状态后才能支持。

## 运行参数

命令行参数优先于环境变量：

| 命令行参数 | 环境变量 | 说明 |
|---|---|---|
| `--mode` | `HCI_BRIDGE_MODE` | `desktop` 或 `cluster` |
| `--listen-address` | `HCI_BRIDGE_LISTEN_ADDRESS` | 监听 IP |
| `--port` | `HCI_BRIDGE_PORT` | 监听端口，默认 `9999` |
| `--allowed-origins` | `HCI_BRIDGE_ALLOWED_ORIGINS` | 逗号分隔 Origin；支持 `*`、`same-origin` |
| — | `HCI_BRIDGE_LOG_DIR` | 本地日志目录，**优先级最高**；设置后覆盖桌面默认目录 |
| — | `HCI_BRIDGE_LOG_MAX_BYTES` | 单个日志文件最大字节数，默认 64 MiB，超限轮转为 `*.log.1` |
| — | `HCI_BRIDGE_LOG_TO_DESKTOP` | desktop 模式桌面落盘开关，设为 `false`/`0`/`no` 关闭，默认开启 |
| — | `HCI_BRIDGE_LOG_LEVEL` | 日志级别阈值 `DEBUG`/`INFO`/`WARN`/`ERROR`，默认 `INFO` |
| — | `HCI_BRIDGE_LOG_UPLOAD_INTERVAL_SECONDS` | 直传批量上报间隔，默认 `2` |
| — | `HCI_BRIDGE_LOG_UPLOAD_BATCH` | 单批条数，默认 `100`（上限 1000） |
| — | `HCI_BRIDGE_LOG_OUTBOX_MAX_BYTES` | 直传 outbox 字节上限，默认 8MB，超限丢最旧 |
| — | `HCI_BRIDGE_DELIVERY_CONCURRENCY` | 任务内分片并发（1–4），默认按可用内存/CPU 自动决策 |
| — | `HCI_BRIDGE_DELIVERY_TASKS` | 跨任务并发（1–2），默认 1；高配机才允许 2 |
| — | `HCI_BRIDGE_SSH_KEEPALIVE_SECONDS` | SSH 应用层保活探测间隔（秒），默认 30；设为 `0` 关闭探测，仅保留 TCP keepalive |
| — | `HCI_BRIDGE_SFTP_DIR` | 文件下载的**本机落地目录**，优先级最高；未设置时按 `$HOME/Downloads/hci-sftp` → `/var/lib/terminal-bridge/downloads` → `$TMPDIR/hci-sftp` 探测可写项 |
| — | `HCI_BRIDGE_SFTP_MAX_BYTES` | 单文件下载上限（字节），默认 5 GiB；超过直接报 `too_large`，不静默截断 |
| — | `PLATFORM_ARTIFACT_URL` | 虚拟机控制台截图 PPM 直传的制品服务基址（conversation-service）；**未配置时 `capture_baseline` fail-closed 返回 `artifact_upload_disabled`，不降级 base64 over WS** |
| — | `PLATFORM_INTERNAL_API_TOKEN` | PPM 直传的内部 Bearer Token（`INTERNAL_API_TOKEN` 对端校验） |

示例：

```bash
./terminal_bridge \
  --mode=cluster \
  --listen-address=0.0.0.0 \
  --port=9999 \
  --allowed-origins=same-origin
```

## SSH 连接保活

SSH 会话是 Bridge 里唯一有生命周期的外部资源：`sessions` 只是内存 map，键为 `caseID@nodeIP`，
进程重启或连接断开即全部消失。终端空闲时这条连接上没有任何字节流动，而客户环境普遍存在
防火墙/NAT/负载均衡的空闲映射老化，链路会被**静默丢弃**（无 FIN/RST）。

因此 Bridge 做两层保活：

| 层次 | 机制 | 失败判据 |
|---|---|---|
| TCP | `SetKeepAlive(true)` + 周期 30s | 由内核判定，Bridge 不感知 |
| SSH | 每 `HCI_BRIDGE_SSH_KEEPALIVE_SECONDS`（默认 30s）发一次 `keepalive@openssh.com` 全局请求 | 返回传输层错误，或 10s 内无响应 |

判定失效后不会静默丢弃：先记 `ssh.keepalive_failed`，再 `close()` 会话。关闭会让 pty 读循环
立即 EOF，走既有的 `onExit` 清理链路（从 `sessions` 移除并向前端发 `ssh_disconnected`），
不引入第二条清理路径。

> 注意：`keepalive@openssh.com` 不被服务端支持时回 `REQUEST_FAILURE`（`wantReply` 为 false、
> `err` 为 `nil`），这是**链路活着**的证据，不算失败。

会话消失的排查顺序（日志事件）：`bridge.disconnected`（浏览器断开连坐）→
`ssh_disconnected`（远端 shell 退出）→ 重复的 `ssh.connected` 同键（重连覆盖）→ Pod 重启。
`log_collect.session_missing` 事件现在带 `active_sessions`、`session_keys`、`requested_key`：
**会话数为 0 说明连接生命周期已结束；会话数 >0 但键对不上说明是路由/键问题**，两者处置完全不同。

## 健康、状态与指标

| 端点 | 用途 |
|---|---|
| `/health/live` | 进程存活探针 |
| `/health/ready` | HTTP/WebSocket 服务就绪探针 |
| `/status` | 版本、模式、活跃 WebSocket/SSH 会话、日志缓冲等状态 |
| `/metrics` | Prometheus 指标 |

Ingress 保留 `/terminal-bridge` 前缀，因此浏览器侧对应端点为 `/terminal-bridge/status`、`/terminal-bridge/metrics` 等。

readiness 只表示 Bridge 服务可接受请求，不代表任意 HCI 目标一定可 SSH 连通；目标连通性由每次 `ssh_connect` 的结果和 `bridge_ssh_connection_errors_total` 体现。

## 本地日志（排障证据源）

自动回采链路为 `bridge → WebSocket → 浏览器 → 后端`，**强依赖浏览器在线**；断网 / 页面关闭 /
回采失败时桥侧证据会整体丢失，诊断失败将无法定因（工单 Q2026092010235）。因此 desktop 模式
默认在**用户桌面**落盘全量 JSONL 日志，作为兜底证据源。

### 落盘位置

| 场景 | 目录 | 来源标识 |
|---|---|---|
| 显式指定 | `$HCI_BRIDGE_LOG_DIR` | `env:HCI_BRIDGE_LOG_DIR` |
| desktop 默认 | `<桌面>/HCI-TerminalBridge-Logs/`（兼容中文系统 `桌面`） | `desktop` |
| 桌面不可用 | `%LOCALAPPDATA%/HCI/TerminalBridge/Logs` 或可执行文件同级 `logs/` | `fallback:appdata` |
| cluster 模式 | 由 Helm 注入 `HCI_BRIDGE_LOG_DIR`（Pod 内） | `env:HCI_BRIDGE_LOG_DIR` |

最终路径在启动事件 `bridge.startup` 与 `/status` 的 `log_path`、`log_dir_source` 中自证，
排障时无需猜测"日志到底在哪"。

### 文件名与轮转

- 文件名：`bridge-<YYYYMMDD>-<实例ID前8位>.log`（一个进程一个文件，重启后新建）。
- 轮转：超过 `HCI_BRIDGE_LOG_MAX_BYTES` 后重命名为 `*.log.1` 并新建空文件。
- 重启续接：启动时回放最近一次运行的日志文件到内存缓冲，供晚连接的浏览器补收。
- **stderr 独立落盘**：`bridge-stderr.log`（同目录，0600）。panic / runtime fatal 只写 stderr，
  而 `log.SetOutput` 只接管标准库 log 包；不落盘的话，控制台窗口一关崩溃栈就永久丢失。

### 多工单隔离与关联

一个 bridge 可同时服务多个工单；日志为**单文件 + 字段隔离**：

- 每条日志携带 `case_id / conversation_id / exec_id / tool_call_id / trace_id / node_ip`；
- 同一工单的 `exec.request → exec.start → exec.done` 通过 `exec_id` 串联，并与后端 `trace_id` 打通；
- 云端按 `case_id` 归档检索；手动上传时未带 `case_id` 的条目继承上传时绑定的工单。

### 事件字典

| 事件 | 级别 | 说明 |
|---|---|---|
| `bridge.startup` | INFO | 启动，含版本/模式/监听地址/日志路径/日志级别 |
| `bridge.listening` | INFO | 监听就绪，`listen`/`port` 自证最终生效端口（可能已是备用端口） |
| `bridge.already_running` | INFO | 检测到本机已有存活 Bridge，本次幂等退出（重复启动/未退净） |
| `bridge.backup_port_activated` | WARN | 主端口被陌生进程占用，已回退至备用端口 47324 |
| `bridge.port_conflict` | ERROR | 主备端口均不可用，fail-fast 退出，含占用地址与 best-effort 占用 PID |
| `bridge.shutdown` | WARN | 收到退出信号（Ctrl+C/SIGTERM），含 `signal`/`uptime_seconds`/`active_sessions`；**有 startup 无 shutdown = 被强杀** |
| `bridge.panic` | ERROR | 未捕获 panic，含 `panic`/`stack`（完整栈见 `bridge-stderr.log`） |
| `bridge.connected` | INFO | 浏览器 WebSocket 连接建立，含 `conn_id` |
| `bridge.disconnected` | WARN | 浏览器断开（断连窗口内的命令结果会丢失）；含 `conn_id`/`role`/`duration_seconds`/`owned_keys` |
| `conn.role` | INFO | 连接用途确定：`terminal`（终端族）或 `panel`（日志收集族）；探测连接停在 `unknown` |
| `ws.message` | DEBUG | 收到消息（默认 INFO 级别不落盘；ping/pong 不记录） |
| `ws.send_failed` | ERROR | 出站消息发送失败（结果回不去浏览器） |
| `ssh.connected` | INFO | SSH 认证成功，含 `conn_id`/`role` |
| `session.created` | INFO | 会话建立：`key`/`conn_id`/`role`/`source(ssh_connect\|auto_connect)` |
| `session.destroyed` | WARN | 会话销毁：`reason(ws_closed\|reconnect\|remote_eof\|client_disconnect)`/`by_conn`/`owner_conn`/`age_seconds`/`active_sessions` |
| `session.destroy_miss` | WARN | 销毁落空（键已不存在），用于识别重复清理链路 |
| `ssh.disconnected` | WARN | SSH 连接结束：`reason(remote_eof\|local_close)`/`exit_error`/`duration_seconds` |
| `ssh.keepalive_failed` | WARN | SSH 保活探测失败，会话判定失效并主动关闭（含 `address`/`error`） |
| `ssh.output` | INFO | 终端输出回采 |
| `resource.sample` | INFO | 5 分钟一次的 `goroutines`/`heap_alloc_mb`/`sys_mb`/`num_gc` 采样 |
| `exec.request` / `exec.start` | INFO | 收到/开始执行命令 |
| `exec.done` | INFO/ERROR | 执行完成，含 `exit_code`、`error_type`、`stderr_preview` |
| `exec.timeout` | ERROR | 命令超时（显式事件，可独立检索） |
| `exec.rejected` | ERROR | 命令被拒绝（缺 `exec_id`/`command` 等，含 `error_type`） |
| `exec.session_missing` | ERROR | SSH 会话不存在 |
| `log_collect.session_missing` | ERROR | 日志收集动作找不到 SSH 会话；含 `requested_key`、`active_sessions`、`session_keys` 用于定性 |
| `sftp.list` / `sftp.list_failed` | INFO/ERROR | 远端目录读取成功/失败（含 `path`、`entries`、`download_dir`） |
| `sftp.download_start` / `sftp.download_done` | INFO | 开始/完成下载：含 `remote_path`、`local_path`、`size`、`resume_from`、`transport`、`sha256` |
| `sftp.download_failed` / `sftp.download_cancelled` | ERROR/INFO | 下载失败（`error_type`）/ 用户取消（保留 `.part` 供续传） |
| `sftp.session_missing` / `sftp.unavailable` | ERROR | 会话不存在 / 远端未启用 SFTP subsystem（此时改用 `sftp.exec_fallback` 下载） |
| `sftp.tasks_cancelled_on_disconnect` | INFO | WebSocket 断开时回收本连接发起的下载任务数 |
| `log.dropped` | WARN | 回采队列溢出等导致的日志丢弃（限频上报） |
| `log_upload.configured` | INFO | 已收到直传凭据，直传通道启用（含 `endpoint`/`instance`） |
| `log_upload.token_received` | INFO | 收到前端下发的直传令牌（只记长度，不记内容） |
| `log_upload.failed` | WARN | 直传失败并退避重试（含 `backoff_seconds`/`queued`） |
| `log_upload.recovered` | INFO | 进程重启后从 outbox 恢复未发送日志 |
| `delivery.runner_started` | INFO | 交付执行器启动，含并发决策（`parts`/`tasks`/`reason`/`cpu`/`mem_mb`） |
| `delivery.submitted` | INFO | 交付作业已提交（`job_id`/`task_id`/`source_path`） |
| `delivery.recovered` | INFO | 进程重启后恢复未完成作业（含阶段与已拉/已传量） |
| `delivery.collect_finished` | INFO | 节点采集完成，开始交付（`total`/`archive_path`） |
| `delivery.job_finished` | INFO | 作业状态更新（`phase`/`attempt`/`needs_reauth`/进度） |
| `client.error` | ERROR | Custom-UI 浏览器未捕获异常（`service.name=customer-ui`） |

### 自动直传（不依赖浏览器）

自动回采 `bridge → WS → 浏览器 → 后端` 强依赖浏览器在线：刷新、断网、休眠都会让断连窗口内的
日志永久丢失。直传通道把载体换成 bridge 自己：

1. 前端向平台申请**短期令牌**（12h、绑定工单与桥实例），经 WS `bridge_log_token` 下发；
2. bridge 先落盘 `bridge-log-outbox/<实例>.jsonl`，再由**唯一 worker 串行**批量
   `POST /api/bridge-logs/direct`（gzip、100 条/批、2s 一次）；
3. 失败整批保留并指数退避（1s→60s），重启后从 outbox 恢复；
4. 无令牌时是彻底的空操作——旧平台/旧前端保持原链路。

直传状态可在 `/status` 的 `log_upload` 字段看到（`enabled`/`queued`/`dropped`/`outbox`）。

### 上传本地日志

自动回采缺失时，可在 Custom-UI 点击「上传本地日志」，选择桌面
`HCI-TerminalBridge-Logs/bridge-*.log` 上传补采；云端按 `event_id` 去重，重复上传不产生重复数据。
该入口受后端开关 `bridgeLogs.uploadEnabled` 控制，自动回采稳定后可整体关闭。

## WebSocket 消息协议

### 浏览器 → Bridge

| type | 主要字段 | 说明 |
|---|---|---|
| `ssh_connect` | `host, username, port, auth_type, password?, private_key?, case_id` | 发起连接 |
| `ssh_input` | `data, case_id` | 键盘输入 |
| `ssh_inject_command` | `command, case_id` | 注入命令但不回车 |
| `ssh_exec_command` | `command, exec_id, case_id, trace_id?` | PTY marker 通道执行 |
| `ssh_exec_process` | `command, exec_id, case_id, node_ip?, container?, trace_id?` | 独立 SSH Session 执行 |
| `vm_console_op` | `operation, capture_id, host_node_id, vm_id, exec_id, case_id, node_ip, timeout?, trace_id?` | 虚拟机控制台截图固定操作（无自由文本 command）；`operation` 仅 `capture_baseline` / `wake_down_key`，入口严格校验，非法输入 fail-closed |
| `sftp_op` | `sftp_action(list\|download\|cancel), sftp_path?, exec_id?, case_id, node_ip?, trace_id?` | 文件下载：`list` 时空 `sftp_path` 表示取登录用户 home；`download`/`cancel` 用 `exec_id` 作为任务 ID。**刻意没有本地落点字段** |
| `resume` | `case_id` | 重放近期结构化日志 |
| `ssh_disconnect` | `case_id, node_ip?` | 断开会话 |
| `bridge_log_token` | `case_id, token, endpoint` | 下发日志直传凭据（短期、绑定工单与实例）；bridge 回 `bridge_log_token_ack` |
| `delivery_submit` | `case_id, node_ip, exec_id(task_id), source_path, platform_url, upload_token, chunk_size, part_count, total_size, uploaded_parts?` | 提交交付作业，**立即返回 job_id**（执行权在 bridge） |
| `delivery_status` | `job_id` | 查询作业快照（phase/progress/pulled_bytes/uploaded_parts/needs_reauth） |
| `delivery_resume` | `job_id` | 重连后唤醒 `needs_reauth` 的作业 |
| `delivery_cancel` | `job_id` | 取消作业（不碰节点上的 acli 任务） |

### Bridge → 浏览器

| type | 说明 |
|---|---|
| `bridge_hello` | 连接握手首包：携带版本，供浏览器端口冲突探测确认对端确为 terminal_bridge（旧客户端忽略未知类型） |
| `ssh_connected` / `ssh_disconnected` / `ssh_error` | SSH 会话生命周期 |
| `ssh_output` | 交互终端输出流 |
| `exec_stdout` / `exec_stderr` / `exec_result` | Agent 命令执行生命周期和最终结果 |
| `vm_console_result` | 虚拟机控制台固定操作元数据结果（`capture_id`、`operation`、`exit_code`、`sha256`、`size_bytes`、`upload_status`、`error_type`、`duration_ms`、`timed_out`）；只含元数据，PPM 原图经 HTTP 直传制品服务 |
| `bridge_log` | 带 `case_id`、`trace_id`、节点和事件标签的结构化回采日志 |
| `sftp_list_result` | 目录列表：`sftp_data{path, parent, download_dir, entries[], truncated}` |
| `sftp_transfer_progress` | 下载进度：`sftp_data{task_id, phase(preparing\|downloading\|done), bytes, total_bytes, percent, transport, status, local_path, sha256, download_dir}`；只含元数据，文件字节不经 WebSocket |
| `sftp_result` | 动作终态：`error_type` + `message` + 可选 `sftp_data`（成功时 `status=ok`，已存在同大小文件时 `exists`，取消时 `cancelled`） |

### 文件下载通道（`sftp_op`）

设计依据 [SSH 终端 SFTP 文件下载方案](../docs/solution/custom-ui/events/2026-10-27-SSH终端SFTP文件下载方案.md)：

- 文件从远端主机**直接**落到本进程所在机器（桌面模式=工程师本机，集群模式=Pod 持久卷），不经浏览器、不经平台存储。
- 复用已建立的 SSH 连接开 SFTP subsystem（会话内懒加载缓存），不做二次认证；下载在独立 goroutine 中执行，不阻塞交互式 shell。
- **本地落点由 Bridge 决定**：桌面模式 Origin 可为 `*`，接受前端传入的本地绝对路径等于把“任意文件写入”开放给所有网页。
- 文件名一律清洗（路径穿越、控制字符、Windows 保留名 CON/NUL/COM1、结尾点空格、超长截断保留扩展名）；同名文件不覆盖（同大小幂等跳过，不同大小加 `-2`/`-3` 后缀）。
- 一致性：写 `<name>.part` → 校验大小 → 原子 rename；续传时对已有片段重新摘要，最终回 `sha256`。
- SFTP subsystem 不可用时降级为 exec 通道 `tail -c +N`（此时目录浏览不可用，只能用路径直下）。
- 治理：全局并发 2、单文件上限 5 GiB、进度节流 200 ms、列表上限 2000 项、WebSocket 断开即回收任务。

### 虚拟机控制台截图通道（`vm_console_op`）

设计依据 `docs/solution/agent/虚拟机控制台视觉生产者信号设计与需求.md` §3.2/§3.3/§5.2/§6.1：

- 只执行代码常量表构造的固定操作：基线截图 `screendump`、固定唤醒 `sendkey down`、
  `test -f` 有界轮询探测（≤10 次、间隔 1s）、`base64 -w0` 读取（有界捕获 16MiB，Go 本地解码并计算 SHA-256）、无条件 `rm -f` 清理（失败附加 `cleanup_failed` 标记，不阻断结果）。
- 执行走独立 SSH 连接（与 `ssh_exec_process` 同款隔离通道），不经过 PTY marker 通道；
  宿主机直连，不做容器包装。
- `capture_baseline` 成功后原始 PPM 经 `POST {PLATFORM_ARTIFACT_URL}/internal/vm-console/artifacts/{capture_id}?kind=ppm&case_id=...&mode=online`
  直传（Bearer `PLATFORM_INTERNAL_API_TOKEN`、`X-Capture-Sha256` 完整性头，独立超时 60s），WS 只回元数据。
- 未配置 `PLATFORM_ARTIFACT_URL` → fail-closed `artifact_upload_disabled`；上传失败 → `upload_failed`（保留 sha256/size 元数据）。
- 校验拒绝：未知 `operation` → `operation_invalid`；`host_node_id`（`^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$`）、
  `vm_id`（纯数字 ≤20 位）、`capture_id`（UUID）任一非法或缺失关键字段 → `target_invalid`，均不执行任何命令。

详细部署与端到端验证见 [K3s 部署指南](../docs/deploy/terminal_bridge-K3s双运行形态部署.md)。
