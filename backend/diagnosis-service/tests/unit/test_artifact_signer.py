"""Ed25519 Artifact 签名器测试。"""

import base64

import pytest
from app.errors import DiagnosisError
from app.services.artifact_signer import Ed25519ArtifactSigner
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey


def test_ed25519_signature_can_be_verified():
    """签名结果可由对应公钥验证。"""

    private_key = Ed25519PrivateKey.generate()
    private_bytes = private_key.private_bytes_raw()
    signer = Ed25519ArtifactSigner(
        private_key_base64=base64.b64encode(private_bytes).decode("ascii"),
        key_id="collector-key-2026-01",
    )
    content = b"#!/bin/sh\necho safe\n"

    result = signer.sign(content)
    identity = signer.public_identity()

    private_key.public_key().verify(base64.b64decode(result.signature_base64), content)
    assert result.algorithm == "Ed25519"
    assert result.key_id == "collector-key-2026-01"
    assert base64.b64decode(result.public_key_base64) == private_key.public_key().public_bytes_raw()
    assert len(result.public_key_fingerprint) == 64
    assert identity.key_id == result.key_id
    assert identity.public_key_base64 == result.public_key_base64
    assert identity.public_key_fingerprint == result.public_key_fingerprint


def test_missing_signing_key_is_default_deny():
    """未配置私钥时不得弱签名降级。"""

    with pytest.raises(DiagnosisError) as exc_info:
        Ed25519ArtifactSigner(private_key_base64="", key_id="")

    assert exc_info.value.code == "ARTIFACT_SIGNER_UNAVAILABLE"
