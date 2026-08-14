"""身份验证、内部服务身份和工单对象级授权。"""

import asyncio
import base64
import json
import re
import secrets
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol

import httpx
from cryptography.exceptions import InvalidSignature
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519, padding, rsa
from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
from fastapi import Request
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.errors import DiagnosisError

_TRUSTED_HEADER_PATTERN = re.compile(r"^[A-Za-z0-9._:@-]{1,128}$")
_INTERNAL_ROLES = frozenset({"platform_admin", "support_engineer", "diagnosis_worker"})


@dataclass(frozen=True, slots=True)
class ActorContext:
    """由正式身份验证器产生的可信操作者上下文。"""

    tenant_id: str
    user_id: str
    roles: frozenset[str]
    customer_id: str | None = None

    def __post_init__(self) -> None:
        """拒绝不完整的可信身份。"""

        if not self.tenant_id.strip() or not self.user_id.strip() or not self.roles:
            raise ValueError("可信身份必须包含 tenant_id、user_id 和 roles")

    def has_any_role(self, *roles: str) -> bool:
        """判断操作者是否拥有任一角色。"""

        return bool(self.roles.intersection(roles))


class IdentityVerifier(Protocol):
    """正式身份提供方需要实现的最小协议。"""

    async def verify(self, request: Request) -> ActorContext:
        """校验请求并返回可信身份。"""


class CaseAuthorizer(Protocol):
    """工单对象级授权协议。"""

    async def assert_access(self, actor: ActorContext, case_id: str) -> None:
        """校验操作者是否可以访问目标工单。"""


class InternalTokenIdentityVerifier:
    """使用项目统一内部令牌生成可信的服务身份。"""

    def __init__(self, token: str):
        self._token = token.strip()

    async def verify(self, request: Request) -> ActorContext:
        """校验 Bearer Token，并读取受保护的调用方身份头。"""

        authorization = request.headers.get("Authorization", "")
        scheme, _, supplied_token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not supplied_token.strip():
            raise DiagnosisError(
                code="UNAUTHORIZED",
                message="缺少 Bearer Token",
                http_status=401,
            )
        if not self._token or not secrets.compare_digest(supplied_token.strip(), self._token):
            raise DiagnosisError(
                code="FORBIDDEN",
                message="内部接口令牌无效",
                http_status=403,
            )

        tenant_id = request.headers.get("X-Tenant-ID", "").strip()
        actor_id = request.headers.get("X-Actor-ID", "").strip() or "internal-service"
        if not _TRUSTED_HEADER_PATTERN.fullmatch(tenant_id):
            raise DiagnosisError(
                code="INVALID_TENANT_CONTEXT",
                message="内部调用必须提供合法的 X-Tenant-ID",
                http_status=422,
            )
        if not _TRUSTED_HEADER_PATTERN.fullmatch(actor_id):
            raise DiagnosisError(
                code="INVALID_ACTOR_CONTEXT",
                message="X-Actor-ID 格式不合法",
                http_status=422,
            )
        return ActorContext(tenant_id=tenant_id, user_id=actor_id, roles=_INTERNAL_ROLES)


class OidcJwtIdentityVerifier:
    """使用固定公钥或带缓存的 OIDC JWKS 验证正式 JWT。"""

    def __init__(
        self,
        *,
        public_key_pem_base64: str = "",
        jwks_url: str = "",
        issuer: str,
        audience: str,
        clock_skew_seconds: int = 60,
    ):
        self._public_key = None
        if public_key_pem_base64.strip():
            try:
                key_bytes = base64.b64decode(public_key_pem_base64, validate=True)
                self._public_key = serialization.load_pem_public_key(key_bytes)
            except (ValueError, TypeError) as exc:
                raise ValueError("OIDC_PUBLIC_KEY_PEM_B64 不是合法 PEM Base64") from exc
        self._jwks_url = jwks_url.strip()
        if self._jwks_url and not self._jwks_url.startswith("https://"):
            raise ValueError("OIDC_JWKS_URL 必须使用 HTTPS")
        if self._public_key is None and not self._jwks_url:
            raise ValueError("OIDC 必须配置固定公钥或 OIDC_JWKS_URL")
        if not issuer.strip() or not audience.strip():
            raise ValueError("OIDC_ISSUER 和 OIDC_AUDIENCE 不能为空")
        self._issuer = issuer.rstrip("/")
        self._audience = audience
        self._clock_skew_seconds = max(0, min(int(clock_skew_seconds), 300))
        self._jwks_cache: dict[str, object] = {}
        self._jwks_expires_at = 0.0
        self._jwks_lock = asyncio.Lock()

    async def verify(self, request: Request) -> ActorContext:
        """验证签名、签发方、受众和过期时间，并从签名 Claim 构造身份。"""

        authorization = request.headers.get("Authorization", "")
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() != "bearer" or not token:
            raise DiagnosisError(code="UNAUTHORIZED", message="缺少正式 Bearer Token", http_status=401)
        try:
            header_segment, payload_segment, signature_segment = token.split(".")
            header = json.loads(_base64url_decode(header_segment))
            claims = json.loads(_base64url_decode(payload_segment))
            signature = _base64url_decode(signature_segment)
            public_key = await self._resolve_public_key(header)
            self._verify_signature(
                public_key=public_key,
                algorithm=header.get("alg"),
                signing_input=f"{header_segment}.{payload_segment}".encode(),
                signature=signature,
            )
            self._validate_claims(claims)
            roles_value = claims.get("roles") or claims.get("role")
            roles = (
                frozenset(item for item in roles_value if isinstance(item, str))
                if isinstance(roles_value, list)
                else frozenset(str(roles_value or "").split())
            )
            actor = ActorContext(
                tenant_id=str(claims["tenant_id"]),
                user_id=str(claims["sub"]),
                roles=roles,
                customer_id=str(claims["customer_id"]) if claims.get("customer_id") else None,
            )
        except DiagnosisError:
            raise
        except (ValueError, KeyError, json.JSONDecodeError, InvalidSignature) as exc:
            raise DiagnosisError(code="INVALID_IDENTITY_TOKEN", message="正式身份令牌无效", http_status=401) from exc
        return actor

    async def _resolve_public_key(self, header: dict) -> object:
        if not self._jwks_url:
            return self._public_key
        kid = str(header.get("kid") or "")
        if not kid:
            raise DiagnosisError(code="IDENTITY_KID_MISSING", message="身份令牌缺少 kid", http_status=401)
        if time.monotonic() >= self._jwks_expires_at or kid not in self._jwks_cache:
            async with self._jwks_lock:
                if time.monotonic() >= self._jwks_expires_at or kid not in self._jwks_cache:
                    await self._refresh_jwks()
        key = self._jwks_cache.get(kid)
        if key is None:
            raise DiagnosisError(code="IDENTITY_KID_UNKNOWN", message="身份令牌签名密钥未知", http_status=401)
        return key

    async def _refresh_jwks(self) -> None:
        try:
            async with httpx.AsyncClient(timeout=5.0, follow_redirects=False) as client:
                response = await client.get(self._jwks_url, headers={"Accept": "application/json"})
                response.raise_for_status()
                payload = response.json()
        except Exception as exc:
            raise DiagnosisError(
                code="IDENTITY_PROVIDER_UNAVAILABLE",
                message="OIDC 签名密钥暂时不可用",
                http_status=503,
                retryable=True,
            ) from exc
        keys = payload.get("keys") if isinstance(payload, dict) else None
        if not isinstance(keys, list):
            raise DiagnosisError(code="INVALID_JWKS", message="OIDC JWKS 契约不合法", http_status=503)
        cache: dict[str, object] = {}
        for item in keys:
            if not isinstance(item, dict) or not item.get("kid"):
                continue
            if item.get("use") not in {None, "sig"}:
                continue
            if isinstance(item.get("key_ops"), list) and "verify" not in item["key_ops"]:
                continue
            if item.get("alg") not in {None, "RS256", "ES256", "EdDSA"}:
                continue
            try:
                cache[str(item["kid"])] = _public_key_from_jwk(item)
            except (ValueError, TypeError, KeyError):
                continue
        if not cache:
            raise DiagnosisError(code="INVALID_JWKS", message="OIDC JWKS 没有可用签名密钥", http_status=503)
        self._jwks_cache = cache
        self._jwks_expires_at = time.monotonic() + 300

    @staticmethod
    def _verify_signature(
        *, public_key: object, algorithm: str | None, signing_input: bytes, signature: bytes
    ) -> None:
        if algorithm == "RS256" and isinstance(public_key, rsa.RSAPublicKey):
            public_key.verify(signature, signing_input, padding.PKCS1v15(), hashes.SHA256())
            return
        if algorithm == "ES256" and isinstance(public_key, ec.EllipticCurvePublicKey):
            if len(signature) != 64:
                raise InvalidSignature
            der_signature = encode_dss_signature(
                int.from_bytes(signature[:32], "big"),
                int.from_bytes(signature[32:], "big"),
            )
            public_key.verify(der_signature, signing_input, ec.ECDSA(hashes.SHA256()))
            return
        if algorithm == "EdDSA" and isinstance(public_key, ed25519.Ed25519PublicKey):
            public_key.verify(signature, signing_input)
            return
        raise DiagnosisError(code="UNSUPPORTED_IDENTITY_ALGORITHM", message="身份令牌签名算法不受支持", http_status=401)

    def _validate_claims(self, claims: dict) -> None:
        now = datetime.now(UTC).timestamp()
        audience = claims.get("aud")
        audience_matches = self._audience in audience if isinstance(audience, list) else audience == self._audience
        if claims.get("iss", "").rstrip("/") != self._issuer or not audience_matches:
            raise DiagnosisError(
                code="INVALID_IDENTITY_AUDIENCE", message="身份令牌签发方或受众不匹配", http_status=401
            )
        leeway = self._clock_skew_seconds
        if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= now - leeway:
            raise DiagnosisError(code="IDENTITY_TOKEN_EXPIRED", message="身份令牌已过期", http_status=401)
        if isinstance(claims.get("nbf"), (int, float)) and claims["nbf"] > now + leeway:
            raise DiagnosisError(code="IDENTITY_TOKEN_NOT_YET_VALID", message="身份令牌尚未生效", http_status=401)
        if isinstance(claims.get("iat"), (int, float)) and claims["iat"] > now + leeway:
            raise DiagnosisError(code="IDENTITY_TOKEN_ISSUED_IN_FUTURE", message="身份令牌签发时间异常", http_status=401)
        if not claims.get("sub") or not claims.get("tenant_id") or not (claims.get("roles") or claims.get("role")):
            raise DiagnosisError(code="INVALID_IDENTITY_CLAIMS", message="身份令牌缺少必要 Claim", http_status=401)


class InternalCaseAuthorizer:
    """内部控制面工单授权器，仅允许平台级可信服务访问已存在工单。"""

    def __init__(self, session: AsyncSession):
        self._session = session

    async def assert_access(self, actor: ActorContext, case_id: str) -> None:
        """校验内部服务角色和工单存在性。"""

        elevated = actor.has_any_role("platform_admin", "support_engineer", "domain_expert", "diagnosis_worker")
        if elevated:
            statement = text('SELECT 1 FROM "case" WHERE case_id = :case_id LIMIT 1')
            params = {"case_id": case_id}
        else:
            customer_id = actor.customer_id or actor.tenant_id
            statement = text(
                """
                SELECT 1 FROM "case"
                WHERE case_id = :case_id AND customer_id::text = :customer_id
                LIMIT 1
                """
            )
            params = {"case_id": case_id, "customer_id": customer_id}
        result = await self._session.execute(statement, params)
        if result.scalar_one_or_none() is None:
            raise DiagnosisError(
                code="CASE_NOT_FOUND_OR_FORBIDDEN",
                message="关联工单不存在或当前身份无权访问",
                http_status=404,
            )


async def require_actor(request: Request) -> ActorContext:
    """解析可信身份；未配置验证器时默认拒绝业务请求。"""

    verifier: IdentityVerifier | None = getattr(request.app.state, "identity_verifier", None)
    if verifier is None:
        raise DiagnosisError(
            code="IDENTITY_PROVIDER_UNAVAILABLE",
            message="诊断服务身份提供方尚未配置",
            http_status=503,
            retryable=False,
        )
    return await verifier.verify(request)


def _base64url_decode(value: str) -> bytes:
    """解码无填充 Base64URL。"""

    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def _public_key_from_jwk(jwk: dict) -> object:
    """把受支持的 RSA、P-256 或 Ed25519 JWK 转为公钥。"""

    key_type = jwk.get("kty")
    if key_type == "RSA":
        exponent = int.from_bytes(_base64url_decode(str(jwk["e"])), "big")
        modulus = int.from_bytes(_base64url_decode(str(jwk["n"])), "big")
        return rsa.RSAPublicNumbers(exponent, modulus).public_key()
    if key_type == "EC" and jwk.get("crv") == "P-256":
        x = int.from_bytes(_base64url_decode(str(jwk["x"])), "big")
        y = int.from_bytes(_base64url_decode(str(jwk["y"])), "big")
        return ec.EllipticCurvePublicNumbers(x, y, ec.SECP256R1()).public_key()
    if key_type == "OKP" and jwk.get("crv") == "Ed25519":
        return ed25519.Ed25519PublicKey.from_public_bytes(_base64url_decode(str(jwk["x"])))
    raise ValueError("不支持的 JWK")
