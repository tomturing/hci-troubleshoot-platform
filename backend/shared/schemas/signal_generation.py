"""Deterministic provenance fingerprints for generated KBD signal contracts.

契约门禁根治（见 docs/solution/events/2026-08-13-kbd-contract-gate-structural-semantic-fingerprint-adr.md）：

最终采用「校验器驱动」方案（第一性原理最优解）：

- ``tool_contract_revision``：字节哈希全量变化探测（粗粒度，已有字段，向后兼容）。
- 门禁唯一真相源：``validate_publishable_signals_json(old_signals, current_schema)``。
  pass → 兼容演进（soft_stale 观测，不阻断）；
  fail → 破坏性变更（hard_break 阻断）。
- 省略两级结构/语义指纹：校验器本身覆盖所有约束关键词（含 minimum/pattern/format 等），
  无需手工枚举关键词子集，不存在盲区；校验器结果在 ``_execution_issues`` 顶层跑一次后
  全程复用，消除双重校验（REDUNDANCY-1）和错误信息重复问题。
"""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_SIGNALS_DIR = Path(__file__).resolve().parent / "signals"


def _fingerprint(value: Any) -> str:
    """对任意可 JSON 序列化的值生成确定性 SHA-256 十六进制摘要。"""
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


@lru_cache(maxsize=1)
def current_tool_contract_revision() -> str:
    """对所有 Signal/QKV/QFK schema 文件内容做聚合哈希，作为「是否发生任何变动」的粗粒度探测。

    向后兼容旧 KBD 快照的 ``publish_validation.tool_contract_revision`` 比对。
    哈希变化表示至少有一个 schema 文件被修改；具体是否 breaking 由门禁实跑校验器决定。
    """
    material = [
        {
            "path": path.relative_to(_SIGNALS_DIR).as_posix(),
            "sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
        }
        for path in sorted(_SIGNALS_DIR.rglob("*.schema.json"))
    ]
    return _fingerprint(material)


def build_signal_generation_metadata(
    *,
    source: dict[str, Any],
    prompt_template: str,
    model_id: str,
) -> dict[str, Any]:
    """Build the immutable inputs needed to decide whether Signal/Contract is stale."""
    source_fingerprint = _fingerprint(source)
    prompt_revision = hashlib.sha256(prompt_template.encode()).hexdigest()
    tool_contract_revision = current_tool_contract_revision()
    generation_fingerprint = _fingerprint(
        {
            "source_fingerprint": source_fingerprint,
            "prompt_revision": prompt_revision,
            "model_id": model_id,
            "tool_contract_revision": tool_contract_revision,
        }
    )
    return {
        "schema_version": 1,
        "status": "current",
        "source_fingerprint": source_fingerprint,
        "prompt_revision": prompt_revision,
        "model_id": model_id,
        "tool_contract_revision": tool_contract_revision,
        "generation_fingerprint": generation_fingerprint,
    }


def staleness_reasons(
    stored: dict[str, Any] | None,
    current: dict[str, Any],
) -> list[str]:
    """Return closed, machine-readable reasons; an absent legacy record is untracked."""
    if not stored:
        return ["generation_metadata_missing"]
    reasons: list[str] = []
    if stored.get("status") == "stale":
        reasons.append("explicitly_marked_stale")
    for field in (
        "source_fingerprint",
        "prompt_revision",
        "model_id",
        "tool_contract_revision",
    ):
        if stored.get(field) != current.get(field):
            reasons.append(f"{field}_changed")
    return reasons
