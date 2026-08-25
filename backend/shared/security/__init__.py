"""
Shared security utilities（服务间身份签名等）
"""

from shared.security.signature import (
    CLIENT_ID_HEADER,
    CLIENT_ID_PATTERN,
    CLOCK_SKEW_SECONDS,
    SIGNATURE_HEADER,
    sign_client_identity,
    verify_client_identity,
)

__all__ = [
    "CLIENT_ID_HEADER",
    "CLIENT_ID_PATTERN",
    "CLOCK_SKEW_SECONDS",
    "SIGNATURE_HEADER",
    "sign_client_identity",
    "verify_client_identity",
]
