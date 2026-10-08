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
- **客户侧权限集合必须显式包含 `customer`**：平台尚无客户登录体系，浏览器客户
  经网关重签后只剩这一个角色，因此凡是把 `customer_admin` / `field_engineer`
  当作"客户可用"的权限集合（`CREATE_ROLES`、`UPLOAD_ROLES`、`REPORT_READ_ROLES`、
  `list_available_scenarios` 与删除状态读取），都必须同时接受 `customer`，否则客户
  离线诊断会整体 403（B1 合入后实测回归，已由 `test_auth.py` 固化为契约）。
- **管理面权限集合不得包含 `customer`**：`ARTIFACT_ROLES` / `PLAN_ROLES` /
  `TRUST_ROLES` 等保持内部角色专属，降级才有意义。

## 5. 行为变化

| 场景 | 变化 |
| --- | --- |
| 客户离线诊断（自服务） | 不变：匿名访客由网关自动签发身份，按 `customer` 角色使用 `/api/diagnosis-*` |
| 客户访问 `/api/internal/*` | **403**（此前匿名即可读写） |
| admin 前端链路 | **收敛**：admin-ui Nginx 仅透传浏览器 `Authorization`（登录 JWT），不再注入 `X-Tenant-ID`/`X-Actor-ID`；身份上下文由网关从 admin JWT 派生（见 §6） |
| 内部服务直连 | 不变 |
| 本地 Compose | 与 K8s 行为一致：客户身份由网关签发，不再依赖注入的 dev 令牌 |

## 6. admin 身份上下文收敛（SRC-L1）

历史上 `frontend/admin/nginx.conf` 为 `/api` 注入服务端令牌与 `X-Tenant-ID`/`X-Actor-ID`，
使"能访问 Admin UI 入口"等同管理员（与修复前 customer 域同构）。admin 强制认证
（`AUTHN_ENFORCE_ADMIN`，关共享令牌后门）落地后，管理员身份唯一来源已收敛为
**auth-service 签发的 admin JWT**，故本次（SRC-L1）删除 admin-ui 的静态身份注入：

- **派生链**：浏览器（已登录 admin）→ admin-ui Nginx **仅透传 `Authorization $http_authorization`**
  （JWT）→ api-gateway `IdentityMiddleware` 验签 → `request.state.auth`（realm=admin, user_id）
  → `routes/diagnosis.py:_resigned_upstream_headers` 对 admin-realm 派生
  `X-Tenant-ID = AUTH_DEFAULT_TENANT_ID`（服务端配置）、`X-Actor-ID = user_id`（JWT sub）。
- **透传比静态注入更危险**：若只删 helm env 而保留 nginx `${ADMIN_API_TENANT_ID}` 默认
  （Dockerfile 原为 `$http_x_tenant_id`），envsubst 回退会**透传浏览器伪造头**，把客户端
  可控身份重新引入下游。故 Nginx 既**不静态写死、也不以 `$http_*` 透传** `X-Tenant-ID`/
  `X-Actor-ID`——网关对下游身份头是白名单重建（`FORWARDED_REQUEST_HEADERS` 不含二者），
  nginx 不发即安全，客户端伪造值不会直达下游。
- **四处联动删除**：`frontend/admin/nginx.conf` 注入两行、`Dockerfile` 的 `ADMIN_API_*` 透传默认值、
  helm `admin-ui/deployment.yaml` 的 `ADMIN_API_TENANT_ID`/`ADMIN_API_ACTOR_ID` env 与随之失效的
  `checksum/diagnosis-internal-auth` 滚动重启注解（admin-ui 不再持有内部身份 secret）、
  `values.yaml` 的 `diagnosisService.internalIdentity.*`、`config-contract.yaml` 的
  `DIAGNOSIS_DEV_TENANT_ID`/`DIAGNOSIS_DEV_ACTOR_ID` 声明与 `ADMIN_API_*` 运行时映射、
  `docker-compose.yml` 的 admin-ui 注入 env。
- **网络可达性治理保留**：`adminUI.ingress.internalGuard.enabled`（默认 false）作为纵深防御，
  开启后 Admin UI 入口复用 `internal-guard` 的 ipAllowList，仅允许内网源访问。

## 7. 验证

- 网关单测 `api-gateway/tests/unit`：含 diagnosis 重签矩阵 + **SRC-L1 admin-JWT 派生
  （`test_admin_jwt_realm_derives_tenant_and_actor_ignoring_self_report`：fake verifier
  注入 realm=admin actor，携带伪造 `X-Tenant-ID`/`X-Actor-ID`，断言上游用
  `AUTH_DEFAULT_TENANT_ID` + JWT `user_id`，忽略自报头）**；
- 诊断服务单测 `diagnosis-service/tests/unit`：**189 passed**（含角色头与归属校验）；
- chart：`helm unittest --strict`（admin 用例断言已翻转为 `notContains`/`isNull`）+ `helm lint`；
- 渲染契约脚本：`scripts/verify/verify_identity_resign.py`（customer + **admin 静态注入守卫 +
  admin-ui `ADMIN_API_*` env 前缀守卫**）；
- 归属审计脚本：`scripts/verify/verify_case_ownership.py`（SRC-L3，见 §9）；
- 部署后运行时验证：匿名/客户打 `/api/internal/*` 应为 403，admin（携 JWT）应为 200，
  客户打 `/api/diagnosis-*` 应正常。

## 8. SRC-L3 工单归属可观测（只读审计）

平台无客户登录，工单归属键为 `"case".client_id`（网关依据服务端签发的身份 Cookie
写入，客户凭 Cookie 回访）。因此"匿名工单"**不是可清理的孤儿**，而是无账号体系
模型的设计固有属性：Cookie 丢失即不可回访，但这**无法用 SQL 判定、更不得删除**
（删除只会毁灭真实数据）。SRC-L3 因此以**可观测审计 + 契约**收口，而非数据清理：

- 纯函数 `classify_client_id(value)` 把归属键分类为 `ok`/`anon_shape_ok`（合法：
  `client-<随机>`、32-hex、`hci-sim-admin` 等）或 `empty`/`placeholder`/`null`（异常：
  签发链/中间件被回退，如 SRC-L2 占位 token 复发）；异常判定可脱离数据库单测。
- 可选 `--database-url` 连库只读审计（总数、异常归属、conversation 无对应 case、
  `case.user_id` 无对应 user），默认只报告不改数据；`--strict` 时异常>0 非零退出，
  用于运维巡检 / 发布前门禁。
- staging 实测（2026-10-08，453 行）：异常归属=0、conversation 孤儿=0、user 孤儿=0，
  归属形态为 `client-<随机>` 228 + `hci-sim-admin` 212 + 测试种子，**无完整性缺陷**。

## 9. 变更文件

```
backend/api-gateway/app/routes/diagnosis.py          身份重签 + internal 管理面 require_admin + admin-realm 派生
backend/api-gateway/tests/unit/test_diagnosis_proxy.py   + admin-JWT 派生单测
backend/diagnosis-service/app/auth.py               角色头 + 归属校验
backend/diagnosis-service/tests/unit/test_auth.py
frontend/customer/nginx.conf                        删除注入
frontend/admin/nginx.conf                           SRC-L1：删除 X-Tenant-ID/X-Actor-ID 注入，保留 Authorization 透传
frontend/admin/Dockerfile                           SRC-L1：删除 ADMIN_API_TENANT_ID/ACTOR_ID 透传默认值
deploy/helm/hci-platform/templates/customer-ui/deployment.yaml
deploy/helm/hci-platform/templates/admin-ui/deployment.yaml   SRC-L1：删除 ADMIN_API_* env + 失效滚动重启注解
deploy/helm/hci-platform/templates/ingress.yaml     /api 直达网关
deploy/helm/hci-platform/templates/admin-ui/ingress.yaml
deploy/helm/hci-platform/values.yaml                SRC-L1：删除 diagnosisService.internalIdentity.*
deploy/helm/hci-platform/tests/*.yaml               SRC-L1：admin 用例断言翻转 notContains/isNull
deploy/config/config-contract.yaml                  SRC-L1：删除 DIAGNOSIS_DEV_* 与 ADMIN_API_* 映射
deploy/docker/docker-compose.yml                    SRC-L1：admin-ui 删除注入 env；.env.example 同步
scripts/verify/verify_identity_resign.py            + admin 静态注入 + ADMIN_API_* env 前缀守卫
scripts/verify/verify_case_ownership.py             SRC-L3：归属只读审计（新增）
tests/unit/test_verify_case_ownership.py            SRC-L3：分类纯函数单测（新增）
```
