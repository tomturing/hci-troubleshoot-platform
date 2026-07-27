---
status: active
category: deploy
audience: operator
date: 2026-07-27
related_prs: []
owner: team
---

# Terminal Bridge 端到端可观测性改进手册

## 1. 适用范围

本手册用于 WSL Ubuntu + K3s dev 联调，以及后续 Windows EXE 回归。所有协议和执行逻辑来自同一 Go 源码。

## 2. 推荐配置

```yaml
terminalBridge:
  enabled: true
  replicaCount: 1
  allowedOrigins: same-origin
  execTimeoutSeconds: 120
  execMaxOutputBytes: 4194304
  otelEndpoint: http://tempo.hci-observability.svc.cluster.local:4318
  hostKeyPolicy: accept-new
  persistence:
    enabled: true
    storageClass: local-path
    size: 512Mi
```

Windows/WSL 裸进程继续使用 `ws://localhost:9999`；K3s 使用同源 `/terminal-bridge`。

## 3. 构建与部署

```bash
IMAGE_TAG=dev-local \
BUILD_ONLY_IMAGES=hci-terminal-bridge,hci-customer-ui,hci-conversation-service,hci-api-gateway,hci-agent-service \
bash scripts/ops/k3s-build.sh

bash scripts/ops/k3s-deploy-dualrepo.sh \
  --env dev \
  --namespace hci-dev \
  --skip-traefik
```

可观测栈升级：

```bash
helm upgrade --install hci-platform-obs deploy/helm/hci-platform-obs \
  -n hci-observability \
  -f deploy/helm/hci-platform-obs/values.yaml \
  -f /mnt/d/aihci/hci-platform-env/environments/dev/values.yaml
```

升级后必须确认 `DaemonSet/alloy` Ready 且 `DaemonSet/promtail` 已不存在。

## 4. 验收顺序

### 4.1 静态与单元检查

```bash
helm lint deploy/helm/hci-platform
helm lint deploy/helm/hci-platform-obs
cd terminal_bridge && go test ./...
```

### 4.2 资源和入口

```bash
k3s kubectl -n hci-dev rollout status deploy/terminal-bridge --timeout=180s
k3s kubectl -n hci-dev get deploy,svc,pvc,ingress | rg terminal-bridge
curl -fsS http://127.0.0.1/terminal-bridge/health/ready
curl -fsS http://127.0.0.1/terminal-bridge/status
curl -fsS http://127.0.0.1/terminal-bridge/metrics
```

### 4.3 真实命令验收

从 customer-ui 建立 SSH 后，让 Agent 执行一条只读命令。记录 `exec_id`、`trace_id` 和 `artifact_id`，逐项查询：

```sql
SELECT exec_id, artifact_id, trace_id, exit_code, duration_ms,
       stdout_bytes, stderr_bytes, stdout_truncated, stderr_truncated,
       stdout_sha256, stderr_sha256, error_type
FROM bridge_execution_artifacts
WHERE exec_id = '<exec-id>';

SELECT event_id, bridge_instance_id, seq, event_time, trace_id, span_id,
       exec_id, event, level, success, error_type, artifact_id
FROM bridge_execution_logs
WHERE exec_id = '<exec-id>'
ORDER BY event_time;

SELECT id, exec_id, artifact_id, trace_id, bridge_trace_id, status,
       duration_ms, error_type, output_sha256
FROM tool_result
WHERE exec_id = '<exec-id>';
```

Artifact 默认保留 30 天，访问分类为 `restricted`。普通日志、`tool_result`、动态资源审计和 Langfuse 只保存脱敏输入、执行摘要、hash、截断标志及 Artifact ID，不复制原始命令输出。`terminal-bridge-artifact-cleanup` CronJob 每日删除 `expires_at < now()` 的过期 Artifact；结果写入路径也会机会性清理。验收时需确认 CronJob 已创建，并手工触发一次 Job 验证数据库连接。

Tempo 必须在同一 Trace 中看到 `terminal_bridge.websocket.receive`、`terminal_bridge.ssh.exec`、`terminal_bridge.websocket.result.send` 和结果回传 HTTP Span。

### 4.4 Loki / Alloy

```bash
k3s kubectl -n hci-observability get ds alloy
k3s kubectl -n hci-observability logs ds/alloy --tail=200
```

Loki 查询示例：

```logql
{namespace="hci-dev", service_name="terminal-bridge"} | json | trace_id="<trace-id>"
```

### 4.5 Langfuse 和指标

Langfuse tool observation metadata 必须包含 `exec_id`、`otel_trace_id`、`artifact_id` 和输出 hash。

Prometheus 查询：

```promql
sum by (tool_name, status) (rate(agent_tool_call_total[5m]))
histogram_quantile(0.95, sum by (le, tool_name) (rate(agent_tool_execution_duration_seconds_bucket[5m])))
sum by (tool_name, error_type) (rate(agent_tool_error_total[5m]))
```

## 5. 故障注入

至少验证：

1. 非零退出码：状态为 failed，`error_type=nonzero_exit`；
2. 超时：Bridge 关闭独立 SSH TCP 连接和 Session，`timed_out=true`，实际耗时应接近配置阈值而不是远端命令自然耗时；
3. 大输出：内存不继续增长，truncated=true，总字节和 SHA-256仍准确；
4. 重复日志：第二次回采计入 duplicates，不新增行；
5. 重复结果：已存在 Artifact 时返回幂等成功；
6. 错误 Origin：WebSocket 返回 403；
7. 主机指纹变化：accept-new/strict 均拒绝已知主机的变化。

可使用安全验收脚本（只输出字节数、hash、截断状态和错误分类，不输出凭据及命令正文）：

```bash
node scripts/verify/terminal-bridge-fault-injection.cjs
```

脚本必须通过环境变量传入浏览器、SSH 和故障命令参数，`HCI_FAULT_CASE_ID` 不得超过数据库字段上限 32 个字符。
超时测试可在 dev 临时把 `HCI_BRIDGE_EXEC_TIMEOUT_SECONDS` 调为 2，测试结束后必须恢复 120 并等待 rollout 完成。

超时验收必须同时核对四级预算：Bridge 权威执行超时为 Helm 权威值 `T`，浏览器等待 `T+15` 秒、Agent Redis 等待 `T+30` 秒、pending/result Redis TTL 为 `T+60` 秒；默认对应 120/135/150/180 秒。任何上游等待窗口早于 Bridge 权威超时，或后一级预算不大于前一级预算，都属于验收失败。

## 6. 回滚

业务回滚：

```bash
helm -n hci-dev history hci-platform
helm -n hci-dev rollback hci-platform <revision>
```

可观测栈回滚：

```bash
helm -n hci-observability history hci-platform-obs
helm -n hci-observability rollback hci-platform-obs <revision>
```

数据库新增字段是向后兼容的，应用回滚时默认保留。不要为了回滚应用而删除 Artifact 数据。
