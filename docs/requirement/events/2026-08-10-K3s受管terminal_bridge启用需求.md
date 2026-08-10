---
status: approved
category: requirement
audience: architect, developer, operator, tester
last_updated: 2026-08-10
owner: team
---

# K3s 受管 terminal_bridge 启用需求

## 1. 背景与问题

当前 terminal_bridge 同时存在 Windows 桌面进程和 Linux Docker 联调进程。浏览器到 Bridge 的地址由浏览器所在机器决定，`localhost` 在远端 Windows 浏览器中指向 Windows 自身，不能指向 Linux；临时 Pod patch 也会在 Argo CD 同步或 Pod 重建后消失。该拓扑导致以下问题：

- Custom UI、Bridge、hci-sim 三者的运行位置难以从页面判断；
- `terminalBridgeUrl` 为空时静默回退到 `ws://localhost:9999`，失败表象只是 SSH 握手失败；
- Linux Docker Bridge 的运行状态不受 K3s/Argo 管理，无法纳入统一探针、日志和回滚；
- 两套 Bridge 并存时，操作者可能把“浏览器到 Bridge”端口和“Bridge 到 hci-sim SSH”端口混为一谈；
- 多副本会破坏当前单进程内存中的 SSH 会话路由。

## 2. 目标

在 dev/test 环境启用 K3s 受管的 terminal_bridge，使默认链路固定为：

```text
浏览器 Custom UI
  → 同源 /terminal-bridge（WebSocket）
  → K3s terminal_bridge Service/Deployment
  → hci-sim SSH
```

目标是让 Bridge 的镜像、配置、Origin 白名单、探针、日志、指标、持久化和回滚均由 Helm + GitOps 管理，并完成至少一个 KBD 仿真 TestRun 的端到端验证。

## 3. 用户故事

| 编号 | 用户 | 需求 | 验收结果 |
|---|---|---|---|
| TB-REQ-01 | 测试人员 | 打开同源 Custom UI 后直接使用 SSH/仿真测试 | 浏览器 WebSocket 连接 `/terminal-bridge`，不依赖 Windows Bridge |
| TB-REQ-02 | 运维人员 | 通过 Argo CD 管理 Bridge 的启停和版本 | 修改环境仓库 values 后可同步、审计和回滚 |
| TB-REQ-03 | 开发人员 | 定位 WebSocket、SSH、命令和 Lease 问题 | Pod 日志、Prometheus、Loki 和工单证据可用且可关联 |
| TB-REQ-04 | 安全审查人员 | 防止任意网页把 K3s Bridge 当 SSH 跳板 | Origin、NetworkPolicy、身份和命令边界均 fail-closed |

## 4. 必须满足的功能需求

1. Helm 提供 `terminalBridge.enabled`、镜像、监听端口、Origin、超时、输出上限、known_hosts 和持久化配置。
2. `terminalBridge.enabled=true` 时必须创建 Deployment、Service、探针、日志卷和必要的网络策略。
3. Customer UI 默认收到同源 `/terminal-bridge`，显式 `customerUI.terminalBridgeUrl` 仍可覆盖，用于迁移回滚和受控联调。
4. Ingress/Traefik 必须正确转发 WebSocket Upgrade、长连接和错误状态，不得把 WebSocket 降级为普通 HTTP。
5. Bridge 必须以 cluster 模式运行，禁止依赖宿主机 Docker socket、`hostNetwork`、特权容器或任意 shell 旁路。
6. Bridge 必须能够把 SSH Lease 的 `host/port/test_run_id/scenario_id` 原样、非敏感地关联到 hci-sim；Lease password、私钥和 HMAC key 不得进入日志、指标标签或 K8s 资源。
7. 当前实现采用单副本语义；若配置副本数大于 1，Helm/Admission/启动检查必须拒绝，直到会话状态完成外置或显式分片。
8. readiness 只证明 Bridge 进程和监听端口可用，不伪造 HCI 可达性；实际 SSH/exec 结果必须在 TestRun 中记录。
9. 关闭或回滚 K3s Bridge 后，必须有明确的 Windows 桌面 Bridge 回退路径，且不得自动把 K3s、Linux Docker 和 Windows 三者混用。

## 5. 非功能需求

### 可用性与性能

- WebSocket 握手 P95 不超过 1 秒（不含远端 SSH 认证）；
- Bridge readiness 在镜像启动后 30 秒内完成；
- 命令超时、取消、非零退出码和输出截断均返回结构化结果；
- Pod 重启后新会话可建立，旧会话必须显式失败，不得静默串到其他 TestRun。

### 安全

- cluster 模式默认只允许同源 Origin，不允许 `*`；
- Service 仅为 ClusterIP；不得通过 NodePort/公网 IP 暴露 SSH 跳板；
- Pod 使用非 root、禁止特权升级、drop all capabilities 和 RuntimeDefault seccomp；
- NetworkPolicy 只允许来自 Customer/Admin UI 的 WebSocket 流量及到受控 hci-sim SSH 目标的出站流量；
- 日志和 Prometheus label 禁止出现 password、私钥、Lease 明文和完整命令输出。

### 可观测性

- 必须有 health/live、health/ready、status、metrics；
- 结构化事件至少包含 `trace_id/test_run_id/exec_id/case_id`（敏感字段脱敏）；
- Loki、Prometheus 和工单 Bridge 日志能够按同一非敏感关联 ID 检索；
- 必须区分 `execution_mode=ssh`、`sim-ssh`、`replay`，仿真结果不得进入真实环境成功率。

## 6. 不在本需求范围

- 不在本阶段实现 terminal_bridge 多副本高可用；
- 不改变 Windows 桌面 Bridge 的生产认证协议；
- 不把 hci-sim 数据库拆分、TestRun 上下文绑定或 23821 完整 Fixture 实现混入本阶段代码；这些属于本次重构的另外两组，按依赖顺序实施；
- 不把 K3s Bridge 直接暴露为真实 HCI 的公网 SSH 代理。

## 7. 前置条件与依赖

1. `terminal_bridge/` Go 镜像可构建并通过现有 quality gate。
2. hci-sim 有可用的 Lease、SSH 端口和健康探针。
3. 外部环境仓库 `hci-platform-env` 的 dev values 是实际 Argo Application 生效来源。
4. Traefik/Ingress 支持 WebSocket Upgrade；集群 DNS、Service endpoints 和 NetworkPolicy 已可观测。
5. 迁移期间必须保留一键关闭 K3s Bridge 的回滚配置。

## 8. 验收门槛

只有以下全部满足，才可将本需求标记为完成：

- Argo CD 同步后 Bridge Deployment/Service/Endpoints 就绪；
- Customer UI runtime-config 为 `/terminal-bridge`，浏览器 Network 中没有回退到 `localhost:9999`；
- 一个正向 hci-sim TestRun 完成 `ssh_connect → exec → result`，同一 `test_run_id` 可在 Bridge 和 hci-sim 日志中关联；
- 错误 Origin、错误 Lease、不可达 SSH、命令非零退出和 Pod 重启均有可辨识的失败证据；
- 回滚到 Windows Bridge 后，页面不会继续缓存 K3s Bridge 地址；
- docs/solution、docs/task、docs/verify 的对应现行/事件文档与 README 第一屏已同步。

## 9. 关联文档

- [现有 K3s terminal_bridge 部署说明](../../deploy/terminal-bridge-k3s.md)
- [HCI 真实/仿真双轨需求](2026-07-30-HCI真实环境与仿真环境双轨测试需求.md)
- [本次重构方案](../../solution/events/2026-08-10-K3s受管terminal_bridge启用方案.md)
- [本次实施任务](../../task/events/2026-08-10-K3s受管terminal_bridge启用任务.md)
- [本次验证方案](../../verify/events/2026-08-10-K3s受管terminal_bridge启用验证.md)

## 当前实现状态（2026-08-10）

- namespace、Deployment、Service 和 NetworkPolicy 已由 Argo 创建；Pod 曾因 `hci-sim-credentials` 缺失处于 `ContainerCreating`，手工 bootstrap 后达到 `1/1`。
- 已确认需求中的 Gateway 控制面边界还需要两项落地：api-gateway 注入与 Runtime 相同的控制 Token，以及 hci-sim HTTP 8080 只允许 api-gateway 调用。
- Bridge→hci-sim SSH/exec-result 的 27123 现场证据已取得；正式 immutable image、Secret Manager/双 namespace provision 和 Agent 关联仍 pending。
- 因此需求保持 `approved`，但不把当前现场 patch 或 Pod Ready 视为需求全部满足。
