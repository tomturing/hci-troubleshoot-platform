"""Compile a category KBD inventory into an immutable acquisition graph."""

from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

from app.adapters.agents.htp.kbd_model import KBD, _acquire_tool

from .models import Acquisition, SignalPlan, SignalRef


def _canonical(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _fingerprint(value: Any) -> str:
    return hashlib.sha256(_canonical(value).encode()).hexdigest()


def _required(signal: dict[str, Any]) -> bool:
    review = signal.get("review") or {}
    orchestrate = signal.get("orchestrate") or {}
    return bool(
        signal.get(
            "required_for_support",
            signal.get(
                "required_for_confirmation",
                review.get(
                    "required_for_support",
                    review.get("required_for_confirmation", orchestrate.get("phase", "diagnostic") == "diagnostic"),
                ),
            ),
        )
    )


def _names(items: Any) -> tuple[str, ...]:
    names: list[str] = []
    for item in items or []:
        name = item.get("name") if isinstance(item, dict) else item
        if name:
            names.append(str(name).strip().lower())
    return tuple(sorted(set(names)))


def _number(signal: dict[str, Any], name: str, default: float) -> float:
    policy = signal.get("policy") or signal.get("scheduling") or {}
    try:
        return max(0.0, float(policy.get(name, default)))
    except (TypeError, ValueError):
        return default


def compile_signal_plan(
    candidates: list[KBD],
    *,
    snapshot_id: str = "runtime",
    policy_version: str = "cdd-v1",
) -> SignalPlan:
    ordered = sorted(candidates, key=lambda item: (item.support_id or item.id, item.id))
    category_id = ordered[0].category_id if ordered else ""
    material = {
        "snapshot_id": snapshot_id,
        "policy_version": policy_version,
        "candidates": [
            [kbd.id, str((kbd.resource_revision or {}).get("revision") or "0")] for kbd in ordered
        ],
    }
    plan_id = str(uuid.uuid5(uuid.NAMESPACE_URL, _canonical(material)))
    signals: dict[str, SignalRef] = {}
    acquisitions: dict[str, Acquisition] = {}
    errors: dict[str, list[str]] = {}

    for kbd in ordered:
        revision = str((kbd.resource_revision or {}).get("revision") or "0")
        seen_signal_ids: set[str] = set()
        for index, signal in enumerate(kbd.signals, start=1):
            signal_id = str(signal.get("id") or signal.get("signal_id") or f"signal_{index:03d}")
            if signal_id in seen_signal_ids:
                errors.setdefault(kbd.id, []).append(f"duplicate signal_id: {signal_id}")
                continue
            seen_signal_ids.add(signal_id)
            tool = _acquire_tool(signal)
            phase = str((signal.get("orchestrate") or {}).get("phase") or "diagnostic")
            if not tool:
                errors.setdefault(kbd.id, []).append(f"{signal_id}: missing acquire.tool")
                continue
            if phase == "solution":
                continue
            required = _required(signal)
            failure_effect = str(signal.get("failure_effect") or ("reject" if required else "no_support"))
            orchestrate = signal.get("orchestrate") or {}
            requires = _names(orchestrate.get("requires") or signal.get("requires"))
            produces = _names(orchestrate.get("produces") or signal.get("produces"))
            ref_id = f"{kbd.id}/{revision}/{signal_id}"
            ref = SignalRef(
                ref_id=ref_id,
                kbd_id=kbd.id,
                support_id=kbd.support_id,
                revision=revision,
                signal_id=signal_id,
                signal=signal,
                required_for_support=required,
                failure_effect=failure_effect,
                requires=requires,
                produces=produces,
                phase=phase,
                matcher_fingerprint=_fingerprint(signal.get("match")),
            )
            signals[ref_id] = ref
            acquire = signal.get("acquire") or {}
            args = acquire.get("args") or {}
            template_material = {
                "tool": tool,
                "args": args,
                "policy_version": policy_version,
            }
            # qfk_log pushes matcher keywords into the collection command, so matcher
            # changes the acquisition itself until QFK gains a matcher-free log fetch.
            if tool == "qfk_log":
                template_material["execution_matcher"] = signal.get("match")
            if tool.startswith("qkv_"):
                template_material["producer_contract"] = orchestrate.get("produces") or signal.get("produces")
            template_key = _fingerprint(template_material)
            acquisition = acquisitions.setdefault(
                template_key,
                Acquisition(
                    template_key=template_key,
                    tool_name=tool,
                    args_template=args,
                    cost=_number(signal, "cost", 1.0),
                    latency=_number(signal, "latency", 1.0),
                    risk=_number(signal, "risk", 1.0),
                ),
            )
            acquisition.signal_refs.append(ref)
            acquisition.requires.update(requires)
            acquisition.produces.update(produces)

        if not any(ref.kbd_id == kbd.id and ref.required_for_support for ref in signals.values()):
            errors.setdefault(kbd.id, []).append("no executable required diagnostic signal")

    return SignalPlan(
        plan_id=plan_id,
        category_id=category_id,
        snapshot_id=snapshot_id,
        candidates={kbd.id: kbd for kbd in ordered},
        signals=signals,
        acquisitions=acquisitions,
        compile_errors=errors,
    )
