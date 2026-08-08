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

生产接入仍必须提供 PostgreSQL CAS/审计、不可变对象存储、权威 KBD/Tool/Artifact Resolver 和走 Customer UI/Terminal Bridge 协议的 `Runner`。没有这些依赖或受控真实 HCI 校准环境时，系统只能声明“代码级基础已就绪”，不能声明已支持任意 KBD 自动构建环境或完成产品级验证。

当前内存配额 Tracker 是单副本实现，因此 Helm `replicaCount` 必须为 `1`。共享 Store、Fixture 编译发布、TestRun 调度和规模验证属于后续阶段 C–E，不能据此基线推断已经支持自动化环境构建。

## C3 两步人工验收

dev 环境可使用仓库根目录的 `scripts/hci-sim/two-step-acceptance.sh <KBD_ID>`。脚本只接受已由 C1 Resolver 判定为 `ready_for_artifact_binding` 且在 synthetic 白名单中的 `23821`、`27736`、`34164`，生成 `positive-minimal` Manifest、短时 htp2 Lease 并启动单副本容器。第二步在 Custom UI SSH 终端选择“仿真租约”，按脚本输出的 `.hci-sim-run/<KBD_ID>-<UTC时间>/connection.json` 填写字段，执行 `recommended_command`。

输出明确为 synthetic signal-contract-only；没有获批 Artifact 时禁止使用 `positive-realistic` 或宣称真实 HCI/Artifact E2E。`27123` 等 capability gap 会在第一步失败且不产生 Lease。

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
