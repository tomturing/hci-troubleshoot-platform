# 服务间身份签名与网关鉴权机制（P0 修复后）

## 背景

为修复 IDOR 越权访问漏洞（CWE-639），平台引入**网关签发的服务端身份信任链**。
客户端身份由网关统一签发、签名并下发给 HttpOnly Cookie，下游服务基于"网关验签后的可信
client_id"做资源归属校验。

> **历史方案（2026-08-25，已废弃）**：早期方案由网关读取客户端**自报**的 `X-Client-ID` 并
> 重签转发，下游仅验签名真伪 + 归属比对。该方案未把身份绑定到可信签发方，攻击者可自报他人
> `client_id` 越权（外部安全报告 SRC-2026-5356 实锤）。**P0 修复已改为服务端签发 Cookie，
> 彻底忽略客户端自报头。**

## 架构

```
┌─────────┐  HttpOnly Cookie     ┌─────────────┐  签名头(X-Client-ID/    ┌──────────────────┐
│ Browser │ ───(hci_client_id)─▶ │ API Gateway │   X-Client-Signature)─▶│ case/conversation │
│         │                      │             │                        │ /terminal Service│
│ 自报头   │ ─── 被剥离 ─────────│ 签发+验签    │ 仅用 Cookie 验出的      │  验签 + 归属校验  │
│ X-Client│                      │ + require_*  │ client_id 重签         │                  │
└─────────┘                      └─────────────┘                        └──────────────────┘
```

信任根：身份由网关用共享密钥 `INTERNAL_API_TOKEN` 签名，下游**只信任网关注入的签名头**，
不再信任任何客户端自报头。

## 关键组件

### 1. 身份签发与验签（shared）

位置：`backend/shared/security/identity.py`

```python
from shared.security.identity import issue_identity, verify_identity

# 签发：返回 (client_id, cookie_value)
client_id, cookie = issue_identity(settings.INTERNAL_API_TOKEN)

# 验签：返回 client_id 或 None（无效/过期/篡改）
cid = verify_identity(cookie, settings.INTERNAL_API_TOKEN)
```

- Cookie 格式：`<client_id>.<timestamp>.<hmac>`
- `hmac = HMAC-SHA256(INTERNAL_API_TOKEN, "<timestamp>:<client_id>")`
- `client_id` 为服务端生成的随机串（uuid4 hex），客户端不可控

### 2. 网关身份中间件（api-gateway）

位置：`backend/api-gateway/app/middleware/identity.py`（`IdentityMiddleware`，`BaseHTTPMiddleware`）

职责（仅对 `/api` 路径，跳过 `/api/health`、`/api/metrics`）：

1. 读取 `hci_client_id` Cookie，用 `verify_identity` 验签；
2. 验签失败（缺失/过期/篡改）→ 重新 `issue_identity` 并 `set_cookie`（HttpOnly）；
3. 将可信身份写入请求上下文：`request.state.client_id`（验出的 client_id）、
   `request.state.is_admin`（当 `Authorization: Bearer <INTERNAL_API_TOKEN>` 时为真）、
   `request.state.auth_scheme`；
4. 非 `/api` 路径直接放行，不签发身份。

Cookie 属性：`httponly=True`、`samesite="lax"`、`secure=IDENTITY_COOKIE_SECURE`
（生产 HTTPS 应设 `True`）。

### 3. 网关鉴权依赖（api-gateway）

位置：`backend/api-gateway/app/security/gateway_auth.py`

```python
from app.security.gateway_auth import require_user, require_admin

# 强制已认证用户身份，返回 client_id
# - 普通用户：返回 Cookie 验出的 client_id（不可伪造）
# - 管理员(携带 INTERNAL_API_TOKEN)：优先使用其显式 ?client_id= 参数（信任 admin）
async def require_user(request) -> str: ...

# 强制管理员凭证，否则 403
async def require_admin(request) -> None: ...
```

- 路由装饰：`@router.get("/api/cases/", dependencies=[Depends(require_user)])`
- 管理路由（如 `/api/cases/all`、`/api/environments/all`）：`dependencies=[Depends(require_admin)]`

### 4. 网关出口代理（api-gateway）

- 用 `request.state.client_id` 调用 `sign_client_identity()` 重新生成签名头
  `X-Client-ID` / `X-Client-Signature`；
- **剥离**客户端自报的 `X-Client-ID` / `X-Client-Signature` / `X-Client-Environment`，
  杜绝伪造；
- 下游 `case-service` / `conversation-service` 的归属校验（`verify_case_ownership` 等）
  逻辑保持不变，但此时验签源已是网关可信身份。

### 5. 下游归属校验（case-service / conversation-service）

跨服务无共享 ORM，使用参数化 SQL 比对：

```python
# backend/case-service/app/security/auth.py
result = await session.execute(
    text('SELECT client_id FROM "case" WHERE case_id = :case_id'),
    {"case_id": case_id},
)
owner = result.scalar()
if owner != request.state.client_id:   # 实为网关注入的可信 client_id
    raise HTTPException(status_code=403, detail="无权访问该资源")
```

## 配置

### 必需配置

- `INTERNAL_API_TOKEN`：网关与下游服务共享，既用于服务间鉴权，也作为身份 Cookie 的 HMAC 密钥。
  Helm 部署经 `secrets.internalApiToken` 注入。
- `IDENTITY_COOKIE_NAME`：Cookie 名，默认 `hci_client_id`。
- `IDENTITY_COOKIE_SECURE`：是否仅 HTTPS 下发，默认 `False`（本地开发）；生产须 `True`。

### 行为约束

| 场景 | 行为 |
|------|------|
| 无 Cookie 访问 `/api/...` | 401（require_user 拒绝） |
| 伪造/篡改 Cookie | 验签失败 → 重新签发随机身份（原越权目标不可达） |
| 自报 `X-Client-ID` / `?client_id=` | 普通用户忽略；仅 `require_user` 在 admin 令牌下读 query |
| 非 admin 访问 `/api/.../all` | 403（require_admin 拒绝） |
| admin 令牌 + `?client_id=victim` | 以 victim 身份执行（信任 admin，与既有 admin 路由一致） |

## 防护机制

### 1. 防重放

签名时间戳须在 ±300 秒窗口内（`CLOCK_SKEW_SECONDS = 300`），过期拒绝。

### 2. 防篡改

- Cookie/签名 HMAC 用 `hmac.compare_digest` 比对；
- 客户端无法构造合法 `client_id`（由网关随机生成）；
- 网关强制剥离客户端自报身份头。

### 3. 零信任集群内访问

直连下游服务无有效签名仍 401；且网关已剥离自报头，即使攻击者直连也无法伪造归属。

## 前端对接

- `frontend/shared/src/api.ts`：**移除**自动注入 `X-Client-ID` 拦截器（身份改由 Cookie 承载），
  `listByClient` 等方法新增可选 `options` 参数透传 admin 令牌/显式 client_id；
- `frontend/admin/src/components/SimulationConversation.vue`：管理后台特权调用使用 `fetch`
  携带 `Authorization: Bearer <VITE_INTERNAL_API_TOKEN>` 与 `?client_id=`，并以 `credentials: "include"`
  携带网关 Cookie。

## 测试验证

```bash
# 身份 Cookie 模块（纯标准库）
PYTHONPATH=backend uv run pytest backend/tests/unit/test_identity_cookie.py -v

# 网关鉴权接线（require_user / require_admin / 中间件）
PYTHONPATH=api-gateway:backend uv run pytest backend/tests/unit/test_gateway_auth.py -v
```

覆盖场景：
- ✅ 签发/验签往返
- ✅ 错误密钥 / 篡改 / 缺失 / 畸形 Cookie 均拒绝
- ✅ require_user：Cookie 身份优先、admin 显式 client_id 覆盖、无身份→401
- ✅ require_admin：非 admin→403
- ✅ IdentityMiddleware：无 Cookie 签发并下发、合法复用、篡改重签、Bearer 标记 admin、非 /api 跳过

## 故障排查

### 问题：401 Unauthorized

1. 检查请求是否携带 `hci_client_id` Cookie（由网关首次访问自动签发）；
2. 检查 `INTERNAL_API_TOKEN` 网关与下游是否一致；
3. 查看网关日志确认中间件验签是否失败并重签。

### 问题：管理后台调用 403

确认 `Authorization: Bearer <INTERNAL_API_TOKEN>` 是否与部署的 `secrets.internalApiToken` 一致。

## 残留风险（P1，非本次范围）

- `download_vm_console_artifact` 网关路由仍无 `require_user`（下游用占位 token 伪鉴权，
  artifact_id 为 UUID），建议 P1 补 `require_user`；
- `exec-result` / `vm-console-*` 下游保留占位 token 兜底（内部伪鉴权，非报告利用点），建议 P1 移除；
- 历史匿名工单未绑定身份，建议 P1 引入客户登录并迁移孤儿数据。

## 变更历史

- 2026-08-25：初始版本，网关重签客户端 `X-Client-ID`（**方案不充分，已被 SRC-2026-5356 击穿**）
- 2026-09-21：P0 修复——网关签发服务端签名身份 Cookie（`hci_client_id`），彻底忽略客户端自报头；
  cases/environments/terminal 路由加 `require_user`/`require_admin`；case-service 强化归属校验与强制 limit
