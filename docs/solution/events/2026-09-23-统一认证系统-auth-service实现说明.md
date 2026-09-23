# 统一认证系统 — auth-service 实现说明（阶段 0：T5–T9）

> 本文是 `docs/solution/events/2026-09-23-统一认证系统设计方案.md` 的实现层补充，记录阶段 0 的 auth-service 骨架落地细节。

## 1. 服务定位

独立部署的认证服务（`backend/auth-service`），承载：

- admin / customer 双 realm 账号密码登录
- JWT（RS256 + JWKS）签发
- 登录审计（`auth_audit`）、会话写入（`auth_session`）
- **不做授权**：角色权限仍由下游既有逻辑判断（网关注入身份后，case-service / diagnosis-service 自判）

与现有"网关注入 `X-Client-ID`（HMAC）+ admin 用 `INTERNAL_API_TOKEN`"体系并存，通过 feature flag 灰度切换（见设计方案阶段 1）。

## 2. 目录结构

```
backend/auth-service/
├── pyproject.toml          # 依赖与 pytest 配置
├── Dockerfile              # 仅做认证，最小镜像
├── .gitignore
└── app/
    ├── main.py             # 入口：lifespan + 健康检查 + /metrics
    ├── config.py           # 配置单例（pydantic-settings，无依赖时降级 os.getenv）
    ├── security/
    │   ├── keys.py         # RS256 私钥加载 + JWKS 导出
    │   ├── jwt.py          # RS256 签发（claims 含 tv=token_version）
    │   └── password.py     # argon2id 校验
    ├── services/
    │   └── repository.py   # 凭证校验 / 角色读取 / 审计 / 会话 / 白名单
    ├── routes/
    │   └── auth.py         # 双 realm 登录端点 + JWKS 端点
    └── tests/
        └── test_security.py
```

## 3. 安全核心

### 3.1 `keys.py` — 密钥与 JWKS

- `AUTH_RSA_PRIVATE_KEY_PEM`（env，PEM）加载私钥；缺失时生成**临时内存 RSA 密钥**并告警（仅 dev/test，重启即失效，**禁止生产**）
- `get_jwks()` 导出 JWKS 公钥（`kty/alg/kid/use/n/e`），供网关 / 下游验签
- **架构收益**：auth-service 短暂不可用不影响已登录用户——网关持 JWKS 公钥即可验签

### 3.2 `jwt.py` — 签发

`issue_token(user_id, realm, roles, aud, token_version, ttl)` → RS256 紧凑 JWT。

Claims（与下游 `OidcJwtIdentityVerifier` 契约对齐）：

| claim | 含义 |
|---|---|
| `iss` | `JWT_ISSUER` |
| `sub` | user_id |
| `realm` | `admin` / `customer` |
| `roles` | 角色列表（白名单校验后） |
| `aud` | `hci-admin` / `hci-customer`（区分双 realm） |
| `iat` / `exp` | 签发 / 过期 |
| `jti` | 唯一令牌 ID |
| `tv` | **token_version**：改角色 / 停用 / 改密时 +1 → 已签发 JWT 立即失效（见设计方案 §3.8） |

header 含 `kid`，下游据此选 JWKS 公钥。

### 3.3 `password.py` — 密码

- `argon2id`（`PasswordHasher`）：`hash_password` / `verify_password`
- verify 对任意异常（不匹配 / 哈希损坏）统一返回 `False`

## 4. 路由（`auth.py`）

| 方法 | 路径 | realm | aud | TTL |
|---|---|---|---|---|
| POST | `/api/auth/customer/login` | customer | `hci-customer` | 7d |
| POST | `/api/auth/admin/login` | admin | `hci-admin` | 4h（短时效，配合 `tv`） |
| GET | `/.well-known/jwks.json` | — | — | JWKS 公钥 |

登录成功返回 `Bearer` access_token（JSON 体）+ 下发 `HttpOnly`/`Secure`/`SameSite=Lax` Session Cookie（关键操作二次校验防 CSRF）。

## 5. 数据访问（`repository.py`）

自包含 SQLAlchemy async（与 `shared.DatabaseManager` 解耦，避免引入未知接口）。流程：

1. 按 `(credential_type='password', identifier, realm)` 查 `user_credential` + `user`
2. 锁定检查（`locked_until`）
3. `argon2id` 校验；失败累加 `failed_attempts`
4. `status != 'active'` 拒绝
5. 读取 `user_role`；**角色白名单校验（T2a）**——越权则拒绝签发（DB 无 CHECK 的应用层兜底）
6. 成功：清零失败计数 + 写 `auth_audit`（success）+ 写 `auth_session`

## 6. 与下游兼容性（关键验证点）

auth-service 使用 **RS256 + JWKS**，与 `diagnosis-service` 既有 `OidcJwtIdentityVerifier`（RS256 + JWKS）**契约兼容**：

- 阶段 0 已内联验证：用 auth-service 私钥签发的 JWT，可被对应 JWKS 导出的公钥按 RS256 验签通过
- 这意味着阶段 1 网关侧可直接复用 `OidcJwtIdentityVerifier`，用 JWKS 公钥验签并注入 `request.state`，无需 auth-service 在线

## 7. 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://...` | 异步连接串 |
| `AUTH_RSA_PRIVATE_KEY_PEM` | 空（dev 临时密钥） | RS256 私钥 PEM（生产由 K8s Secret 注入） |
| `JWT_ISSUER` | `hci-auth-service` | |
| `JWT_AUD_CUSTOMER` / `JWT_AUD_ADMIN` | `hci-customer` / `hci-admin` | |
| `ACCESS_TOKEN_TTL_CUSTOMER` / `_ADMIN` | 7d / 4h | |
| `SERVICE_PORT` | 8007 | |

## 8. 测试与验证

- 依赖管理：**`uv`**（环境规则强制）。`uv venv` + `uv pip install cryptography argon2-cffi sqlalchemy pytest pydantic-settings` + `uv run pytest`
- `app/tests/test_security.py`：JWT 签发/JWKS 验签、过期、argon2id 往返、角色白名单、JWKS 导出 —— **5 passed**
- schema 侧：`atlas migrate validate` 通过
- 门禁：`ruff check` / `ruff format --check` 通过

## 9. 后续阶段（本 PR 不含）

- 阶段 1：网关验签改造（复用 `OidcJwtIdentityVerifier` 注入身份）+ admin 登录灰度（双 feature flag）
- 阶段 2+：customer 登录、建单强制登录、历史匿名工单认领
