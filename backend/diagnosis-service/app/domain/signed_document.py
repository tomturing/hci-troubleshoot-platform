"""离线信任链签名文档公共函数。"""

import json
from typing import Any

from app.services.artifact_signer import ArtifactSigner


def canonical_json_bytes(document: dict[str, Any]) -> bytes:
    """使用稳定 JSON 编码生成签名原文。"""

    return json.dumps(
        document,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def attach_detached_signature(document: dict[str, Any], signer: ArtifactSigner) -> dict[str, Any]:
    """复制文档并附加覆盖其余字段的 Ed25519 分离式签名。"""

    signed_document = dict(document)
    signature = signer.sign(canonical_json_bytes(signed_document))
    signed_document["document_signature"] = {
        "algorithm": signature.algorithm,
        "key_id": signature.key_id,
        "signature_base64": signature.signature_base64,
        "public_key_fingerprint": signature.public_key_fingerprint,
    }
    return signed_document
