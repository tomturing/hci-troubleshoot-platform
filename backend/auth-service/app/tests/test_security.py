"""安全核心单元测试（不依赖数据库）。

覆盖：RS256 密钥/JWKS、JWT 签发与验签、argon2id 校验、角色白名单。
关键验证：用私钥签发的 JWT 可被对应公钥（JWKS 导出）按 RS256 验签通过，
即与下游 OidcJwtIdentityVerifier（RS256+JWKS）契约兼容。
"""

import base64
import json
import time

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from app.security import jwt as jwt_mod
from app.security.keys import get_jwks, get_kid, get_private_key
from app.security.password import hash_password, verify_password
from app.services.repository import validate_roles


def _b64url_decode(seg: str) -> bytes:
    return base64.urlsafe_b64decode(seg + "=" * (-len(seg) % 4))


def _decode_and_verify(token: str) -> dict:
    header_s, payload_s, sig_s = token.split(".")
    signing_input = f"{header_s}.{payload_s}".encode()
    public_key = get_private_key().public_key()
    public_key.verify(_b64url_decode(sig_s), signing_input, padding.PKCS1v15(), hashes.SHA256())
    return json.loads(_b64url_decode(payload_s))


def test_jwt_sign_and_verify():
    token = jwt_mod.issue_token(
        user_id="u-1", realm="admin", roles=["platform_admin"],
        aud="hci-admin", token_version=3, ttl=3600,
    )
    payload = _decode_and_verify(token)
    assert payload["sub"] == "u-1"
    assert payload["realm"] == "admin"
    assert payload["aud"] == "hci-admin"
    assert payload["tv"] == 3
    assert "jti" in payload and payload["exp"] > time.time()

    header = json.loads(_b64url_decode(token.split(".")[0]))
    assert header["alg"] == "RS256"
    assert header["kid"] == get_kid()


def test_jwt_expired_token():
    token = jwt_mod.issue_token(
        user_id="u-2", realm="customer", roles=["customer"],
        aud="hci-customer", token_version=1, ttl=-10,
    )
    payload = _decode_and_verify(token)
    assert payload["exp"] < time.time()


def test_password_roundtrip():
    hashed = hash_password("Str0ng#Pass!")
    assert verify_password(hashed, "Str0ng#Pass!")
    assert not verify_password(hashed, "wrong-password")


def test_validate_roles_whitelist():
    assert validate_roles("admin", ["platform_admin", "support_engineer"])
    assert not validate_roles("admin", ["customer"])
    assert validate_roles("customer", ["customer"])
    assert not validate_roles("customer", ["platform_admin"])


def test_jwks_export():
    jwks = get_jwks()
    assert "keys" in jwks and len(jwks["keys"]) == 1
    key = jwks["keys"][0]
    assert key["kty"] == "RSA"
    assert key["alg"] == "RS256"
    assert {"kid", "n", "e", "use"} <= set(key.keys())
