"""Deterministic provenance fingerprints for generated KBD signal contracts."""

from __future__ import annotations

import hashlib
import json
from functools import lru_cache
from pathlib import Path
from typing import Any

_SIGNALS_DIR = Path(__file__).resolve().parent / "signals"


def _fingerprint(value: Any) -> str:
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
    """Hash every generated Signal/QKV/QFK schema, not only the root `$ref` file."""
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
