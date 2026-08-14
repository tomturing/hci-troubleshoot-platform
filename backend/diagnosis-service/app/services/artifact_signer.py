"""Collector Artifact Ed25519 分离式签名器。"""

import base64
import binascii
import hashlib
from dataclasses import dataclass
from typing import Protocol

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

from app.errors import DiagnosisError


@dataclass(frozen=True, slots=True)
class SignatureResult:
    """分离式签名结果。"""

    algorithm: str
    key_id: str
    signature_base64: str
    public_key_base64: str
    public_key_fingerprint: str


@dataclass(frozen=True, slots=True)
class PublicKeyIdentity:
    """制品签名公钥身份。"""

    algorithm: str
    key_id: str
    public_key_base64: str
    public_key_fingerprint: str


class ArtifactSigner(Protocol):
    """制品签名器协议。"""

    def sign(self, content: bytes) -> SignatureResult:
        """对制品原始字节签名。"""

    def public_identity(self) -> PublicKeyIdentity:
        """返回当前签名公钥身份。"""


class Ed25519ArtifactSigner:
    """从 Base64 原始私钥加载的 Ed25519 签名器。"""

    def __init__(self, *, private_key_base64: str, key_id: str):
        if not private_key_base64.strip() or not key_id.strip():
            raise DiagnosisError(
                code="ARTIFACT_SIGNER_UNAVAILABLE",
                message="Collector Artifact 签名密钥尚未配置",
                http_status=503,
            )
        try:
            private_bytes = base64.b64decode(private_key_base64, validate=True)
            self._private_key = Ed25519PrivateKey.from_private_bytes(private_bytes)
        except (binascii.Error, ValueError, TypeError) as exc:
            raise DiagnosisError(
                code="INVALID_ARTIFACT_SIGNING_KEY",
                message="Collector Artifact Ed25519 私钥格式不合法",
                http_status=503,
            ) from exc
        self._key_id = key_id.strip()

    def public_identity(self) -> PublicKeyIdentity:
        """返回当前 Ed25519 公钥和稳定指纹。"""

        public_bytes = self._private_key.public_key().public_bytes(
            encoding=serialization.Encoding.Raw,
            format=serialization.PublicFormat.Raw,
        )
        return PublicKeyIdentity(
            algorithm="Ed25519",
            key_id=self._key_id,
            public_key_base64=base64.b64encode(public_bytes).decode("ascii"),
            public_key_fingerprint=hashlib.sha256(public_bytes).hexdigest(),
        )

    def sign(self, content: bytes) -> SignatureResult:
        """生成 Ed25519 分离式签名和公钥指纹。"""

        identity = self.public_identity()
        return SignatureResult(
            algorithm=identity.algorithm,
            key_id=identity.key_id,
            signature_base64=base64.b64encode(self._private_key.sign(content)).decode("ascii"),
            public_key_base64=identity.public_key_base64,
            public_key_fingerprint=identity.public_key_fingerprint,
        )
