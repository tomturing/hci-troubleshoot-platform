"""Compile a category KBD inventory into an immutable acquisition graph."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from typing import Any

from shared.schemas.acquirer_args import DEFAULT_SIGNAL_TIMEOUT_SECONDS, SUPPORTED_TOOLS, validate_acquire_args
from shared.schemas.signal_generation import current_tool_contract_revision

from app.adapters.agents.htp.kbd_model import KBD, _acquire_tool
from app.tools.qfk.handlers import build_acli_command
from app.tools.qfk.signal import BackendSignal

from .models import Acquisition, CaseVerificationPolicy, EvidenceRole, SignalPlan, SignalRef


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


def _verification_policy(kbd: KBD) -> CaseVerificationPolicy:
    contract = kbd.verification_contract or {}
    evidence = contract.get("evidence_policy") or {}

    def _ids(name: str) -> tuple[str, ...]:
        return tuple(str(item) for item in evidence.get(name) or [] if str(item).strip())

    try:
        minimum_should = max(0, int(evidence.get("minimum_should", 0)))
    except (TypeError, ValueError):
        minimum_should = 0
    return CaseVerificationPolicy(
        must=_ids("must"),
        should=_ids("should"),
        exclude=_ids("exclude"),
        context=_ids("context"),
        minimum_should=minimum_should,
        scope=dict(contract.get("scope") or {}),
        external_variables=(
            tuple(
                sorted(
                    str(name).strip().lower()
                    for name in (contract.get("variables") or {})
                    if str(name).strip()
                )
            )
            if contract
            else None
        ),
    )


def _evidence_role(signal_id: str, signal: dict[str, Any], policy: CaseVerificationPolicy) -> EvidenceRole:
    # 运行时消费持久化的 Verification Contract。专家保存/LLM 抽取时，该 Contract
    # 已由 signals[].role 单向投影生成；执行器不在现场反向改写知识。对于绕过新写入
    # 路径遗留的历史不一致文档，仍以已发布 Contract 为兼容性边界，避免运行时漂移。
    for role, ids in (
        (EvidenceRole.MUST, policy.must),
        (EvidenceRole.SHOULD, policy.should),
        (EvidenceRole.EXCLUDE, policy.exclude),
        (EvidenceRole.CONTEXT, policy.context),
    ):
        if signal_id in ids:
            return role
    explicit = str(signal.get("role") or "").lower()
    if explicit in {role.value for role in EvidenceRole}:
        return EvidenceRole(explicit)
    return EvidenceRole.MUST if _required(signal) else EvidenceRole.CONTEXT


def _compile_tool_contract(tool: str, signal: dict[str, Any]) -> str | None:
    """用保存契约和真实 QFK Handler 对同一条 acquisition 做发布前编译。"""

    if tool not in SUPPORTED_TOOLS:
        return None  # CDD 仍兼容注册表外的测试/扩展工具，生产发布门禁由上游控制。
    args = (signal.get("acquire") or {}).get("args") or {}
    ok, error = validate_acquire_args(tool, args)
    if not ok:
        return error
    if not tool.startswith("qfk_"):
        return None

    namespace = tool.removeprefix("qfk_")
    sample_values = {
        "PID": "1",
        "HOST": "127.0.0.1",
        "VM": "golden-vm",
        "DEVICE": "/dev/sda",
        "STORAGE_PATH": "/sf/data/golden",
        "END": "2026-07-30 10:00:00",
        "REQUEST_ID": "a5ed4ad9340ce338ba1ac71d13ffcfb9",
    }

    def resolve_sample(value: Any, field_name: str = "") -> Any:
        if isinstance(value, str):
            if field_name == "file" and re.fullmatch(r"\{\{[A-Z][A-Z0-9_]*\}\}", value):
                return "sample.log"
            return re.sub(
                r"\{\{([A-Z][A-Z0-9_]*)\}\}",
                lambda match: sample_values.get(match.group(1), "value"),
                value,
            )
        if isinstance(value, dict):
            return {key: resolve_sample(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve_sample(item) for item in value]
        return value

    compiled_args = resolve_sample(args)
    matcher = signal.get("match") or {}
    pattern = matcher.get("pattern") if matcher.get("type") == "keyword" else None
    keywords = [pattern] if isinstance(pattern, str) and pattern else list(pattern or []) if isinstance(pattern, list) else []
    data: dict[str, Any] = {
        "namespace": namespace,
        "host": compiled_args.get("host"),
        "timeout": compiled_args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS),
        "container": compiled_args.get("container"),
        "command": compiled_args.get("command"),
        "resource_keyword": compiled_args.get("resource_keyword"),
        "file": compiled_args.get("file"),
        "path": compiled_args.get("path"),
        "time_window": compiled_args.get("time_window"),
        "source_family": compiled_args.get("source_family", "auto"),
        "parser": compiled_args.get("parser"),
        "request_id": compiled_args.get("request_id"),
        "context_lines": compiled_args.get("context_lines", 0),
        "include_archives": compiled_args.get("include_archives", False),
        "archive_precheck": compiled_args.get("archive_precheck"),
        "matcher": matcher or None,
        "keyword": keywords,
        "match_mode": {"any": "or", "all": "and"}.get(
            str(matcher.get("mode") or "or"), str(matcher.get("mode") or "or")
        ),
        "expected": bool(matcher.get("expected", True)),
    }
    if namespace == "service":
        data["service"] = compiled_args.get("resource_keyword")
        data["action"] = compiled_args.get("command") or "status"
    try:
        build_acli_command(BackendSignal.model_validate(data))
    except Exception as exc:
        return f"runtime command compile failed: {exc}"
    return None


def _dependency_error(
    refs: list[SignalRef],
    declared_external: tuple[str, ...] | None,
) -> str | None:
    """检查变量依赖图、未声明外部变量和不可达生产者链。"""

    produced = {name for ref in refs for name in ref.produces}
    required = {name for ref in refs for name in ref.requires}
    if declared_external is None:
        # 无 Verification Contract 的历史数据继续兼容运行时 env_context。
        available = required - produced
    else:
        available = set(declared_external)
        undeclared = required - produced - available
        if undeclared:
            return f"undeclared external variables: {sorted(undeclared)}"
    remaining = list(refs)
    while remaining:
        ready = [ref for ref in remaining if set(ref.requires).issubset(available)]
        if not ready:
            detail = ", ".join(
                f"{ref.signal_id}(requires={list(ref.requires)}, produces={list(ref.produces)})"
                for ref in remaining
            )
            return f"variable dependency cycle or unreachable producer chain: {detail}"
        for ref in ready:
            available.update(ref.produces)
            remaining.remove(ref)
    return None


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
    verification_policies = {kbd.id: _verification_policy(kbd) for kbd in ordered}

    for kbd in ordered:
        revision = str((kbd.resource_revision or {}).get("revision") or "0")
        verification_policy = verification_policies[kbd.id]
        generation = kbd.generation_metadata or {}
        publish_validation = kbd.publish_validation or {}
        if kbd.verification_contract and publish_validation:
            if publish_validation.get("status") != "passed":
                errors.setdefault(kbd.id, []).append("expert publish validation status is invalid")
            if publish_validation.get("tool_contract_revision") != current_tool_contract_revision():
                errors.setdefault(kbd.id, []).append("expert publish tool contract revision is stale")
        elif kbd.verification_contract and generation:
            if generation.get("status") == "stale":
                errors.setdefault(kbd.id, []).append("signal generation metadata is stale")
            if generation.get("tool_contract_revision") != current_tool_contract_revision():
                errors.setdefault(kbd.id, []).append("signal tool contract revision is stale")
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
            contract_error = _compile_tool_contract(tool, signal)
            if contract_error:
                errors.setdefault(kbd.id, []).append(f"{signal_id}: {contract_error}")
                continue
            if phase == "solution":
                continue
            required = _required(signal)
            evidence_role = _evidence_role(signal_id, signal, verification_policy)
            required = evidence_role is EvidenceRole.MUST
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
                evidence_role=evidence_role,
                failure_effect=failure_effect,
                requires=requires,
                produces=produces,
                phase=phase,
                matcher_fingerprint=_fingerprint(signal.get("match")),
            )
            signals[ref_id] = ref
            acquire = signal.get("acquire") or {}
            args = acquire.get("args") or {}
            # instruction 是 SignalRef 上的人类可读说明，QKV/QFK Handler 不消费它。
            # 若把它纳入 acquisition identity，仅文案不同的同一次安全
            # 采集会被重复执行，既浪费又扩大现场风险。
            execution_args = {key: value for key, value in args.items() if key != "instruction"}
            template_material = {
                "tool": tool,
                "args": execution_args,
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
                    # identity 排除 instruction，但执行/报告模板保留第一条人类说明。
                    # 这样同一采集可去重，诊断报告也不会丢失可读步骤。
                    args_template=args,
                    cost=_number(signal, "cost", 1.0),
                    latency=_number(signal, "latency", 1.0),
                    risk=_number(signal, "risk", 1.0),
                ),
            )
            acquisition.signal_refs.append(ref)
            acquisition.requires.update(requires)
            acquisition.produces.update(produces)

        if not any(
            ref.kbd_id == kbd.id and ref.evidence_role is EvidenceRole.MUST
            for ref in signals.values()
        ):
            errors.setdefault(kbd.id, []).append("no executable required diagnostic signal")
        # Contract 引用完整文档 ID，而执行计划只包含 diagnostic ID。solution/context
        # 合法存在于文档但不会生成 SignalRef，不能因此被误报为“引用不存在”。
        known_ids = {
            str(signal.get("id") or signal.get("signal_id") or f"signal_{index:03d}")
            for index, signal in enumerate(kbd.signals, start=1)
        }
        referenced_ids = set(
            verification_policy.must
            + verification_policy.should
            + verification_policy.exclude
            + verification_policy.context
        )
        for missing_id in sorted(referenced_ids - known_ids):
            errors.setdefault(kbd.id, []).append(
                f"verification contract references missing signal: {missing_id}"
            )
        if verification_policy.minimum_should > len(verification_policy.should):
            errors.setdefault(kbd.id, []).append(
                "minimum_should exceeds declared should signal count"
            )
        dependency_error = _dependency_error(
            [ref for ref in signals.values() if ref.kbd_id == kbd.id],
            verification_policy.external_variables,
        )
        if dependency_error:
            errors.setdefault(kbd.id, []).append(dependency_error)

    return SignalPlan(
        plan_id=plan_id,
        category_id=category_id,
        snapshot_id=snapshot_id,
        candidates={kbd.id: kbd for kbd in ordered},
        signals=signals,
        acquisitions=acquisitions,
        verification_policies=verification_policies,
        compile_errors=errors,
    )
