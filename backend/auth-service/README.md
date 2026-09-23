# auth-service

统一认证服务（阶段 0 骨架）。承载 admin / customer 双账号密码登录、JWT（RS256 + JWKS）签发、会话与登录审计。

## 职责边界

- ✅ 账号密码登录、JWT 签发（RS256 + JWKS）、登录审计、会话写入
- ❌ 不做授权：角色权限仍由下游既有逻辑判断（网关注入身份后，case-service / diagnosis-service 自判）

## 依赖管理

本仓库后端统一使用 **`uv`**（禁止使用系统 pip）：

```bash
cd backend/auth-service
uv venv
uv pip install -e ".[test]"      # 或显式：uv pip install cryptography argon2-cffi sqlalchemy pytest pydantic-settings
uv run pytest app/tests -q
```

> 生产依赖见 `pyproject.toml`；`[test]` extra 含 `pytest`。

## 本地运行

```bash
uv run uvicorn app.main:app --host 0.0.0.0 --port 8007 --reload
```

需要可连的 PostgreSQL（DATABASE_URL）与 RS256 私钥（AUTH_RSA_PRIVATE_KEY_PEM，缺失时生成临时内存密钥，**仅 dev/test**）。

## 端点

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/api/auth/customer/login` | realm=customer，aud=`hci-customer`，TTL 7d |
| POST | `/api/auth/admin/login` | realm=admin，aud=`hci-admin`，TTL 4h |
| GET  | `/.well-known/jwks.json` | JWKS 公钥（网关 / 下游验签） |
| GET  | `/health` `/health/live` `/health/ready` | 健康检查 |
| GET  | `/metrics` | Prometheus 指标 |

登录返回 `Bearer` access_token（JSON）+ `HttpOnly`/`Secure`/`SameSite=Lax` Session Cookie。

## 配置（环境变量）

| 变量 | 默认 | 说明 |
|---|---|---|
| `DATABASE_URL` | `postgresql+asyncpg://postgres:postgres@localhost:5432/hci` | 异步连接串 |
| `AUTH_RSA_PRIVATE_KEY_PEM` | 空（dev 临时密钥） | RS256 私钥 PEM |
| `JWT_ISSUER` | `hci-auth-service` | |
| `JWT_AUD_CUSTOMER` / `JWT_AUD_ADMIN` | `hci-customer` / `hci-admin` | |
| `ACCESS_TOKEN_TTL_CUSTOMER` / `_ADMIN` | 604800 / 14400 | 秒 |
| `SERVICE_PORT` | 8007 | |

## 目录

```
app/
├── main.py             # 入口：lifespan + 健康检查 + /metrics
├── config.py           # 配置单例（pydantic-settings，缺失时降级 os.getenv）
├── security/           # keys.py / jwt.py / password.py
├── services/           # repository.py（凭证校验 / 审计 / 会话 / 白名单）
├── routes/             # auth.py（登录 + JWKS 端点）
└── tests/              # test_security.py
```

## 与下游集成

JWT 使用 RS256 + JWKS，与 `diagnosis-service` 既有 `OidcJwtIdentityVerifier` 契约兼容：网关持 JWKS 公钥即可验签，auth-service 短暂不可用不影响已登录用户。
