"""JWT 签发（RS256）。

Claims 设计（与下游 OidcJwtIdentityVerifier 契约对齐）：
- iss / sub / realm / roles / aud / iat / exp / jti 为 JWT 标准字段
- `tv`：token_version，改角色/停用/改密时 +1 → 已签发令牌立即失效（见方案 §3.8）
"""

from __future__ import annotations

import base64
import json
import secrets
import time
from typing import Iterable

from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import padding

from app.config import settings
from app.security.keys import get_kid, get_private_key


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def issue_token(
    *,
    user_id: str,
    realm: str,
    roles: Iterable[str],
    aud: str,
    token_version: int,
    ttl: int,
) -> str:
    """签发 RS256 JWT，返回紧凑序列化字符串。"""
    header = {"alg": "RS256", "typ": "JWT", "kid": get_kid()}
    now = int(time.time())
    payload = {
        "iss": settings.JWT_ISSUER,
        "sub": user_id,
        "realm": realm,
        "roles": list(roles),
        "aud": aud,
        "iat": now,
        "exp": now + ttl,
        "jti": secrets.token_urlsafe(16),
        "tv": token_version,
    }
    segments = [
        _b64url(json.dumps(header, separators=(",", ":")).encode()),
        _b64url(json.dumps(payload, separators=(",", ":")).encode()),
    ]
    signing_input = ".".join(segments).encode()
    signature = get_private_key().sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    segments.append(_b64url(signature))
    return ".".join(segments)
