# hci_sim

`hci_sim/` 是 HCI 模拟 SSH Runtime 的唯一正式源码目录；产品、镜像和 Helm release 名保持 `hci-sim`，但仓库中不再保留第二套 `hci-sim/` Go Spike，避免实现和安全边界漂移。

## 阶段 A/B 基线

- 仅加载带有 `bundle.digest` 的已发布 Manifest v2；加载时拒绝未知字段、摘要漂移、非规范 argv 和歧义 RouteKey。
- 每个命令精确绑定 `variant + tool + acquisition_key + argv + virtual_node_id + container`，没有正则、通配符、评分路由或默认 fixture。
- SSH 仅接受 `htp2` 租约；租约签名、issuer、audience、Bundle、KBD、工具/策略版本、目标、时效及会话/命令/输出配额均受校验。
- `exec` 与交互 shell 共用有界 worker 队列、每命令授权和 fail-closed 路由；原始命令和 token 不进入日志。
- Terminal Bridge 只能通过完整的 `auth_type=lease`、`execution_mode=sim-ssh`、`htp2.*` 认证上下文标记模拟执行，绝不根据 host 名猜测。

## C–E 控制面基础

`internal/controlplane` 实现了不在 SSH 数据面运行的阶段 C–E 业务内核：冻结编译输入与 Bundle 生命周期、双角色审批和 stale；按精确 Bundle 创建的 TestRun、短时 Run Lease 与容量预留；差分、mutation、稳定性和容量阶梯证据模型。`MemoryRegistry` 只用于确定性单元测试，绝不能部署为多副本生产存储。

生产接入已提供 hci_sim 专用 PostgreSQL CAS、Event/Result/outbox 和受控对象 URI 边界；`Runner` 仍必须提供权威 KBD/Tool/Artifact Resolver、Oracle 结果和受控 Customer UI/Terminal Bridge 协议。没有 approved Artifact 或真实 HCI 校准环境时，系统只能声明“代码级基础已就绪”，不能声明已支持任意 KBD 自动构建环境或完成产品级验证。

当前内存配额 Tracker 是单副本实现，因此 Helm `replicaCount` 必须为 `1`。共享 Store、Fixture 编译发布、TestRun 调度和规模验证属于后续阶段 C–E，不能据此基线推断已经支持自动化环境构建。

Runtime 配置了 `HCI_SIM_OUTBOX_WEBHOOK_URL` 后会启动 durable outbox reconciler；没有配置下游 URL 时只恢复过期 processing 记录，不会把任何事件标记为 `processed`。最终 Run 结论必须通过 `/v1/simulations/test-runs/{id}/result` 由 Runner/Oracle 提交。

## C3 两步人工验收

dev 环境可使用仓库根目录的 `scripts/hci-sim/two-step-acceptance.sh <KBD_SUPPORT_ID>`。C1 Resolver 从已发布 KBD Snapshot（快照）的 Signal（信号）和当前 Tool Registry（工具注册表）修订生成 `synthetic_routes`（合成路由），不按 KBD ID 维护白名单。每条路由把 `tool_revision` 和 `tool_checksum` 写入 Bundle（制品包）摘要；只要至少一条 Signal 能在无现场变量的情况下确定编译，即可生成 `positive-minimal` Manifest（清单）、短时 htp2 Lease（租约）并启动单副本容器；没有安全可编译路由时返回 Capability Gap（能力缺口）。

仓库中的 `kbd-27123-fixture-manifest.json` 只是 Golden E2E（黄金端到端测试）的默认夹具。Helm 通过 `fixture.manifestFile` 选择夹具，运行时代码、资源名和 Lease（租约）默认场景都不依赖该 KBD。

脚本默认优先使用 SSH `2222`、HTTP `18080`；如果其中一个端口已经被其他仿真实例占用，会自动为本次运行选择隔离端口，并把最终端口写入 `connection.json`。需要让端口占用直接失败时设置 `HCI_SIM_AUTO_PORTS=false`。同一个 KBD 的旧实例会在启动新 Lease 前自动停止，避免 Lease 与容器错配。

脚本对同一 KBD 自动停止上一轮受管容器，预检 SSH/HTTP 端口，使用 dev 主机稳定 SSH host key，并在 `readyz` 未通过时失败退出；它不会把 Lease password 打印到终端。连接文件路径和脱敏字段会输出到屏幕，密码只能从本地 0600 文件读取。默认 Lease TTL 为 15 分钟，生成后应立即完成第二步。

无工单的 Custom UI 初始状态可点击终端面板中的“仿真租约连接（dev）”直接打开仿真租约弹窗；不必先创建无 SSH 工单。选择“仿真租约”后可直接粘贴完整 `connection.json` 并点击“载入连接文件”，页面会校验 `support_id`、端口、`auth_type=lease`、`execution_mode=sim-ssh` 和 `expires_at`，自动填充连接字段及首条 `recommended_command`。完整可验收命令保存在 `recommended_commands`；已有工单仍可通过“连接 SSH”进入同一弹窗。Linux/K3s 联调时，Customer UI 的运行时配置必须指向 Linux Bridge（`/terminal-bridge` 或 `ws://<linux-host>:9999`），不能让远端浏览器默认回退到 Windows `localhost:9999`。

如果页面的 `/runtime-config.js` 返回 `terminalBridgeUrl: ""`，浏览器会按桌面兼容逻辑连接浏览器所在机器的 `ws://localhost:9999`；这不等于 Linux 主机的 `172.28.24.21:9999`。Linux Bridge 没有对应 `WebSocket 请求` 日志时，优先修复 runtime-config/Helm/GitOps 路由，不要反复更换 Lease password。

Bridge 拓扑必须与浏览器所在机器一致：

```text
Linux 集群联调：浏览器 → ws://172.28.24.21:9999（Linux terminal_bridge）→ SSH 172.28.24.21:<connection.port>（hci-sim）
Windows 桌面联调：浏览器（172.28.24.22）→ ws://localhost:9999（Windows terminal_bridge.exe）→ SSH 172.28.24.21:<connection.port>（hci-sim）
```

两种 Bridge 不要在同一个浏览器会话中混用。Windows 联调时应保持 Customer UI 的 `terminalBridgeUrl` 为空，让浏览器回退到 Windows 本机 `ws://localhost:9999`；Windows Bridge 的 SSH 目标始终填写 Lease 文件中的 `host`、`port`、`username` 和仿真租约 password，不要把 `localhost:9999` 当成 SSH 目标。

若要用 Linux Bridge 做无浏览器三段链路验收，可设置 `HCI_SIM_CONNECTION_JSON` 运行 `cmd/hci-sim-smoke`；该模式会通过 WebSocket 连接 Bridge，再经 SSH 执行 `recommended_command`，不绕过 Bridge。

示例（Linux/K3s dev）：

```bash
HCI_SIM_CONNECTION_JSON="$PWD/.hci-sim-run/<run>/connection.json" \
HCI_SIM_BRIDGE_URL=ws://127.0.0.1:9999 \
HCI_SIM_BRIDGE_ORIGIN=http://172.28.24.21 \
go run ./cmd/hci-sim-smoke
```

通过时只输出 `support_id/test_run_id/exit_code` 等非敏感摘要；Lease password 始终只从本地文件读取，不写入输出。

输出明确为 synthetic signal-contract-only；没有获批 Artifact 时禁止使用 `positive-realistic` 或宣称真实 HCI/Artifact E2E。不满足已发布快照、Tool 修订或安全可编译路由门禁的 KBD 会在第一步返回 capability gap，且不产生 Lease。

## Diagnosis Sample Lab（诊断样例实验室）

五篇 `diagnosis-signal-matrix-v1` KBD 的长期手测和自动回归统一使用仓库根目录的 `make diagnosis-lab-*` 命令。实验室支持同一场景多实例并存、六种 Variant（场景变体）、稳定 SSH Host Key（主机密钥）、短时 Lease（租约）、Terminal Bridge（终端桥）在线 smoke（冒烟测试）以及无网络 Go Collector（采集器）执行。

它与本节的 C3 两步验收没有运行时冲突：两者复用同一 `hci-sim` 二进制和 Capability Resolver（能力解析器），但使用不同容器标签、实例目录和生命周期规则。新增样例能力应进入 Diagnosis Sample Lab；`two-step-acceptance.sh` 保留为任意已发布 KBD 的通用最小验收入口。

完整说明见 [Diagnosis Sample Lab（诊断样例实验室）设计与使用说明](../docs/solution/agent/Diagnosis-Sample-Lab诊断样例实验室设计与使用说明.md)。

## 本地验证

宿主机不要求安装 Go；使用固定容器工具链：

```bash
docker run --rm --user "$(id -u):$(id -g)" \
  -e GOCACHE=/tmp/go-build-cache -e GOPATH=/tmp/go \
  -v "$PWD:/src" -w /src/hci_sim golang:1.24-alpine \
  sh -lc 'mkdir -p /tmp/go-build-cache /tmp/go && /usr/local/go/bin/gofmt -l . && /usr/local/go/bin/go test ./... && /usr/local/go/bin/go test -race ./... && /usr/local/go/bin/go vet ./... && /usr/local/go/bin/go build ./cmd/hci-sim && /usr/local/go/bin/go build ./cmd/hci-sim-smoke'
```

为发布前写入 Manifest digest：

```bash
go run ./cmd/hci-sim manifest-digest --manifest testdata/kbd-27123-fixture-manifest.json
```

将命令输出的 `sha256:` 值写入 `bundle.digest` 后，再加载/部署；Helm 随附的 Manifest 必须与测试样本字节一致。

## KBD 27123 范围

随附 bundle 明确绑定 `support_id=27123`、revision、checksum 和 `SIM-HCI-NODE-01/host`。签发租约示例：

```bash
HCI_SIM_FIXTURE_MANIFEST=testdata/kbd-27123-fixture-manifest.json \
HCI_SIM_LEASE_HMAC_KEY='至少32字节的开发密钥xxxxxxxxxxxxxxxx' \
go run ./cmd/hci-sim lease --test-run run-local --virtual-node SIM-HCI-NODE-01
```

该命令只输出 token 给调用方；不要写入终端历史、日志、工单或配置库。
