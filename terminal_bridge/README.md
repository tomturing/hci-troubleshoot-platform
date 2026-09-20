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

### 多工单隔离与关联

一个 bridge 可同时服务多个工单；日志为**单文件 + 字段隔离**：

- 每条日志携带 `case_id / conversation_id / exec_id / tool_call_id / trace_id / node_ip`；
- 同一工单的 `exec.request → exec.start → exec.done` 通过 `exec_id` 串联，并与后端 `trace_id` 打通；
- 云端按 `case_id` 归档检索；手动上传时未带 `case_id` 的条目继承上传时绑定的工单。

### 事件字典

| 事件 | 级别 | 说明 |
|---|---|---|
| `bridge.startup` | INFO | 启动，含版本/模式/监听地址/日志路径/日志级别 |
| `bridge.connected` | INFO | 浏览器 WebSocket 连接建立 |
| `bridge.disconnected` | WARN | 浏览器断开（断连窗口内的命令结果会丢失） |
| `ws.send_failed` | ERROR | 出站消息发送失败（结果回不去浏览器） |
| `ssh.connected` | INFO | SSH 认证成功 |
| `ssh.output` | INFO | 终端输出回采 |
| `exec.request` / `exec.start` | INFO | 收到/开始执行命令 |
| `exec.done` | INFO/ERROR | 执行完成，含 `exit_code`、`error_type`、`stderr_preview` |
| `exec.timeout` | ERROR | 命令超时（显式事件，可独立检索） |
| `exec.rejected` | ERROR | 命令被拒绝（缺 `exec_id`/`command` 等，含 `error_type`） |
| `exec.session_missing` | ERROR | SSH 会话不存在 |
| `log.dropped` | WARN | 回采队列溢出等导致的日志丢弃（限频上报） |
| `client.error` | ERROR | Custom-UI 浏览器未捕获异常（`service.name=customer-ui`） |

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
| `resume` | `case_id` | 重放近期结构化日志 |
| `ssh_disconnect` | `case_id, node_ip?` | 断开会话 |

### Bridge → 浏览器

| type | 说明 |
|---|---|
| `ssh_connected` / `ssh_disconnected` / `ssh_error` | SSH 会话生命周期 |
| `ssh_output` | 交互终端输出流 |
| `exec_stdout` / `exec_stderr` / `exec_result` | Agent 命令执行生命周期和最终结果 |
| `vm_console_result` | 虚拟机控制台固定操作元数据结果（`capture_id`、`operation`、`exit_code`、`sha256`、`size_bytes`、`upload_status`、`error_type`、`duration_ms`、`timed_out`）；只含元数据，PPM 原图经 HTTP 直传制品服务 |
| `bridge_log` | 带 `case_id`、`trace_id`、节点和事件标签的结构化回采日志 |

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

详细部署与端到端验证见 [K3s 部署指南](../docs/deploy/terminal-bridge-k3s.md)。
