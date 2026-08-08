"""统一关键信号审查内核。

所有生产、专家审核、发布和离线审计入口都必须先经过本模块。这里不复制各领域
Resolver 的规则，而是把 ``signals_json`` 转换为 ``SignalIntent``，直接调用
``SharedResolutionRuntime.compile/resolve``。调用方可以在统一结果之上增加自己的
特有规则，但不能绕过或弱化这里的运行时审查。

离线/发布阶段通常没有现场路径探针，因此 ``needs_probe`` 表示模板已经通过运行时
编译、仍需 Agent 在具体环境中完成探测；它不是静态错误。真正执行时可设置
``require_verified=True``，把未验证结果升级为阻断。
"""

from __future__ import annotations

import json
from enum import StrEnum
from typing import Any

from jsonschema import ValidationError
from pydantic import BaseModel, ConfigDict, Field
from shared.resolution.models import ResolutionStatus, SignalIntent
from shared.resolution.runtime import SharedResolutionRuntime, get_resolution_runtime
from shared.schemas.acquirer_args import validate_acquire_args
from shared.schemas.signal_schema import validate_signals_json


class SignalReviewFeature(StrEnum):
    """统一审查内核的调用场景；场景只影响策略和可观测性，不改变底层规则。"""

    LLM_GENERATION = "llm_generation"
    PIPELINE = "pipeline"
    EXPERT = "expert"
    PUBLISH = "publish"
    AGENT_EXECUTION = "agent_execution"


class SignalReviewStatus(StrEnum):
    PASSED = "passed"
    NEEDS_REVIEW = "needs_review"
    BLOCKED = "blocked"
    EMPTY = "empty"


class SignalReviewIssue(BaseModel):
    model_config = ConfigDict(frozen=True)

    code: str
    message: str
    level: str = "error"
    source: str = "shared_resolution_runtime"
    signal_id: str | None = None
    signal_index: int | None = None
    field: str | None = None


class SignalRuntimeReview(BaseModel):
    model_config = ConfigDict(frozen=True)

    signal_id: str
    signal_index: int
    tool: str | None = None
    resolver_id: str | None = None
    status: ResolutionStatus
    catalog_version: str = "unknown"
    command: str | None = None
    candidates_tried: list[str] = Field(default_factory=list)
    issues: list[SignalReviewIssue] = Field(default_factory=list)


class SignalReviewResult(BaseModel):
    model_config = ConfigDict(frozen=True)

    feature: SignalReviewFeature
    status: SignalReviewStatus
    signal_count: int
    runtime_status_counts: dict[str, int] = Field(default_factory=dict)
    signals: list[SignalRuntimeReview] = Field(default_factory=list)
    issues: list[SignalReviewIssue] = Field(default_factory=list)

    @property
    def blocked(self) -> bool:
        return self.status is SignalReviewStatus.BLOCKED


def _signals_document(raw: Any) -> dict[str, Any]:
    if isinstance(raw, str):
        raw = json.loads(raw)
    if isinstance(raw, list):
        return {"schema_version": 2, "signals": raw}
    return raw if isinstance(raw, dict) else {}


def _resolver_id(tool: str) -> str | None:
    if tool.startswith("qkv_"):
        return "qkv"
    if tool == "qfk_log":
        return "log"
    if tool == "qfk_system":
        return "system"
    if tool == "qfk_service":
        return "service"
    if tool in {"qfk_vm", "qfk_network", "qfk_storage", "qfk_hardware", "qfk_platform"}:
        return "domain"
    return None


def _intent(tool: str, args: dict[str, Any], signal: dict[str, Any]) -> SignalIntent:
    resolver_id = _resolver_id(tool)
    if resolver_id is None:
        raise ValueError(f"acquire.tool 没有 Shared Resolution Runtime Resolver: {tool}")
    canonical_args = dict(args)
    if resolver_id == "qkv":
        canonical_args["query"] = tool.removeprefix("qkv_")
    elif resolver_id == "domain":
        canonical_args["domain"] = tool.removeprefix("qfk_")
    provenance = signal.get("provenance") if isinstance(signal.get("provenance"), dict) else {}
    evidence = str(provenance.get("evidence") or "").strip()
    return SignalIntent(
        resolver_id=resolver_id,
        tool=tool,
        args=canonical_args,
        evidence=[evidence] if evidence else [],
        source=str(provenance.get("source_section") or "") or None,
    )


def _review_issue(
    code: str,
    message: str,
    *,
    signal_id: str | None = None,
    signal_index: int | None = None,
    field: str | None = None,
    level: str = "error",
    source: str = "shared_resolution_runtime",
) -> SignalReviewIssue:
    return SignalReviewIssue(
        code=code,
        message=message,
        signal_id=signal_id,
        signal_index=signal_index,
        field=field,
        level=level,
        source=source,
    )


def review_signal_document(
    raw: Any,
    *,
    feature: SignalReviewFeature | str,
    context: dict[str, Any] | None = None,
    require_verified: bool = False,
    runtime: SharedResolutionRuntime | None = None,
) -> SignalReviewResult:
    """使用 Agent 的 Shared Resolution Runtime 审查整份 Signal 文档。

    ``require_verified=False`` 用于不具备现场探针的生产、专家、发布和离线审查：
    ``needs_probe`` 会明确保留为待运行时确认，而不会伪装成通过。
    Agent 真正执行前应使用 ``require_verified=True``。
    """

    feature = SignalReviewFeature(feature)
    runtime = runtime or get_resolution_runtime()
    issues: list[SignalReviewIssue] = []
    signal_reviews: list[SignalRuntimeReview] = []
    try:
        document = _signals_document(raw)
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        return SignalReviewResult(
            feature=feature,
            status=SignalReviewStatus.BLOCKED,
            signal_count=0,
            issues=[_review_issue("SIGNAL_DOCUMENT_INVALID", f"signals_json 无法解析: {exc}")],
        )

    signals = document.get("signals") if isinstance(document.get("signals"), list) else []
    if not signals:
        return SignalReviewResult(
            feature=feature,
            status=SignalReviewStatus.EMPTY,
            signal_count=0,
        )

    try:
        validate_signals_json(document)
    except ValidationError as exc:
        path = list(exc.absolute_path)
        signal_index = path[1] if len(path) >= 2 and path[0] == "signals" and isinstance(path[1], int) else None
        signal = (
            signals[signal_index]
            if signal_index is not None and signal_index < len(signals) and isinstance(signals[signal_index], dict)
            else None
        )
        field = ".".join(str(part) for part in path[2:]) if signal_index is not None else None
        issues.append(
            _review_issue(
                "SIGNAL_SCHEMA_INVALID",
                str(exc.message or exc),
                source="shared_signal_schema",
                signal_id=str(signal.get("id") or f"signals[{signal_index}]") if signal is not None else None,
                signal_index=signal_index,
                field=field or None,
            )
        )
        schema_invalid_indexes = {signal_index} if signal_index is not None else set(range(len(signals)))
    else:
        schema_invalid_indexes = set()

    for index, signal in enumerate(signals):
        signal_id = (
            str(signal.get("id") or f"signals[{index}]")
            if isinstance(signal, dict)
            else f"signals[{index}]"
        )
        per_signal: list[SignalReviewIssue] = []
        if not isinstance(signal, dict):
            issue = _review_issue(
                "SIGNAL_INVALID",
                "Signal 必须是对象",
                signal_id=signal_id,
                signal_index=index,
            )
            issues.append(issue)
            signal_reviews.append(
                SignalRuntimeReview(
                    signal_id=signal_id,
                    signal_index=index,
                    status=ResolutionStatus.BLOCKED,
                    issues=[issue],
                )
            )
            continue

        acquire = signal.get("acquire") if isinstance(signal.get("acquire"), dict) else {}
        tool = str(acquire.get("tool") or "")
        args = acquire.get("args") if isinstance(acquire.get("args"), dict) else {}
        args_ok, args_error = validate_acquire_args(tool, args)
        if not args_ok:
            issue = _review_issue(
                "SIGNAL_ACQUIRE_ARGS_INVALID",
                str(args_error or "acquire.args 不合法"),
                signal_id=signal_id,
                signal_index=index,
                field="acquire.args",
                source="shared_acquirer_args",
            )
            per_signal.append(issue)
            issues.append(issue)

        resolver_id = _resolver_id(tool)
        if resolver_id is None:
            issue = _review_issue(
                "SIGNAL_RESOLVER_MISSING",
                f"acquire.tool 没有 Shared Resolution Runtime Resolver: {tool or '<missing>'}",
                signal_id=signal_id,
                signal_index=index,
                field="acquire.tool",
            )
            per_signal.append(issue)
            issues.append(issue)
            signal_reviews.append(
                SignalRuntimeReview(
                    signal_id=signal_id,
                    signal_index=index,
                    tool=tool or None,
                    status=ResolutionStatus.BLOCKED,
                    issues=per_signal,
                )
            )
            continue

        try:
            plan = runtime.compile(_intent(tool, args, signal))
            acquisition = runtime.resolve(plan, context or {})
        except Exception as exc:
            issue = _review_issue(
                "SIGNAL_RESOLUTION_FAILED",
                f"Shared Resolution Runtime 审查异常: {exc}",
                signal_id=signal_id,
                signal_index=index,
            )
            per_signal.append(issue)
            issues.append(issue)
            signal_reviews.append(
                SignalRuntimeReview(
                    signal_id=signal_id,
                    signal_index=index,
                    tool=tool,
                    resolver_id=resolver_id,
                    status=ResolutionStatus.BLOCKED,
                    issues=per_signal,
                )
            )
            continue

        runtime_issues = [*plan.issues, *acquisition.issues]
        seen_runtime_issues: set[tuple[str, str, str | None]] = set()
        for runtime_issue in runtime_issues:
            key = (runtime_issue.code, runtime_issue.message, runtime_issue.field)
            if key in seen_runtime_issues:
                continue
            seen_runtime_issues.add(key)
            level = "error" if acquisition.status is ResolutionStatus.BLOCKED else runtime_issue.level
            issue = _review_issue(
                runtime_issue.code,
                runtime_issue.message,
                signal_id=signal_id,
                signal_index=index,
                field=runtime_issue.field,
                level=level,
            )
            per_signal.append(issue)
            issues.append(issue)

        if require_verified and acquisition.status is not ResolutionStatus.VERIFIED:
            issue = _review_issue(
                "SIGNAL_RUNTIME_NOT_VERIFIED",
                f"Agent 执行前要求 verified，当前状态为 {acquisition.status.value}",
                signal_id=signal_id,
                signal_index=index,
                level="error",
            )
            per_signal.append(issue)
            issues.append(issue)

        # Argument-contract failures are a hard review failure even when a
        # resolver happens to compile the remaining fields.  Reflect that in
        # the per-signal status as well as the aggregate issue list; otherwise
        # reports would misleadingly count an invalid signal as ``verified``.
        effective_status = (
            ResolutionStatus.BLOCKED
            if not args_ok or index in schema_invalid_indexes
            else acquisition.status
        )
        signal_reviews.append(
            SignalRuntimeReview(
                signal_id=signal_id,
                signal_index=index,
                tool=tool,
                resolver_id=resolver_id,
                status=effective_status,
                catalog_version=acquisition.catalog_version,
                command=acquisition.command,
                candidates_tried=acquisition.candidates_tried,
                issues=per_signal,
            )
        )

    runtime_status_counts: dict[str, int] = {}
    for item in signal_reviews:
        key = item.status.value
        runtime_status_counts[key] = runtime_status_counts.get(key, 0) + 1

    blocked = bool(issues and any(issue.level == "error" for issue in issues)) or any(
        item.status is ResolutionStatus.BLOCKED for item in signal_reviews
    )
    needs_review = any(item.status is ResolutionStatus.NEEDS_PROBE for item in signal_reviews) or any(
        issue.level == "warning" for issue in issues
    )
    status = (
        SignalReviewStatus.BLOCKED
        if blocked
        else SignalReviewStatus.NEEDS_REVIEW
        if needs_review
        else SignalReviewStatus.PASSED
    )
    return SignalReviewResult(
        feature=feature,
        status=status,
        signal_count=len(signals),
        runtime_status_counts=runtime_status_counts,
        signals=signal_reviews,
        issues=issues,
    )
