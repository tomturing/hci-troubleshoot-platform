"""RS256 密钥管理 + JWKS 导出。

auth-service 持私钥签发 JWT；网关/下游服务用 JWKS 公钥验签，
auth-service 短暂不可用不影响已登录用户。
"""

from __future__ import annotations

import base64
import logging
import os

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

logger = logging.getLogger("auth-service.keys")

_KID = "hci-auth-rsa-1"
_PRIVATE_KEY: rsa.RSAPrivateKey | None = None


def _load_or_generate() -> rsa.RSAPrivateKey:
    """加载配置私钥；缺失时生成临时内存密钥（仅 dev/test，重启即失效）。"""
    global _PRIVATE_KEY
    if _PRIVATE_KEY is not None:
        return _PRIVATE_KEY
    pem = os.getenv("AUTH_RSA_PRIVATE_KEY_PEM", "").strip()
    if pem:
        _PRIVATE_KEY = serialization.load_pem_private_key(pem.encode(), password=None)
        return _PRIVATE_KEY
    logger.warning("AUTH_RSA_PRIVATE_KEY_PEM 未配置，生成临时内存 RSA 密钥（重启即失效，禁止用于生产）")
    _PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    return _PRIVATE_KEY


def get_private_key() -> rsa.RSAPrivateKey:
    return _load_or_generate()


def get_kid() -> str:
    return _KID


def _b64url_int(value: int) -> str:
    length = (value.bit_length() + 7) // 8
    raw = value.to_bytes(length, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def get_jwks() -> dict:
    """导出 JWKS（公钥，供网关/下游验签）。"""
    pub = get_private_key().public_key().public_numbers()
    return {
        "keys": [
            {
                "kty": "RSA",
                "kid": _KID,
                "use": "sig",
                "alg": "RS256",
                "n": _b64url_int(pub.n),
                "e": _b64url_int(pub.e),
            }
        ]
    }
