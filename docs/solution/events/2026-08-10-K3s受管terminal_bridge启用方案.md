---
status: proposed
category: solution
audience: architect, developer, operator, tester
last_updated: 2026-08-10
owner: team
---

# K3s 受管 terminal_bridge 启用方案

## 1. 背景与需求

本方案响应 [K3s 受管 terminal_bridge 启用需求](../../requirement/events/2026-08-10-K3s受管terminal_bridge启用需求.md)。当前 Bridge 的多运行位置造成地址语义、配置权威源、可观测性和回滚边界混乱。目标不是简单“把 Docker 容器搬进 K3s”，而是建立一条唯一、可证明、受 GitOps 管理的浏览器到 SSH 执行链路。

## 2. 第一性原理

terminal_bridge 的本质是一个权限放大边界：它接受浏览器消息并使用 SSH 凭据在另一个执行域中运行命令。因此设计必须首先满足以下不变量：

1. **唯一入口**：同一页面只能有一个权威 Bridge URL；不能静默尝试多个 Bridge。
2. **身份绑定**：一个 SSH session 只能属于一个 `case_id/test_run_id/execution_mode`。
3. **命令来源可证**：Agent 自动执行只能来自结构化 `agent_exec_command`，普通 Markdown/页面文本不能获得执行权。
4. **失败封闭**：Origin、Lease、host key、session 路由或 exec 结果不确定时必须停止，不能回退真实 HCI。
5. **敏感信息最小化**：凭据只存在于浏览器到 Bridge 的瞬时请求和 Bridge 进程内存，不进入日志、指标、数据库或 K8s manifest。
6. **可观察不等于可用**：Pod Ready 只代表进程就绪；只有真实 WebSocket、SSH 和 exec 证据才能证明链路可用。

## 3. 方案（WHAT）：目标架构

```text
                         GitOps 管理面
 hci-platform-env ──> Argo CD ──> Helm release
                                  │
                                  ▼
Browser ── HTTPS/WSS ──> Traefik Ingress
                             │ /terminal-bridge
                             ▼
                    terminal-bridge Service
                             │
                             ▼
                    terminal-bridge Pod (1 replica)
                       │        │        │
                       │        │        ├─ stdout JSON → Alloy/Loki
                       │        ├────────── /metrics → Prometheus
                       │
                       └─ SSH + Lease ──> hci-sim Service/Pod
                                               │
                                               └─ TestRun/exec evidence
```

浏览器只感知同源 `/terminal-bridge`。Service/Pod 地址、端口和镜像 tag 由 Helm 管理。K3s Bridge 通过集群网络访问 K3s hci-sim，不依赖 Windows localhost、Linux Docker Bridge 或宿主机端口映射。

## 4. 配置模型与权威源

### 4.1 权威源

环境配置权威源为外部环境仓库：

```text
hci-platform-env/environments/dev/values.yaml
```

应用仓库 Helm chart 只定义能力和安全默认值。`kubectl patch`、临时 Pod 文件修改和浏览器 localStorage 都不是长期配置来源。

### 4.2 Helm 配置

目标 values：

```yaml
terminalBridge:
  enabled: true
  replicaCount: 1
  allowedOrigins: same-origin
  port: 9999
  hostKeyPolicy: accept-new
  persistence:
    enabled: true
  image:
    repository: hci-terminal-bridge
    tag: <immutable-tag-or-digest>

customerUI:
  terminalBridgeUrl: ""
```

解析顺序：

```text
customerUI.terminalBridgeUrl 非空
  → 使用显式 URL（迁移/回滚用途）
terminalBridge.enabled=true
  → 使用同源 /terminal-bridge
否则
  → Windows desktop 兼容模式；页面必须明确提示而不是隐式猜测
```

迁移完成后 dev/test 不设置显式主机 IP，避免把临时 Linux Docker Bridge 固化为长期依赖。

## 5. 请求与会话生命周期

```text
WebSocket handshake
  → Origin/Host 校验
  → 浏览器连接建立
  → ssh_connect(case_id, execution_mode, test_run_id, target)
  → 认证与 host key 校验
  → session key = case_id + test_run_id + target
  → agent_exec_command(exec_id, command, limits)
  → ssh_exec_process
  → stdout/stderr 有界流
  → exec_result(exit_code, timeout, truncated)
  → ssh_disconnect / WebSocket close / TTL 到期
```

### 会话不变量

- `sim-ssh` 必须携带有效 TestRun/Lease；
- 普通 SSH 与 sim-ssh 不能共用同一 session；
- `exec_id` 在一个 TestRun 内唯一，重复提交返回已有结果或明确冲突；
- 浏览器重连必须显式 resume，同一 session 不允许被另一个 case 抢占；
- Pod 重启使内存 session 失效，客户端必须重新获取 Lease，不自动恢复旧凭据。

## 6. 单副本决策

当前 Bridge 会话状态保存在进程内存，K8s Service 对多副本的随机分流会造成：

```text
ssh_connect → Pod A
exec → Pod B
Pod B 找不到 session
```

因此本阶段强制 `replicaCount=1`。这不是高可用终态，但比“表面多副本、会话随机丢失”更正确。未来扩展方案有两种：

1. 以一致性哈希/粘性会话按 `case_id/test_run_id` 分片；
2. 把 session 元数据外置，但 SSH TCP 连接仍属于具体 Pod，需要连接代理或显式 owner 路由。

在没有实现 owner 路由前不开放多副本。

## 7. 网络与安全设计

### 7.1 Ingress

- `/terminal-bridge` 使用 Prefix 路由到 ClusterIP Service；
- 保留 WebSocket Upgrade、原始 Origin 和 Host；
- 配置合理的 idle/read/write timeout；
- HTTPS 页面只使用 WSS，同源转换由前端运行时完成；
- 不允许外部 NodePort 直达 Bridge。

### 7.2 Origin 与身份

- cluster 模式默认 `same-origin`；
- Admin UI 与 Customer UI 如果入口不同，必须列出明确 Origin，禁止 `*`；
- Origin 校验不是用户认证替代品，Bridge 消息仍需绑定已授权 case/TestRun；
- 对错误 Origin 返回 403 并增加低基数计数，不记录完整敏感 URL。

### 7.3 NetworkPolicy

建议拆为：

```text
Ingress:
  Traefik/受控 UI namespace → terminal_bridge:9999

Egress:
  terminal_bridge → hci-sim SSH Service/port
  terminal_bridge → DNS
  terminal_bridge → OTel collector（如启用）
```

不授予访问 PostgreSQL、Kubernetes API、宿主机 socket 或真实 HCI 网段的默认权限。真实 SSH 模式如仍需保留，应使用独立环境/策略，不与仿真 Pod 的最小出站策略混合。

### 7.4 凭据与 known_hosts

- password/private key/Lease 只存内存；
- known_hosts 和非敏感短期日志可使用 PVC；
- host key 变化必须显式拒绝和人工处置，不自动覆盖；
- hci-sim 使用稳定的受管 host key；滚动替换必须有双 key 迁移窗口或显式清理步骤。

## 8. 可观测性设计

### 8.1 四层证据

| 层 | 证据 | 不能替代的事实 |
|---|---|---|
| Ingress | WebSocket 101、路由、延迟 | 不能证明 SSH 成功 |
| Bridge | websocket/ssh/exec 事件与指标 | 不能证明 hci-sim matcher 正确 |
| hci-sim | Lease、route、exec、signal | 不能证明 Agent 结论正确 |
| Agent/TestRun | tool_call/result、Signal outcome、conclusion | 不能证明真实 HCI 效果 |

### 8.2 关联字段

日志使用 `trace_id/test_run_id/case_id/exec_id/scenario_id`；Prometheus 禁止把这些无界 ID 作为 label。指标只使用低基数维度，如 execution mode、result class、error type。

### 8.3 SLO 与告警

- Bridge readiness 连续失败；
- WebSocket handshake error rate；
- SSH auth/connect error rate；
- exec timeout/nonzero/truncation rate；
- 活跃 session 接近容量；
- Loki/metrics 断流；
- Lease reject 突增。

## 9. 发布与回滚

### 发布顺序

1. 先发布不可变 Bridge 镜像；
2. Helm 模板/lint/安全策略通过；
3. 在 dev 环境仓库启用 Bridge；
4. Argo 同步 Deployment/Service/Ingress；
5. 仅做 Bridge smoke；
6. 启用 UI 同源 URL；
7. 执行 hci-sim 正向/负向 TestRun；
8. 观察窗口结束后停止 Linux Docker/Windows 联调 Bridge。

### 回滚

```yaml
terminalBridge:
  enabled: false

customerUI:
  terminalBridgeUrl: "ws://<approved-desktop-or-dev-bridge>:9999"
```

回滚前使新 TestRun 进入 draining，等待活动 session 结束或明确终止；不得在活动命令中间直接切换 URL。

## 10. 对抗性审查

| 攻击/故障 | 风险 | 防御与验收 |
|---|---|---|
| 恶意网页连接 Bridge | 内网 SSH 跳板 | same-origin + 身份绑定 + NetworkPolicy |
| 配置启用但镜像 tag 不存在 | Argo 表面同步、Pod ImagePullBackOff | immutable tag/digest + preflight |
| Service 端口错误 | Pod Ready 但 WS 不通 | Endpoint + 真实 101 smoke |
| 多副本随机路由 | 连接与命令落不同 Pod | 强制 1 replica |
| Lease 重放 | 越权复用 TestRun | TTL、jti、max session/command、TestRun 状态校验 |
| Pod 重启 | 内存 session 丢失 | 显式失败、新 Lease 重连，不伪恢复 |
| 输出洪泛 | 浏览器/Bridge OOM | 边缘筛选、字节上限、截断标记 |
| 日志泄密 | Lease/密码进入 Loki | 结构化白名单字段、secret scanner、负向测试 |
| Argo self-heal 覆盖现场 patch | 配置漂移 | 所有长期配置进入环境仓库 |

## 11. 决策依据（WHY：为什么选此方案，为什么不选其他方案）

### 不选继续使用 Windows terminal_bridge

Windows Bridge 适合真实客户桌面拓扑，但不适合作为 K3s 内部仿真测试的基础设施。它依赖操作者机器、localhost 语义和本地进程状态，不能提供稳定的 GitOps、K8s 探针和集群内网络边界。

### 不选 Linux Docker Bridge 长期运行

Docker Bridge 已证明协议可行，但其生命周期、镜像、日志和配置不受 Argo 管理。它只能作为迁移期 smoke，不应成为长期权威运行形态。

### 不选 UI 直连 hci-sim

hci-sim 提供 SSH 契约，不提供浏览器 WebSocket 安全代理。浏览器直连会绕开 Bridge 的命令授权、输出控制、审计和协议兼容，不满足真实执行链路一致性。

### 不选同时自动探测多套 Bridge

自动 fallback 会把配置错误隐藏成“偶尔成功”，甚至在 sim-ssh 失败后误连真实 HCI。一次运行必须且只能选择一个明确执行轨。

## 12. 影响范围

- `terminal_bridge/`
- `deploy/helm/hci-platform/templates/terminal-bridge/`
- Ingress 与 NetworkPolicy 模板
- Customer/Admin UI runtime config
- 外部 `hci-platform-env` dev values
- `docs/deploy/部署指南.md`、`docs/deploy/发布指南.md`
- `docs/solution/架构设计.md`、`docs/solution/可观测性设计.md`
- `docs/verify/测试指南.md`、`docs/README.md`

## 13. 验收标准

以 [K3s 受管 terminal_bridge 启用验证](../../verify/events/2026-08-10-K3s受管terminal_bridge启用验证.md) 为唯一验证清单；未提供同一 TestRun 的 Ingress、Bridge、hci-sim 和 Agent 分层证据时不得宣称端到端完成。
