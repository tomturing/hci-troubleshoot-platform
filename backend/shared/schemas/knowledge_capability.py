"""知识可推荐与现场可验证的能力边界，不为纯文本案例伪造 Signal。"""

from typing import Any


def is_reference_only(raw: Any) -> bool:
    """仅识别干净的空信号文档；抽取异常、拒绝候选和孤儿画像不能自动降级。"""
    if raw is None or raw == [] or raw == {}:
        return True
    if not isinstance(raw, dict) or raw.get("signals") != [] or raw.get("semantic_entry_profile") is not None:
        return False
    metadata = raw.get("generation_metadata") or {}
    return (
        isinstance(metadata, dict)
        and metadata.get("status") not in {"stale", "failed", "error", "running", "pending"}
        and not raw.get("rejected_candidates")
    )
