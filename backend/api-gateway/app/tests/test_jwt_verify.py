"""网关 JwtVerifier 单元测试（阶段1）。

验证 auth-service 签发的 RS256 JWT 在网关侧的验签链路：成功路径、受众/签发方
不匹配、过期、缺角色、签名篡改、kid 缺失/未知。公钥通过 fixture 预注入，避免
真实网络拉取 JWKS（与 auth-service 的 JWKS 导出契约一致：kid=hci-auth-rsa-1）。
"""

import asyncio
import base64
import json
import time

import pytest
from app.security.jwt_verify import GatewayActor, JwtVerifier
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives.asymmetric.padding import PKCS1v15


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


@pytest.fixture
def rsa_key():
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


@pytest.fixture
def verifier(rsa_key):
    v = JwtVerifier(
        jwks_url="http://auth-service:8007/.well-known/jwks.json",
        issuer="hci-auth-service",
        audience="hci-admin",
    )
    # 预注入公钥（绕过网络拉取 JWKS），保持单元测试自包含
    v._cache = {"hci-auth-rsa-1": rsa_key.public_key()}
    v._cache_expires_at = time.monotonic() + 300
    return v


def _sign(key, kid: str, claims: dict) -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(claims, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode()
    sig = key.sign(signing_input, PKCS1v15(), hashes.SHA256())
    segments.append(_b64url(sig))
    return ".".join(segments)


def _admin_claims(**overrides):
    now = int(time.time())
    claims = {
        "iss": "hci-auth-service",
        "sub": "u-001",
        "realm": "admin",
        "roles": ["platform_admin", "support_engineer"],
        "aud": "hci-admin",
        "iat": now,
        "exp": now + 3600,
        "jti": "abc",
        "tv": 1,
    }
    claims.update(overrides)
    return claims


def test_verify_admin_ok(verifier, rsa_key):
    token = _sign(rsa_key, "hci-auth-rsa-1", _admin_claims())
    actor = asyncio.run(verifier.verify_token(token))
    assert isinstance(actor, GatewayActor)
    assert actor.user_id == "u-001"
    assert actor.realm == "admin"
    assert actor.roles == frozenset({"platform_admin", "support_engineer"})
    assert actor.audience == "hci-admin"


def test_verify_aud_mismatch(verifier, rsa_key):
    token = _sign(rsa_key, "hci-auth-rsa-1", _admin_claims(aud="hci-customer"))
    with pytest.raises(ValueError):
        asyncio.run(verifier.verify_token(token))


def test_verify_iss_mismatch(verifier, rsa_key):
    token = _sign(rsa_key, "hci-auth-rsa-1", _admin_claims(iss="other-issuer"))
    with pytest.raises(ValueError):
        asyncio.run(verifier.verify_token(token))


def test_verify_expired(verifier, rsa_key):
    token = _sign(rsa_key, "hci-auth-rsa-1", _admin_claims(exp=int(time.time()) - 120))
    with pytest.raises(ValueError):
        asyncio.run(verifier.verify_token(token))


def test_verify_missing_roles(verifier, rsa_key):
    token = _sign(rsa_key, "hci-auth-rsa-1", _admin_claims(roles=[]))
    with pytest.raises(ValueError):
        asyncio.run(verifier.verify_token(token))


def test_verify_bad_signature(verifier, rsa_key):
    token = _sign(rsa_key, "hci-auth-rsa-1", _admin_claims())
    header_seg, payload_seg, _ = token.split(".")
    bad = f"{header_seg}.{payload_seg}.AAAA"
    with pytest.raises(ValueError):
        asyncio.run(verifier.verify_token(bad))


def test_verify_unknown_kid(verifier, rsa_key):
    token = _sign(rsa_key, "unknown-kid", _admin_claims())
    with pytest.raises(ValueError):
        asyncio.run(verifier.verify_token(token))


def test_verify_missing_kid(verifier, rsa_key):
    header = {"alg": "RS256", "typ": "JWT"}
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(_admin_claims(), separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode()
    sig = rsa_key.sign(signing_input, PKCS1v15(), hashes.SHA256())
    segments.append(_b64url(sig))
    token = ".".join(segments)
    with pytest.raises(ValueError):
        asyncio.run(verifier.verify_token(token))
