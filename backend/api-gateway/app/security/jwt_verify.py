"""网关侧 JWT 验签器（阶段1）：验证 auth-service 签发的 RS256 JWT。

设计要点（对抗性审查结论，避免直接复用 diagnosis-service 的 OidcJwtIdentityVerifier）：
- `OidcJwtIdentityVerifier` 的 `ActorContext` 契约强制 tenant_id / customer_id claim，
  而 auth-service 签发的 JWT claims 为 (iss/sub/realm/roles/aud/exp/tv)，**无 tenant_id**
  —— 强行复用会令其 `_validate_claims` 返回 401，无法用于网关入口。
- 网关作为唯一 JWT 验签边界，验签成功后**复用 B1 已建立的下游信任头模型**
  （X-Actor-Roles / X-Actor-Customer-ID / X-Tenant-ID）向下游传播身份；下游
  diagnosis-service 仍用既有 `InternalTokenIdentityVerifier` 信任网关注入的头，零回归。
"""

import asyncio
import base64
import json
import time
from dataclasses import dataclass

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15
from fastapi import Request


@dataclass(frozen=True, slots=True)
class GatewayActor:
    """网关验签 JWT 后得到的可信操作者（仅含向下游传播所需的字段）。"""

    user_id: str
    realm: str
    roles: frozenset[str]
    audience: str


def _b64url_decode(value: str) -> bytes:
    """解码无填充 Base64URL。"""

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


class JwtVerifier:
    """RS256 + JWKS 缓存验签器：验证 auth-service 签发的 admin/customer JWT。"""

    def __init__(
        self,
        *,
        jwks_url: str,
        issuer: str,
        audience: str,
        clock_skew_seconds: int = 60,
    ):
        if not jwks_url.startswith(("http://", "https://")):
            raise ValueError("AUTH_JWKS_URL 必须是 HTTP(S) URL")
        self._jwks_url = jwks_url
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._clock_skew = max(0, min(int(clock_skew_seconds), 300))
        self._cache: dict[str, rsa.RSAPublicKey] = {}
        self._cache_expires_at = 0.0
        self._lock = asyncio.Lock()

    async def verify(self, request: Request) -> GatewayActor:
        """从请求头读取 Bearer Token 并验签，返回可信操作者。失败抛 ValueError。"""

        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token.strip():
            raise ValueError("缺少 Bearer Token")
        return await self.verify_token(token.strip())

    async def verify_token(self, token: str) -> GatewayActor:
        """验签 JWT 字符串（便于测试直接传入，绕过 Request 构造）。"""

        try:
            header_seg, payload_seg, sig_seg = token.split(".")
            header = json.loads(_b64url_decode(header_seg))
            claims = json.loads(_b64url_decode(payload_seg))
            signature = _b64url_decode(sig_seg)
        except (ValueError, json.JSONDecodeError) as exc:
            raise ValueError("JWT 格式非法") from exc

        public_key = await self._resolve_public_key(header)
        try:
            public_key.verify(
                signature,
                f"{header_seg}.{payload_seg}".encode(),
                PKCS1v15(),
                hashes.SHA256(),
            )
        except InvalidSignature as exc:
            raise ValueError("JWT 签名无效") from exc

        self._validate_claims(claims)
        roles = frozenset(r for r in (claims.get("roles") or []) if isinstance(r, str))
        return GatewayActor(
            user_id=str(claims["sub"]),
            realm=str(claims.get("realm", "")),
            roles=roles,
            audience=str(claims.get("aud", "")),
        )

    async def _resolve_public_key(self, header: dict) -> rsa.RSAPublicKey:
        kid = str(header.get("kid") or "")
        if not kid:
            raise ValueError("JWT 缺少 kid")
        if time.monotonic() >= self._cache_expires_at or kid not in self._cache:
            async with self._lock:
                if time.monotonic() >= self._cache_expires_at or kid not in self._cache:
                    await self._refresh_jwks()
        key = self._cache.get(kid)
        if key is None:
            raise ValueError("JWT 签名密钥未知")
        return key

    async def _refresh_jwks(self) -> None:
        """拉取并缓存 JWKS 公钥（5 分钟有效期）。"""

        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                response = await client.get(self._jwks_url, headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise ValueError(f"JWKS 不可用: {exc}") from exc

        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            raise ValueError("JWKS 契约不合法")
        cache: dict[str, rsa.RSAPublicKey] = {}
        for item in keys:
            if not isinstance(item, dict) or item.get("kty") != "RSA" or not item.get("kid"):
                continue
            if item.get("use") not in {None, "sig"}:
                continue
            try:
                exponent = int.from_bytes(_b64url_decode(str(item["e"])), "big")
                modulus = int.from_bytes(_b64url_decode(str(item["n"])), "big")
                cache[str(item["kid"])] = rsa.RSAPublicNumbers(exponent, modulus).public_key()
            except (ValueError, KeyError, TypeError):
                continue
        if not cache:
            raise ValueError("JWKS 无可用 RSA 签名密钥")
        self._cache = cache
        self._cache_expires_at = time.monotonic() + 300

    def _validate_claims(self, claims: dict) -> None:
        """校验签发方 / 受众 / 过期 / 必要 claim。失败抛 ValueError。"""

        now = time.time()
        audience = claims.get("aud")
        audience_matches = self._audience in audience if isinstance(audience, list) else audience == self._audience
        if claims.get("iss", "").rstrip("/") != self._issuer or not audience_matches:
            raise ValueError("签发方或受众不匹配")
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now - self._clock_skew:
            raise ValueError("令牌已过期")
        if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now + self._clock_skew:
            raise ValueError("令牌尚未生效")
        if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + self._clock_skew:
            raise ValueError("令牌签发时间异常")
        if not claims.get("sub") or not claims.get("realm") or not claims.get("roles"):
            raise ValueError("令牌缺少必要 Claim")
