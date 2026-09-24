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


def _resolve_pem() -> str:
    """解析签名私钥 PEM。

    优先级：
      1. ``AUTH_RSA_PRIVATE_KEY_PEM``：原始多行 PEM（本地/测试直接注入）。
      2. ``AUTH_RSA_PRIVATE_KEY_PEM_B64``：Base64(PEM) 单行（K8s Secret 注入首选，
         避免多行 PEM 在 values.yaml 三方合并时被截断——见 env-repo 历史坑）。
    """
    pem = os.getenv("AUTH_RSA_PRIVATE_KEY_PEM", "").strip()
    if pem:
        return pem
    b64 = os.getenv("AUTH_RSA_PRIVATE_KEY_PEM_B64", "").strip()
    if b64:
        try:
            return base64.b64decode(b64, validate=True).decode("utf-8").strip()
        except Exception as exc:  # noqa: BLE001 - 启动期配置错误需明确暴露
            raise RuntimeError(f"AUTH_RSA_PRIVATE_KEY_PEM_B64 解码失败：{exc}") from exc
    return ""


def _load_or_generate() -> rsa.RSAPrivateKey:
    """加载持久私钥；缺失时按开关决定 fail-fast 还是生成临时密钥（仅 dev/test）。

    第一性原理：JWT 认证的可用地基是"签名私钥跨重启稳定"。临时内存密钥会导致
    auth-service 每次重启 → JWKS 公钥变更 → 所有已签发 JWT 验签失败 → 全员掉线。
    因此 staging/prod 必须注入固定私钥，并以 ``AUTH_REQUIRE_PERSISTENT_KEY=true``
    强制校验：缺私钥即拒绝启动（fail-fast），杜绝"静默退化为临时密钥"。
    """
    global _PRIVATE_KEY
    if _PRIVATE_KEY is not None:
        return _PRIVATE_KEY
    pem = _resolve_pem()
    if pem:
        _PRIVATE_KEY = serialization.load_pem_private_key(pem.encode(), password=None)
        logger.info("已加载持久化 RSA 签名私钥（kid=%s），JWKS 跨重启稳定", _KID)
        return _PRIVATE_KEY
    # 无私钥：require_persistent 为真则拒绝启动，否则退化为临时密钥（仅 dev/test）。
    require_persistent = os.getenv("AUTH_REQUIRE_PERSISTENT_KEY", "").strip().lower() in ("1", "true", "yes", "on")
    if require_persistent:
        raise RuntimeError(
            "AUTH_REQUIRE_PERSISTENT_KEY=true 但未配置 AUTH_RSA_PRIVATE_KEY_PEM(_B64)，拒绝启动："
            "临时密钥会导致重启后 JWKS 变更、所有已签发 JWT 失效、全员掉线。"
            "请由 K8s Secret 注入固定私钥（secrets.authRsaPrivateKeyPemB64）。"
        )
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
