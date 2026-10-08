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

## P1 扩面加固（2026-09-22）

P0 仅覆盖报告点名的 `cases` / `environments` / `terminal` / `conversations` 四族路由。
P1 将同源信任模型扩展到全部网关路由，并对 admin 控制面升级门禁：

| 路由族 | P1 门禁 | 调用方兼容性 |
|---|---|---|
| `/api/assistants`、`/api/audit-logs*` | `require_user` | 浏览器自动携带身份 Cookie，无感 |
| `/api/v1/kb/search`、`/api/v1/kb/sop/match`（客户对话链路） | `require_user` | 同上 |
| `/api/v1/signals/*`（试运行） | `require_user` | 同上 |
| `/api/hci-sim/*` 控制面（bundle 编译/发布/激活/回滚、TestRun） | `require_admin` | admin 前端统一携带 INTERNAL_API_TOKEN，零改动 |
| kb 管理家族：categories / catalogs / kbd / signal-assets / vm-console / sop、`kb/ingest`、`kb/documents*` | `require_admin` | 同上（下游 kb-service 本就要求 INTERNAL_API_TOKEN，网关此前自注入 Token 形成匿名旁路，现已封堵） |
| `WebSocket /ws/{client_id}` | 握手身份一致性校验 | 前端已无网关 WS 调用方；修复 P0 同源缺陷（路径自报 client_id 会被签名转发） |

**设计边界**：`require_user` 对匿名 HTTP 请求不产生 401——IdentityMiddleware 会对无 Cookie
访客自动签发身份（无登录模型的既定取舍）。user 级门禁的价值是强制请求携带可验签的可信身份；
**真正的安全边界是 `require_admin` 族与下游归属校验**。待客户登录体系（P1 后续）落地后，
user 级门禁可一键切换为真实用户语义。

## SRC-L2 技术债修复（2026-10-08）

P0/P1 已覆盖读侧与 admin 控制面，但 **conversation-service 的客户写入路由**仍停留在
MVP 的「桩鉴权」：网关对 `exec-result` / `vm-console-*` 注入固定占位符 token，下游
`_check_user_session` / `_check_session_or_internal` 只做长度校验，`bridge-logs` 直接
信任自报/占位身份。这些凭据与真实身份解耦，构成 IDOR 与越权回采面。本次将其收敛到与
读侧一致的**签名信任链**：

| 路由 | 上游（网关） | 下游（conversation-service）归属校验 |
|---|---|---|
| `POST /api/conversations/{id}/exec-result` | 已有 `require_user`，删除占位符兜底，改为透传 + 签名 `X-Client-ID` | `verify_conversation_ownership`（会话→case→client_id 比对） |
| `POST /api/conversations/{id}/vm-console-result` | 同上 | `verify_conversation_ownership` |
| `GET /api/conversations/{id}/vm-console-artifacts/{artifact}` | **新增** `require_user`（此前匿名可达）+ 签名注入 | `verify_conversation_ownership` + 记审计（actor=user:{client_id}） |
| `POST /api/bridge-logs`（批量） | **新增** `require_user` + 签名注入，删除占位符兜底 | `_verify_batch_ownership` 校验 `{fallback_case_id ∪ entry.case_id}` 全量归属 |
| `POST /api/bridge-logs/upload`（手动补采） | 同上 | 绑定工单先校验 + 逐条惰性归属（`owned_cache` 去重） |

要点：

- **归属校验凭据 = 网关签名 `X-Client-ID`**，与 `Authorization` 完全解耦；网关出口统一
  剥离用户可伪造的 `X-Client-ID` / `X-Client-Signature`，用 `INTERNAL_API_TOKEN` HMAC 重签。
- **内部直连旁路**：bridge-logs 保留 `Authorization: Bearer <INTERNAL_API_TOKEN>` 分支（返回
  `internal`、跳过归属），供集群内/离线场景使用；前端经网关一律走签名归属。exec-result /
  vm-console 无内部直连调用方，未保留旁路。
- **写库前校验、失败整单回滚**：归属 403 在事务 commit 之前抛出，越权条目不落库。
- **过渡语义不变**：`STRICT_IDENTITY_SIGNATURE=false` 时未签名请求 → `authenticate_request`
  返回 None → 匿名放行且跳过归属比对（存量兼容）；`=true` 时无签名 → 401。
- **admin 兼容**：`require_user` 在 `is_admin` 且带 `?client_id=` 时放行，管理台读侧不受影响。
- **前端**：`MessageBubble.vue` 控制台缩略图下载移除占位 `Authorization`，改依赖同源身份
  Cookie（浏览器自动携带，网关 `require_user` 校验）。
- **防回退守卫**：`scripts/verify/verify_identity_resign.py` 增加 SRC-L2 静态检查——生产
  源码（`api-gateway/app`、`conversation-service/app`、两前端 `src`）不得再出现占位符 token
  字面量，亦不得复活已删除的桩鉴权函数定义。

## 残留风险（P1 后续）

- `/api/diagnosis-*`（含 internal 端点）收紧前需确认运维脚本与已分发 `terminal_bridge.exe`
  的凭证注入方式（否则会中断自动上传）；`/api/bridge-logs` 已于 SRC-L2 收敛为
  网关签名归属（批量/上传均 `require_user` + 工单归属，保留内部令牌旁路）；
- `exec-result` / `vm-console-*` 占位 token 兜底已于 SRC-L2 移除，下游改 `verify_conversation_ownership`；
- admin-ui `nginx.conf` 仍静态注入 `Authorization` / `X-Tenant-ID` / `X-Actor-ID`（残留 P1 B3：
  管理员域名收敛 + 管理员认证），收敛完成后须把 `frontend/admin/nginx.conf` 并入
  `verify_identity_resign.py` 场景 2 的同款注入检查；
- `IDENTITY_COOKIE_SECURE` 生产 HTTPS 需置 True（当前默认 False，影响 http 直连调试）；
- 历史匿名工单未绑定身份，待客户登录体系落地后迁移孤儿数据。

## 变更历史

- 2026-08-25：初始版本，网关重签客户端 `X-Client-ID`（**方案不充分，已被 SRC-2026-5356 击穿**）
- 2026-09-21：P0 修复——网关签发服务端签名身份 Cookie（`hci_client_id`），彻底忽略客户端自报头；
  cases/environments/terminal 路由加 `require_user`/`require_admin`；case-service 强化归属校验与强制 limit
- 2026-09-22：P1 扩面——assistants/audit/kb-search/signal-dry-run 加 `require_user`；
  hci-sim 控制面与 kb 管理家族（categories/catalogs/kbd/signal-assets/vm-console/sop）加 `require_admin`；
  WebSocket 握手改为校验网关签发身份 Cookie 与路径 client_id 一致，堵住自报身份签名转发通道
- 2026-10-08：SRC-L2 技术债修复——conversation-service 客户写入路由（exec-result / vm-console-result /
  vm-console-artifacts 下载 / bridge-logs 批量 / bridge-logs upload）移除占位 token 与桩鉴权
  （`_check_user_session` / `_check_session_or_internal`），统一改为网关签名 `X-Client-ID` + 工单/会话
  归属校验（防 IDOR）；网关 vm-console-artifacts 下载与 bridge-logs 补 `require_user`；前端
  `MessageBubble.vue` 移除占位 Authorization 改依赖同源 Cookie；`verify_identity_resign.py` 增加 SRC-L2
  静态防回退守卫（admin-ui nginx 注入属 P1 B3 残项，暂未纳入）
