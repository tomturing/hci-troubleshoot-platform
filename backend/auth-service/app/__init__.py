"""统一认证服务（auth-service）。

职责边界（与现有体系对齐）：
- 仅做**认证**：账号、凭证校验、会话、JWT 签发/轮换/吊销、登录审计。
- **不做授权**：角色权限集合仍沿用既有常量（_INTERNAL_ROLES / CUSTOMER_ROLE）
  与下游鉴权逻辑，本服务只把"actor 来源"从自报头换成可信 JWT。
- 令牌为非对称签名（RS256 + JWKS）：auth-service 持私钥签发，网关/服务
  用 JWKS 公钥验签，auth-service 短暂不可用不影响已登录用户。
"""

__all__ = ["__version__"]

__version__ = "0.1.0"
