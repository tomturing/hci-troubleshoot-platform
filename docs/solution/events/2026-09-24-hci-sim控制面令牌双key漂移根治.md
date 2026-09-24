# hci-sim 控制面令牌「双 key」漂移根治

- 日期：2026-09-24
- 环境：staging（`hci-staging` / `hci-sim-staging`）
- 分类：配置治理 / 服务间鉴权
- 状态：已应急恢复 + 本 PR 根治

## 一、现象

PR-C（admin 前端强制登录、清除共享内部令牌回退，#1091）人工回归时，管理员登录后
以下三个 `/api/hci-sim/*` GET 接口返回 **403**，其余接口正常：

- `/api/hci-sim/v1/simulations/capabilities/<id>`
- `/api/hci-sim/v1/control-plane/bundles`
- `/api/hci-sim/v1/control-plane/fixture-assets`

## 二、分层定位（可追踪调用链）

沿 `唯一调用链` 逐层施压，用运行时证据排除每一层的臆测：

1. **入口/网关鉴权层**：`/api/v1/kbd/capabilities` 实测 **200** → admin JWT 有效、
   `require_admin` 正常放行。三接口路由 `backend/api-gateway/app/routes/simulations.py`
   （`prefix=/api/hci-sim`，`dependencies=[Depends(require_admin)]`）**已过** `require_admin`，
   日志可见网关进入 handler 并向 `hci-sim.hci-sim-staging.svc:8080` 发起 httpx 转发。
   → **403 来自下游 hci-sim Runtime，而非网关 `require_admin`，与 PR-C 无因果**（PR-C 未碰网关转发/Runtime 令牌）。

2. **服务间转发层**：网关转发使用**服务间令牌** `HCI_SIM_CONTROL_TOKEN`（与浏览器 JWT 无关）。
   在网关 Pod 内直连 Runtime 做 header 对照实验（token-only / +actor / no-auth）：**三组全 403**
   → 排除「缺 actor 头」，锁定为**令牌值本身不被 Runtime 接受**。

3. **Runtime 鉴权层**：`hci_sim/cmd/hci-sim/controlplane_api.go` 的
   `controlPlaneAuthorized()` 只做单令牌比对：
   `Authorization == "Bearer " + HCI_SIM_CONTROL_TOKEN`；且 `HCI_SIM_ALLOW_INSECURE_CONTROL_API=false`
   无旁路 → 令牌不等即 `forbidden`。

## 三、根因（跨 namespace 令牌「双 key」脱钩）

两侧从**不同 secret 对象、不同 key** 读取本应相同的令牌。SHA-256 前缀比对（不泄露明文）：

| 角色 | env 来源 | SHA 前缀 |
|---|---|---|
| 网关（转发发出） | `hci-staging/hci-secrets` → `INTERNAL_API_TOKEN` | `5392a1` |
| Runtime 期望 | `hci-sim-staging/hci-sim-credentials` → `control-plane-token` | `f88039` |
| 同 secret 旁证 | `hci-sim-credentials` → `INTERNAL_API_TOKEN` | `5392a1` |

即 `hci-sim-credentials` 被 provision 成同时含 `INTERNAL_API_TOKEN`(=新值) 与
`control-plane-token`(=旧值) 两个 key。某次轮换 `INTERNAL_API_TOKEN` 时，运维同步了
`hci-sim-credentials.INTERNAL_API_TOKEN` 与 `hci-secrets.INTERNAL_API_TOKEN`，**但漏改影子 key
`control-plane-token`**（Runtime 恰好只读它）→ 网关(A)≠Runtime(B) → 一律 403。

`hci-sim-credentials` 无 ArgoCD tracking-id、chart 不创建它（`ignoreDifferences /data`），
由外部手动 provision，故 Git/selfHeal 均不覆盖其值，漂移不被自动纠正。

## 四、处置

### 4.1 应急（已执行，非本 PR）
将 `hci-sim-credentials.control-plane-token` 对齐为 canonical `INTERNAL_API_TOKEN` 值并
`rollout restart deploy/hci-sim`；从网关 Pod 内直连三接口复测全部 **200**。

### 4.2 根治（本 PR）
消除「双 key」这一漂移面 —— 让 Runtime 与网关**引用同名 key `INTERNAL_API_TOKEN`**，废弃影子 key
`control-plane-token`：

- `deploy/helm/hci-sim/values.yaml`：`runtime.controlTokenSecretKey: control-plane-token`
  → `INTERNAL_API_TOKEN`（附注释记录契约）。
- `deploy/gitops/argo-apps/cloud/hci-sim-staging.yaml`：Runtime `runtime` 段**显式声明**
  `controlTokenSecretKey: INTERNAL_API_TOKEN`，不依赖 chart 默认，防二者脱钩回潮。

## 五、provision 契约（运维须遵守）

轮换 `INTERNAL_API_TOKEN` 时，需保证**同名 key**在两 namespace 的 secret 中一致：

1. `hci-staging/hci-secrets.INTERNAL_API_TOKEN` —— 由 env 仓 Git/ArgoCD 自动渲染。
2. `hci-sim-staging/hci-sim-credentials.INTERNAL_API_TOKEN` —— 手动 patch（`rollout restart deploy/hci-sim`）。

根治后不再有第三处影子 key 需同步。建议后续评估以 External Secrets Operator / secret-reflector
将 (2) 自动化为对 (1) 的镜像，彻底消除手动同步点。

## 六、验证

- `helm template` 渲染确认 `HCI_SIM_CONTROL_TOKEN` 的 `secretKeyRef.key == INTERNAL_API_TOKEN`。
- 合并 sync 后：Runtime Pod 起新 spec，从网关 Pod 内直连三接口应 200；浏览器硬刷
  Bundle 工厂 / 仿真控制面 / fixture 资产页应恢复。
