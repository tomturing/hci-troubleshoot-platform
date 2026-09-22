# 网关身份重签（B1）

> 关联：SRC-2026-5358（内部管理面匿名越权）、SRC-2026-5356（身份可自报）
> 前置：[internal 管理面公网封禁（A1）](./internal-guard.md)、
> [身份 Cookie 签名（P0）](./identity-signature.md)

## 1. 背景

internal 模式下主 ingress 把全部 `/api` 先送 customer-ui，由
`frontend/customer/nginx.conf` 无条件注入 `Authorization: Bearer <INTERNAL_API_TOKEN>`
与租户/操作者头。由此产生一条**身份注入链**：

1. 匿名访客 → customer-ui Nginx 自动获得合法内部令牌与管理员操作者身份；
2. 网关 diagnosis 代理**信任并透传**这些自报头；
3. diagnosis-service 的 `InternalTokenIdentityVerifier` 对"带内部令牌者"一律授予
   三角色（`platform_admin` / `support_engineer` / `diagnosis_worker`）。

结果：**匿名 = 管理员**，可读写 `/api/internal/*` 管理面，且 P0 修复的越权校验
同样被该注入穿透。A1 的 guard Ingress 只收窄公网暴露面，内网源攻击者仍可利用。

修复必须作用在身份注入点本身——继续加网关中间件无效（中间件早已存在且被穿透）。

## 2. 方案（四件套，同一 PR 原子落地）

| 层次 | 变更 |
| --- | --- |
| 前端代理 | `frontend/customer/nginx.conf` 删除 `Authorization` / `X-Tenant-ID` / `X-Actor-ID` 注入三行 |
| 部署拓扑 | chart 删除 customer-ui 的注入 env；ingress `/api` 不再送 customer-ui，统一直达 api-gateway |
| 网关 | `routes/diagnosis.py` 按服务端签发的身份 Cookie **重签**下游身份头；`/api/internal/*` 与 bundle-migration 加 `require_admin` |
| 诊断服务 | 按网关注入的角色头区分客户与内部角色；工单归属校验改用 `case.client_id` |

## 3. 身份头契约（网关 → diagnosis-service）

| 头 | 含义 | 普通访客（Cookie） | 管理员 / 内部调用方 |
| --- | --- | --- | --- |
| `Authorization` | 网关服务身份 | 服务端注入 `Bearer <INTERNAL_API_TOKEN>` | 同左 |
| `X-Actor-Roles` | 角色集合 | `customer` | `platform_admin`（可自报，须在内部角色白名单内） |
| `X-Actor-Customer-ID` | 不可伪造的归属键 | Cookie 中的 `client_id` | 不发送 |
| `X-Actor-ID` | 审计用操作者 | `cust-<client_id>` | 自报值或 `gateway-admin` |
| `X-Tenant-ID` | 租户 | `default`（忽略自报） | 自报值或 `default` |

**客户端自报的租户、操作者与角色头一律不再作为可信输入**：普通访客即使携带
`X-Actor-Roles: platform_admin`，重签后仍为 `customer`。

## 4. 角色与工单归属

- `customer`：非特权角色。`InternalCaseAuthorizer` 走归属分支，比对
  `case.client_id`（匿名体系下工单的归属键，即网关签发的 `client_id`）；
  `case.customer_id` 为选填的客户档案维度（OIDC/CRM 场景），二者任一命中即视为归属。
- `platform_admin` / `support_engineer` / `diagnosis_worker`：仅校验工单存在性。
- **兼容**：直连 diagnosis-service 的既有内部调用方（hci-sim、diagnosis-worker、
  迁移工具）不发送 `X-Actor-Roles`，维持三角色语义不变。

## 5. 行为变化

| 场景 | 变化 |
| --- | --- |
| 客户离线诊断（自服务） | 不变：匿名访客由网关自动签发身份，按 `customer` 角色使用 `/api/diagnosis-*` |
| 客户访问 `/api/internal/*` | **403**（此前匿名即可读写） |
| admin 前端链路 | 不变：admin-ui Nginx 注入保留（管理端暂无独立登录凭证） |
| 内部服务直连 | 不变 |
| 本地 Compose | 与 K8s 行为一致：客户身份由网关签发，不再依赖注入的 dev 令牌 |

## 6. admin 域残余风险（已知，待后续收敛）

`frontend/admin/nginx.conf` 仍为 `/api` 注入服务端令牌，因此"能访问 Admin UI 入口"
即等同管理员——与本次修复前 customer 域的注入结构同构。治理依赖：

- 本次新增 `adminUI.ingress.internalGuard.enabled`（**默认 false**）：开启后 Admin UI
  入口复用 `internal-guard` 的 ipAllowList，仅允许内网源访问。默认关闭以保持既有
  访问方式，建议内网管理面部署开启。
- 根治路径（后续）：管理员认证 / 短时 JWT 化，配合 admin 域名收敛（网络可达性
  不应再等价于管理权限）。

## 7. 验证

- 网关单测 `api-gateway/tests/unit`：**107 passed**（含 diagnosis 重签矩阵 11 项）；
- 诊断服务单测 `diagnosis-service/tests/unit`：**189 passed**（含角色头与归属校验）；
- chart：`helm unittest` **47 passed** + `helm lint` 通过；
- 渲染契约脚本：`scripts/verify/verify_identity_resign.py`；
- 部署后运行时验证：匿名/客户打 `/api/internal/*` 应为 403，admin 应为 200，
  客户打 `/api/diagnosis-*` 应正常。

## 8. 变更文件

```
backend/api-gateway/app/routes/diagnosis.py          身份重签 + internal 管理面 require_admin
backend/api-gateway/tests/unit/test_diagnosis_proxy.py
backend/diagnosis-service/app/auth.py               角色头 + 归属校验
backend/diagnosis-service/tests/unit/test_auth.py
frontend/customer/nginx.conf                        删除注入
frontend/admin/nginx.conf                           注释与治理说明
deploy/helm/hci-platform/templates/customer-ui/deployment.yaml
deploy/helm/hci-platform/templates/ingress.yaml     /api 直达网关
deploy/helm/hci-platform/templates/admin-ui/ingress.yaml
deploy/helm/hci-platform/values.yaml
deploy/helm/hci-platform/tests/*.yaml
deploy/docker/docker-compose.yml
scripts/verify/verify_identity_resign.py
```
