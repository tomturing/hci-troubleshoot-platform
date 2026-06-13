"""动态资源序列化与哈希工具。"""

from __future__ import annotations

import hashlib
import json
from typing import Any


def normalize_json(value: Any) -> Any:
    """将对象归一化为可稳定 JSON 序列化的结构。"""
    if value is None or isinstance(value, str | int | float | bool):
        return value
    if isinstance(value, dict):
        return {str(key): normalize_json(val) for key, val in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, list | tuple | set):
        return [normalize_json(item) for item in value]
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return str(value)


def dumps_stable(value: Any) -> str:
    """稳定 JSON 序列化，供 checksum 和审计 hash 使用。"""
    return json.dumps(normalize_json(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def sha256_json(value: Any) -> str:
    """计算结构化对象的 SHA-256。"""
    return hashlib.sha256(dumps_stable(value).encode("utf-8")).hexdigest()


def resource_checksum(
    content: dict[str, Any],
    contract: dict[str, Any],
    dependencies: list[dict[str, Any]],
    *,
    version: str,
    status: str,
) -> str:
    """计算资源快照 checksum。"""
    return sha256_json(
        {
            "version": version,
            "status": status,
            "content": content,
            "contract": contract,
            "dependencies": dependencies,
        }
    )
