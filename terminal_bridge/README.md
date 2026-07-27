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
```

该配置应写入环境仓库 `environments/dev/values.yaml`，或本地双仓覆盖文件 `.local/values-dualrepo-local.yaml`。

启用后 Helm 会：

- 部署单副本 `terminal-bridge` Deployment 和 ClusterIP Service；
- 为 Pod 添加 startup/liveness/readiness 探针；
- 添加 Prometheus Pod 抓取注解；
- 创建 `/terminal-bridge` 同源 Ingress 路由；
- 向 customer-ui 注入运行时 Bridge URL，无需为 URL 变化重新构建前端；
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
| — | `HCI_BRIDGE_LOG_DIR` | 本地回放日志目录 |
| — | `HCI_BRIDGE_LOG_MAX_BYTES` | 单个 `bridge.log` 最大字节数，默认 64 MiB |

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

## WebSocket 消息协议

### 浏览器 → Bridge

| type | 主要字段 | 说明 |
|---|---|---|
| `ssh_connect` | `host, username, port, auth_type, password?, private_key?, case_id` | 发起连接 |
| `ssh_input` | `data, case_id` | 键盘输入 |
| `ssh_inject_command` | `command, case_id` | 注入命令但不回车 |
| `ssh_exec_command` | `command, exec_id, case_id, trace_id?` | PTY marker 通道执行 |
| `ssh_exec_process` | `command, exec_id, case_id, node_ip?, container?, trace_id?` | 独立 SSH Session 执行 |
| `resume` | `case_id` | 重放近期结构化日志 |
| `ssh_disconnect` | `case_id, node_ip?` | 断开会话 |

### Bridge → 浏览器

| type | 说明 |
|---|---|
| `ssh_connected` / `ssh_disconnected` / `ssh_error` | SSH 会话生命周期 |
| `ssh_output` | 交互终端输出流 |
| `exec_stdout` / `exec_stderr` / `exec_result` | Agent 命令执行生命周期和最终结果 |
| `bridge_log` | 带 `case_id`、`trace_id`、节点和事件标签的结构化回采日志 |

详细部署与端到端验证见 [K3s 部署指南](../docs/deploy/terminal-bridge-k3s.md)。
