"""关键信号输出契约的无副作用辅助函数。"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)(?:\.[A-Z0-9_]+)*\}\}")


def derive_signal_requires(signal: dict[str, Any]) -> list[str]:
    """从 QFK 的采集参数和文本提取规则推导输入变量。"""

    acquire = signal.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    if not tool.startswith("qfk_"):
        return list((signal.get("orchestrate") or {}).get("requires") or [])

    values: list[str] = []

    def collect(value: Any) -> None:
        if isinstance(value, str):
            values.extend(_PLACEHOLDER_RE.findall(value))
        elif isinstance(value, dict):
            for item in value.values():
                collect(item)
        elif isinstance(value, list):
            for item in value:
                collect(item)

    collect(acquire.get("args") or {})
    collect((signal.get("match") or {}).get("extract") or {})
    for produce in ((signal.get("orchestrate") or {}).get("produces") or []):
        if isinstance(produce, dict):
            collect(produce.get("extract") or {})
    return sorted(set(values))


def sync_signal_requires(signal: dict[str, Any]) -> list[str]:
    """就地同步 ``orchestrate.requires`` 并返回推导结果。"""

    requires = derive_signal_requires(signal)
    acquire = signal.get("acquire") or {}
    if str(acquire.get("tool") or "").startswith("qfk_"):
        signal.setdefault("orchestrate", {})["requires"] = requires
    return requires
