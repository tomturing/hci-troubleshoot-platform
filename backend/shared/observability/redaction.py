"""可观测性数据脱敏工具。"""

from __future__ import annotations

import re
from typing import Any

_SENSITIVE_KEY_RE = re.compile(
    r"(?i)(password|passwd|passphrase|private[_-]?key|authorization|token|secret|api[_-]?key)"
)
_INLINE_SECRET_RE = re.compile(
    r"(?i)(password|passwd|passphrase|private[_-]?key|authorization|token|secret|api[_-]?key)"
    r"(\s*[=:]\s*|\s+)([^\s'\"]+|['\"][^'\"]*['\"])"
)


def redact_observation_value(value: Any, key: str = "") -> Any:
    """递归脱敏准备写入日志、审计或 Langfuse 的输入值。"""
    if key and _SENSITIVE_KEY_RE.search(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(child_key): redact_observation_value(child_value, str(child_key)) for child_key, child_value in value.items()}
    if isinstance(value, list):
        return [redact_observation_value(item) for item in value]
    if isinstance(value, tuple):
        return tuple(redact_observation_value(item) for item in value)
    if isinstance(value, str):
        return _INLINE_SECRET_RE.sub(lambda match: f"{match.group(1)}{match.group(2)}[REDACTED]", value)
    return value
