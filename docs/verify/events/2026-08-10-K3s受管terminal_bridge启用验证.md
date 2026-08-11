---
status: passed
category: verify
audience: tester, operator, reviewer, release
last_updated: 2026-08-11
owner: team
---

# K3s 受管 terminal_bridge 启用验证

对应需求：[K3s 受管 terminal_bridge 启用需求](../../requirement/events/2026-08-10-K3s受管terminal_bridge启用需求.md)

对应方案：[K3s 受管 terminal_bridge 启用方案](../../solution/events/2026-08-10-K3s受管terminal_bridge启用方案.md)

## 1. 验证边界

本验证只在 dev/test K3s 进行，目标是证明：

```text
Browser → same-origin WebSocket → K3s terminal_bridge → hci-sim SSH
```

不把单次 Bridge/SSH 成功表述为真实 HCI 成功，也不验证生产多副本高可用。

## 2. 前置检查

```bash
kubectl -n argocd get application hci-platform-dev -o yaml
kubectl -n hci-dev get deploy,svc,endpoints terminal-bridge
kubectl -n hci-dev get networkpolicy
kubectl -n hci-dev get secret | grep -E 'terminal|hci-sim' || true
helm lint deploy/helm/hci-platform
helm template hci-platform deploy/helm/hci-platform \
  --set terminalBridge.enabled=true \
  | grep -E 'terminal-bridge|TERMINAL_BRIDGE_URL|/terminal-bridge'
```

验收标准：实际 Argo source/values 与预期一致，Deployment/Service/Endpoint 的 selector、端口和 namespace 一致，Helm 无 template error。

## 3. 静态与自动验证

| 编号 | 操作 | 期望 |
|---|---|---|
| A-01 | terminal_bridge `go test ./...` | PASS |
| A-02 | Helm lint/template/unittest | PASS |
| A-03 | Origin/Lease/exec contract tests | PASS |
| A-04 | NetworkPolicy static review | 只允许声明的 UI→Bridge、Bridge→hci-sim/DNS/OTel |
| A-05 | image digest/非 root/securityContext 检查 | PASS |
| A-06 | Customer runtime-config build test | enabled 时输出 `/terminal-bridge`，显式 URL 优先 |

## 4. 部署验证

```bash
kubectl -n hci-dev rollout status deploy/terminal-bridge --timeout=120s
kubectl -n hci-dev get pod -l app.kubernetes.io/name=terminal-bridge -o wide
kubectl -n hci-dev get endpoints terminal-bridge -o yaml
kubectl -n hci-dev logs deploy/terminal-bridge --tail=100
```

验收标准：

- Pod Ready；
- 无 ImagePullBackOff、CrashLoopBackOff、权限错误；
- Service 有 endpoints；
- readiness 只表示进程就绪，不虚报 HCI 连接成功；
- Pod 使用非 root，未挂载 docker.sock、hostNetwork 或 privileged。

## 5. 浏览器 WebSocket 验证

1. 打开与 Customer UI 同源的 HTTPS/HTTP 地址。
2. 查看 `/runtime-config.js` 或 `window.__HCI_RUNTIME_CONFIG__`。
3. 进入开发者工具 Network，筛选 WebSocket。
4. 打开连接流程。

验收标准：

```text
runtime terminalBridgeUrl = /terminal-bridge
WebSocket status = 101 Switching Protocols
请求 URL 为当前页面同源路径
没有 ws://localhost:9999
没有 ws://172.28.24.21:9999 的旧 Docker Bridge 地址
```

## 6. hci-sim 正向链路

使用已生成的、未过期的仿真 TestRun，执行：

```text
ssh_connect
ssh_exec_process
ssh_disconnect
```

按以下顺序收集证据：

```bash
kubectl -n hci-dev logs deploy/terminal-bridge --since=10m
kubectl -n hci-dev logs deploy/hci-sim --since=10m
curl -s http://<same-origin>/terminal-bridge/metrics | rg '^bridge_'
```

验收标准：

- Bridge 和 hci-sim 日志含同一 `test_run_id`；
- Bridge 有 `ssh.connected`、`exec.done`；
- hci-sim 有 `ssh.connected`、route 命中、`exec.done`；
- `exit_code=0` 时返回结果完整；
- `simulation=true/execution_mode=sim-ssh`；
- 没有 password、HMAC key、完整 Lease 出现在日志或指标 label。

## 7. 负向与故障注入矩阵

| 编号 | 注入 | 预期表现 | 必须有的证据 |
|---|---|---|---|
| F-01 | Origin 非白名单 | WebSocket 403 | Bridge origin rejected + counter |
| F-02 | 过期 Lease | 认证拒绝 | hci-sim lease reject，Bridge 不 exec |
| F-03 | 错误 host/port | 连接失败 | SSH error/timeout，非模糊 handshake |
| F-04 | 非零 route | exec result 保留 exit code | Bridge/hci-sim command error |
| F-05 | 输出超限 | 截断并标记 | output truncated 指标/事件 |
| F-06 | 浏览器断开 | session 清理，不能串 case | disconnect + active session 回落 |
| F-07 | Bridge Pod restart | 旧 session 明确失败，新 Lease 可连接 | rollout + 新 TestRun 证据 |
| F-08 | 临时 `kubectl patch` | Argo self-heal 恢复 Git 状态 | Application diff/同步记录 |
| F-09 | replicaCount=2 | Helm/CI 阻断 | lint/policy failure |
| F-10 | hci-sim Service 无 endpoint | Bridge 失败封闭 | readiness/SSH error，无真实 HCI fallback |

## 8. 回滚验证

1. 将环境仓库 values 回滚到 `terminalBridge.enabled=false`。
2. Argo 同步并等待 Customer UI rollout。
3. 确认 runtime-config 不再指向 K3s `/terminal-bridge`。
4. 确认活动 TestRun 被 draining/failed 记录，不会新建命令。
5. 若启用桌面 Bridge，验证仅通过明确配置的桌面 URL 连接。

验收标准：回滚后没有 K3s Bridge 残留流量，且页面不会缓存旧 URL。

## 9. 通过/阻断规则

### 通过

- A-01～A-06 全部通过；
- 部署、浏览器、hci-sim 正向链路全通过；
- F-01～F-10 的错误边界符合预期；
- 回滚可重复完成；
- 同一 TestRun 的四层证据完整。

### 阻断

- 发现浏览器静默回退 localhost；
- K3s Bridge 可被非同源页面调用；
- session 在不同 Pod 间串线；
- 仿真失败后回退真实 HCI；
- 日志泄露 Lease/密码；
- 只有 Pod Running 而没有真实 WebSocket/SSH/exec 证据。

## 10. 证据记录模板

```text
environment:
  argo_revision:
  bridge_image_digest:
  hci_sim_image_digest:
  bridge_pod:

test_run:
  test_run_id:
  scenario_id:
  execution_mode:

browser:
  runtime_config:
  websocket_url:
  websocket_status:

bridge:
  trace_id:
  ssh_connected_at:
  exec_id:
  exit_code:

hci_sim:
  lease_result:
  route_key:
  exec_result:

result: PASS | BLOCKED
block_reason:
```

## 已执行现场证据（2026-08-10）

### P0 镜像发布与 GitOps 部署前置证据

| 项目 | 结果 | 证据 |
|---|---|---|
| main 发布构建 | passed | CI run `31386141111` 的“构建并推送镜像（hci-sim）”成功；构建输入为 `hci_sim/Dockerfile`。 |
| 发布镜像 | passed | `ghcr.io/tomturing/hci-troubleshoot-platform/hci-sim:20260810-1204-946af37`。 |
| manifest digest | passed | `sha256:ff0732825a96db8858f03c8b6d574b57dd0dce1fe7c2e60153c6d7b5079cfd02`；CI buildx metadata 与镜像 tag 一致。 |
| Argo source | in_progress | `hci-sim-dev` Application 将从旧的 `ghcr.io/tomturing/hci-sim:dev-local` 切换到上述 repository@digest；需等待 GitOps commit 同步后完成干净重建。 |

上述 digest 是 OCI manifest list digest，部署时必须使用 `repository@sha256:...`，不能改写成 tag，也不能依赖节点已有的 `dev-local` 镜像。

| 用例 | 结果 | 证据摘要 |
|---|---|---|
| Runtime 健康与数据库 | passed（旧 dev-local 现场） | `hci-sim-dev/hci-sim` 达到 `1/1`；`/status` 显示 `database_configured=true`、`database_name=hci_sim`，日志含固定 Bundle digest。immutable digest 部署后需重复。 |
| Gateway build/TestRun | passed（临时现场 patch） | 27123 的 Gateway build 返回 HTTP 200、`execution_mode=sim-ssh`、connection；TestRun 返回 HTTP 200，`test_run_id` 与 build 一致。 |
| Bridge→Runtime SSH/exec | passed（临时 dev image） | `hci-sim-smoke` 五项全部通过：4 条正向/信号命令和 1 条未知命令的 fail-closed 127；Bridge 日志记录 SSH 认证成功和隔离 exec。 |

边界：Gateway/Bridge 正向证据已经取得，但之前的运行面证据使用了未进入 Argo source 的本地 `dev-local-v2` 镜像和临时 NetworkPolicy patch；因此在本变更合入并完成 Argo digest 同步、删除旧 Pod 后，必须按本文件第 6～8 节重跑并保存脱敏日志。23821 因当前 Bundle 未加载，保持 `capability_gap`。

## 最终复验（2026-08-11）

旧的临时证据已由 GitOps 受管链路复验替代。`hci-sim-dev` 为 Synced/Healthy，唯一 Runtime Pod 使用 digest `sha256:9307b597...`；浏览器建立 `ws://172.28.24.21/terminal-bridge`，Bridge 连接 K3s Service `hci-sim.hci-sim-dev.svc:2222`。Run `run-27123-2527ff1a376851c56e0b3e11` 的 3 个 Agent exec 在 Bridge/hci-sim 两端均 exit code 0，Case `Q2026081180588`、Conversation `217066bf-...` 和 trace `ac2de123...` 可关联，最终 Result 为 passed。

结论：K3s 单副本受管 Bridge 的 27123 产品链路 `PASS`；23821/full matrix 与多副本容灾不在本结论内。
