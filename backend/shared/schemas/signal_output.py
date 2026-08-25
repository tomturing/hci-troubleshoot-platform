"""关键信号输出契约的无副作用辅助函数。"""

from __future__ import annotations

import re
from typing import Any

_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)(?:\.[A-Z0-9_]+)*\}\}")


def derive_signal_requires(signal: dict[str, Any]) -> list[str]:
    """从 QFK 的采集参数和文本提取规则推导输入变量。

    qkv_vm_console 的 host/vm_id 参数同样引用 {{HOST}}/{{VM_ID}} 占位符；
    qkv_effect 的期望锚点（观测参数、matcher 阈值等）也可引用变量占位符；
    与显式 requires 合并扫描，兜底前端漏同步导致的变量依赖缺失。
    """

    acquire = signal.get("acquire") or {}
    tool = str(acquire.get("tool") or "")
    explicit_requires = list((signal.get("orchestrate") or {}).get("requires") or [])
    if not tool.startswith("qfk_") and tool not in {"qkv_vm_console", "qkv_effect"}:
        return explicit_requires

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
    if tool.startswith("qfk_"):
        # Matcher 的数值阈值同样可能引用上游变量，不能只扫描第一步取值配置。
        collect(signal.get("match") or {})
        for produce in ((signal.get("orchestrate") or {}).get("produces") or []):
            if isinstance(produce, dict):
                collect(produce.get("extract") or {})
        return sorted(set(values))
    if tool.startswith("qkv_"):
        orchestrate = signal.get("orchestrate") or {}
        local = {
            str(item.get(key) or "").strip().upper()
            for item in (orchestrate.get("produces") or [])
            if isinstance(item, dict)
            for key in ("name", "alias")
            if str(item.get(key) or "").strip()
        }
        derived: set[str] = set()
        for item in orchestrate.get("output_processing") or []:
            if not isinstance(item, dict):
                continue
            collect(item.get("input"))
            target = str(item.get("name") or item.get("target_variable") or "").strip().upper()
            if item.get("mode") == "derive" and target:
                derived.add(target)
        # QKV 后处理的 input 优先引用本信号的 produces/前序 derive，只有其余
        # 占位符才是外部依赖，避免把同一 Signal 内部流水线误报为上游输入。
        return sorted(set(values) - local - derived | set(explicit_requires))
    # qkv_vm_console / qkv_effect：显式 requires 与占位符扫描合并，
    # 保证 HOST/VM_ID 与期望锚点变量依赖不缺失。
    return sorted(set(explicit_requires) | set(values))


def sync_signal_requires(signal: dict[str, Any]) -> list[str]:
    """就地同步 ``orchestrate.requires`` 并返回推导结果。"""

    requires = derive_signal_requires(signal)
    tool = str((signal.get("acquire") or {}).get("tool") or "")
    if tool.startswith("qfk_") or tool.startswith("qkv_"):
        signal.setdefault("orchestrate", {})["requires"] = requires
    return requires
