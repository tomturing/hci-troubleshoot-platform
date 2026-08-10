# terminal_bridge K3s 双运行形态部署与端到端验证

## 1. 目标与边界

当前 HTP 的业务服务、数据库和可观测性组件均运行在 WSL Ubuntu 的本地 K3s 中，但 `terminal_bridge` 传统上运行在 Windows 客户端。虽然工单执行日志可以经 Custom UI 回采，Bridge 进程自身的状态、启动失败、WebSocket 连接数、SSH 会话数、本地缓冲和运行时异常仍留在 Windows 侧，云端链路存在观测断点。

本方案将同一套 `terminal_bridge` Go 代码扩展为两种运行形态：

```text
desktop（客户/生产）
Custom UI ── ws://localhost:9999 ──> Windows terminal_bridge.exe ── SSH ──> HCI

cluster（WSL 本地端到端联调）
Custom UI ── 同源 /terminal-bridge ──> K3s terminal_bridge Pod ── SSH ──> HCI
                                          │
                                          ├── stdout JSON ──> Alloy/Loki
                                          ├── /metrics ─────> Prometheus/Grafana
                                          └── bridge_log ───> 工单日志回采接口
```

核心边界：

- Windows EXE 和 Linux 容器不分叉源码；WebSocket/SSH/日志逻辑完全复用。
- Helm 默认 `terminalBridge.enabled: false`，不会改变 staging/prod 的客户侧 SSH 拓扑。
- 仅当 K3s Pod 网络可以直接 SSH 到 HCI 后台时启用 cluster 模式。
- cluster 模式默认只允许同源 Origin，禁止把服务暴露成任意网页可调用的内网 SSH 跳板。
- 当前 SSH 会话保存在单进程内存，Deployment 只能使用一个副本。

## 2. 运行时选择机制

customer-ui 不再把 Bridge 地址永久写死为唯一值。nginx 在运行时生成 `/runtime-config.js`：

- `terminalBridge.enabled=false`：注入空值，前端回退到 `ws://localhost:9999`；
- `terminalBridge.enabled=true`：注入 `/terminal-bridge`，前端根据页面协议自动使用 `ws://` 或 `wss://` 同源地址。

因此，切换运行形态只需要 Helm 配置，不需要为了 Bridge URL 重新构建 customer-ui。公网 HTTPS 页面会自动使用 `wss://`，不会产生 mixed content。

## 3. 构建

### 3.1 Linux 容器

```bash
cd /mnt/d/aihci/hci-troubleshoot-platform

docker build --network host \
  -t hci-terminal-bridge:dev \
  -f terminal_bridge/Dockerfile .
```

在本项目本地 K3s 构建链路中：

```bash
IMAGE_TAG=latest \
BUILD_ONLY_IMAGES=hci-terminal-bridge,hci-customer-ui \
bash scripts/ops/k3s-build.sh
```

`k3s-build.sh` 会把镜像导入 K3s containerd。注意 K3s 不直接读取 Docker daemon 镜像，参见部署避坑指南 PIT-016。

### 3.2 Windows 客户端

```bat
cd terminal_bridge
build_windows.bat
```

或在有 Go 工具链的 Linux/WSL 中执行：

```bash
bash scripts/build_terminal_bridge.sh
```

两种产物都从 `terminal_bridge/main.go` 构建。任何协议、SSH、日志和错误处理调整都同时进入后续 Windows EXE 与 Linux 镜像。

## 4. Helm 配置与部署

本地 values 覆盖：

```yaml
terminalBridge:
  enabled: true
  replicaCount: 1
  allowedOrigins: "same-origin"
  networkPolicy:
    enabled: true
    ingressNamespace: traefik
    hciSimNamespace: hci-sim-dev
    hciSimSshPort: 2222
    observabilityNamespace: hci-observability
  image:
    repository: hci-terminal-bridge
    tag: latest
```

当前环境采用双仓部署。先确认环境仓库 `environments/dev/values.yaml`（或应用仓库 `.local/values-dualrepo-local.yaml`）包含上述 `terminalBridge` 配置，再执行：

```bash
IMAGE_TAG=latest \
BUILD_ONLY_IMAGES=hci-terminal-bridge,hci-customer-ui \
bash scripts/ops/k3s-build.sh

bash scripts/ops/k3s-deploy-dualrepo.sh --env dev
```

也可直接 Helm 渲染确认：

```bash
helm lint deploy/helm/hci-platform --set terminalBridge.enabled=true

helm template hci-platform deploy/helm/hci-platform \
  --set terminalBridge.enabled=true \
  | rg 'terminal-bridge|TERMINAL_BRIDGE_URL|HCI_BRIDGE_ALLOWED_ORIGINS'
```

部署资源包括：

- `Deployment/terminal-bridge`
- `Service/terminal-bridge`
- 主 Ingress 的 `/terminal-bridge` Prefix 路由
- customer-ui 的 `TERMINAL_BRIDGE_URL=/terminal-bridge`
- `terminal-bridge-allow-required` NetworkPolicy（只允许 Traefik 入站、DNS/hci-sim SSH/Tempo 出站）
- Prometheus Pod scrape annotations
- 日志与 SSH `known_hosts` 使用 PVC 持久化，单文件达到上限后滚动

## 5. 安全设计

### 5.1 网络入口

cluster 模式默认 `HCI_BRIDGE_ALLOWED_ORIGINS=same-origin`。Bridge 会比较浏览器 `Origin` 与请求 `Host`，非同源请求返回 403。可配置逗号分隔的明确 Origin，例如：

```yaml
terminalBridge:
  allowedOrigins: "https://hci.local:4443,http://hci.local:4888"
```

不要在集群模式中配置 `*`。`*` 只为 Windows localhost 桌面兼容保留。

### 5.2 Pod 安全基线

Pod 和镜像均以 uid/gid 1000 非 root 用户运行，并继承 Chart 的以下约束：

- `runAsNonRoot: true`
- `allowPrivilegeEscalation: false`
- drop all capabilities
- `seccompProfile: RuntimeDefault`

Bridge 不需要 `hostNetwork`、`privileged`、ServiceAccount token 或宿主机 socket。阶段一默认只允许 Pod 出站到
`hci-sim` 的 SSH Service（`hci-sim-dev:2222`）、集群 DNS 和 Tempo OTLP；不放行任意真实 HCI 网段。
如果迁移期必须连接真实 HCI，必须在独立环境 values 中显式提供经过评审的 NetworkPolicy，不能临时关闭后遗留。

### 5.3 凭据与日志

SSH 密码/私钥只存在于浏览器到 Bridge 的 WebSocket 请求和当前连接的 Bridge 进程内存中，断开后立即清理，不写入结构化日志、状态端点或 Prometheus label。命令只记录脱敏文本和 hash；完整受控输出进入 Artifact。Loki、Artifact 与工单审计权限应沿用平台管理员边界。

## 6. 可观测性

### 6.1 健康与状态

```bash
kubectl -n hci-dev get pod -l app.kubernetes.io/name=terminal-bridge

curl -s http://hci.local/terminal-bridge/health/live
curl -s http://hci.local/terminal-bridge/health/ready
curl -s http://hci.local/terminal-bridge/status | python3 -m json.tool
```

`/status` 返回：

- 构建版本、commit、构建时间和运行模式；
- 监听地址；
- 活跃浏览器 WebSocket 数；
- 活跃 SSH 会话数和认证缓存工单数；
- 内存回放日志数、订阅者数、待回采数和当前本地日志文件大小。

readiness 不主动探测 HCI，因为 Bridge 不应持有一套全局 SSH 凭据，且不同工单目标不同。HCI 连通性由实际 `ssh_connect` 结果观测。

### 6.2 Prometheus

主要指标：

- `bridge_process_up`
- `bridge_build_info`
- `bridge_websocket_connections_active`
- `bridge_ssh_sessions_active`
- `bridge_ssh_connections_total`
- `bridge_ssh_connection_errors_total`
- `bridge_exec_commands_total`
- `bridge_exec_command_errors_total`
- `bridge_logs_collected_total`
- `bridge_logs_collect_errors_total`
- `bridge_logs_replayed_total`
- `bridge_log_subscribers_active`
- `bridge_log_buffer_entries`
- `bridge_log_pending_entries`
- `bridge_log_file_bytes`

集群内部验证：

```bash
kubectl -n hci-dev port-forward svc/terminal-bridge 9999:9999
curl -s http://127.0.0.1:9999/metrics | rg '^bridge_'
```

### 6.3 Loki 与工单回采

Pod stdout 保留一行一个结构化 JSON 事件，Grafana Alloy 使用 CRI parser 像采集其他 K3s Pod 一样写入 Loki，从而补齐原先 Windows 本地进程不可见的程序级日志。与此同时，带 `case_id` 的 `bridge_log` 仍经 customer-ui 持久 Outbox 批量提交 `/api/bridge-logs` 落库，用于工单维度审计。

本地 `bridge.log` 只用于短期断线/重启回放，不是长期日志权威源。Pod 替换会清空 `emptyDir`；长期查询以 Loki 和数据库回采记录为准。

## 7. 端到端联调验收

### 7.1 基础链路

```bash
kubectl -n hci-dev get deploy,svc terminal-bridge
kubectl -n hci-dev get endpoints terminal-bridge
kubectl -n hci-dev logs deploy/terminal-bridge --tail=50
```

浏览器打开 customer-ui 后，在开发者工具执行：

```javascript
window.__HCI_RUNTIME_CONFIG__
```

期望 `terminalBridgeUrl` 为 `/terminal-bridge`。Network 面板中 WebSocket 应连接当前页面同源的 `/terminal-bridge`，而不是 `localhost:9999`。

### 7.2 SSH 与 Agent 命令链路

1. 创建工单并提交 HCI SSH 信息。
2. 确认 UI 收到 `ssh_connected`。
3. 触发 Agent 的 `ssh_exec_process` 工具调用。
4. 确认 UI 收到 `exec_stdout`/`exec_stderr` 和最终 `exec_result`。
5. 查询 `/terminal-bridge/status`，确认 `active_sessions` 与 WebSocket 数符合预期。
6. 查询 Prometheus 指标，确认 SSH/命令计数增加，失败时 error counter 增加。
7. 在 Loki 按 `app_kubernetes_io_name="terminal-bridge"` 查询程序日志。
8. 在 `bridge_execution_log` 表按 `case_id`/`trace_id` 验证工单回采日志。

### 7.3 故障注入

至少验证以下场景：

- 错误 SSH 密码：UI 返回明确认证错误，`bridge_ssh_connection_errors_total` 增加；
- 不可达节点：连接超时/无路由被结构化记录；
- 非零退出码：`exec_result.exit_code` 正确，命令错误计数增加；
- 浏览器断开重连：发送 `resume` 后近期工单日志回放；
- Pod 重启：服务探针恢复，Loki 保留重启前程序日志；
- 非同源 Origin：WebSocket 握手返回 403，并记录 `websocket.origin_rejected`。

## 8. 回滚

只关闭 Pod 形态即可恢复原 Windows 客户端拓扑：

```bash
helm upgrade hci-platform deploy/helm/hci-platform \
  --reuse-values \
  --set terminalBridge.enabled=false
```

customer-ui 运行时配置随 Pod 重建恢复为空，前端重新回退到 `ws://localhost:9999`。关闭前确认当前联调工单不再依赖 Pod 内存中的 SSH 会话。
