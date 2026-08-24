"""qkv_effect 条件型效果验证生产者的 Shared Resolution Runtime 支持。

设计来源：`docs/solution/agent/效果验证生产者信号设计与需求.md` §5.2。

与 VmConsoleResolver 同理：本 Resolver **不返回字符串命令**，而返回不可变的
Verification Intent（验证意图）。观测一律委派已批准的只读采集原语（封闭通道
集合），判定规则是封闭 matcher 集合，KBD、运营页面和 LLM 均不能写入自由命令、
自由判定文本或运行时可变的期望。期望快照在编译时冻结，运行时逐字段回放。
"""

from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator
from pydantic import ValidationError as PydanticValidationError

from shared.resolution.catalog import resolution_catalog_version
from shared.resolution.models import (
    ResolutionIssue,
    ResolutionPlan,
    ResolutionStatus,
    ResolvedAcquisition,
    SignalIntent,
)
from shared.schemas.acquirer_args import (
    DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    EFFECT_MATCHER_TYPES,
    EFFECT_OBSERVATION_CHANNELS,
    EFFECT_USAGES,
    VM_CONSOLE_HOST_LITERAL_PATTERN,
    VM_CONSOLE_HOST_PLACEHOLDER,
    validate_acquire_args,
)

EFFECT_TOOL = "qkv_effect"
EFFECT_RESOLVER_ID = "effect"
# 唯一允许的固定操作；任何未在此枚举中的 operation 直接 Fail Closed。
EFFECT_OPERATIONS = frozenset({"verify_effect"})
EFFECT_OPERATION_VERIFY = "verify_effect"
EFFECT_TIMEOUT_RANGE = (1, 60)
# 设计文档 §4.1 禁止参数清单。acquirer_args 的 additionalProperties:false 已在
# 保存期拦截；Resolver 再次检查属于纵深防御，防止绕过保存期的直接编译调用。
EFFECT_FORBIDDEN_ARGS = frozenset(
    {
        "command",
        "shell",
        "path",
        "url",
        "judge_text",
        "prompt",
        "script",
    }
)
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Z][A-Z0-9_]*)(?:\.[A-Z0-9_]+)*\}\}")


def _issue(code: str, message: str, *, field: str | None = None) -> ResolutionIssue:
    return ResolutionIssue(code=code, message=message, field=field, level="error")


class EffectExpectationSnapshot(BaseModel):
    """冻结的期望锚点快照：观测通道 + 封闭判定规则 + 时序窗口。

    编译期冻结、运行期只读回放；KBD 事后修订不能篡改进行中的判定依据。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    usage: str = "remediation_verify"
    observation_tool: str
    observation_args: dict[str, Any] = Field(default_factory=dict)
    matcher: dict[str, Any]
    settle_seconds: int = Field(default=120, ge=0, le=3600)
    window_seconds: int = Field(default=900, ge=60, le=86400)
    max_recheck: int = Field(default=2, ge=0, le=5)

    @field_validator("usage")
    @classmethod
    def _usage_must_be_known(cls, value: str) -> str:
        if value not in EFFECT_USAGES:
            raise ValueError(f"未知的效果验证使用模式（fail-closed）: {value}")
        return value

    @field_validator("observation_tool")
    @classmethod
    def _observation_channel_must_be_closed(cls, value: str) -> str:
        if value not in EFFECT_OBSERVATION_CHANNELS:
            raise ValueError(f"观测通道不在封闭集合（fail-closed）: {value}")
        return value

    @field_validator("matcher")
    @classmethod
    def _matcher_must_be_closed(cls, value: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(value, dict) or value.get("type") not in EFFECT_MATCHER_TYPES:
            raise ValueError(f"判定规则不在封闭 matcher 集合（fail-closed）: {value.get('type') if isinstance(value, dict) else value}")
        if not isinstance(value.get("expected"), bool):
            raise ValueError("matcher.expected 必须是布尔值")
        if not isinstance(value.get("extract"), dict):
            raise ValueError("matcher 必须配置新版 extract")
        return value


class EffectVerificationIntent(BaseModel):
    """不可变的效果验证意图。

    执行端（agent-service 专用适配器 / 离线追溯判定）只接受本结构；operation
    必须在 ``EFFECT_OPERATIONS`` 枚举内，构造即校验，未知值 fail-closed。
    意图内部只引用已批准的观测原语，绝不携带命令字符串。
    """

    model_config = ConfigDict(extra="forbid", frozen=True)

    operation: str
    expectation: EffectExpectationSnapshot
    host_ref: str | None = None
    timeout_seconds: int = Field(default=DEFAULT_SIGNAL_TIMEOUT_SECONDS, ge=1, le=60)
    catalog_revision: str = "unknown"
    signal_id: str | None = None

    @field_validator("operation")
    @classmethod
    def _operation_must_be_known(cls, value: str) -> str:
        if value not in EFFECT_OPERATIONS:
            raise ValueError(f"未知的效果验证操作（fail-closed）: {value}")
        return value

    @field_validator("host_ref")
    @classmethod
    def _host_ref_shape(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = str(value).strip()
        if not stripped:
            return None
        if stripped != VM_CONSOLE_HOST_PLACEHOLDER and not VM_CONSOLE_HOST_LITERAL_PATTERN.fullmatch(stripped):
            raise ValueError("host_ref 仅允许 {{HOST}} 占位符或系统规范化节点标识")
        return stripped


def verification_intent_from(acquisition: ResolvedAcquisition) -> EffectVerificationIntent | None:
    """从 verified acquisition 还原 Verification Intent；还原失败即拒绝执行。"""

    payload = (acquisition.evidence or {}).get("verification_intent")
    if not isinstance(payload, dict):
        return None
    try:
        return EffectVerificationIntent.model_validate(payload)
    except PydanticValidationError:
        return None


def _substitute_placeholders(
    value: Any,
    variables: dict[str, Any],
    unresolved: set[str],
    used: set[str] | None = None,
) -> Any:
    """递归替换嵌套结构中的 ``{{VAR}}`` 占位符；缺失变量记录后原样保留（fail-closed）。"""

    if isinstance(value, str):

        def _replace(match: re.Match[str]) -> str:
            name = match.group(1)
            resolved = variables.get(name)
            if resolved in (None, ""):
                # 容忍大小写不一致的变量池键；仅唯一匹配时采用（与 vm_console 同口径）。
                candidates = {key: item for key, item in variables.items() if key.upper() == name}
                if len(candidates) == 1:
                    resolved = next(iter(candidates.values()))
            if resolved in (None, ""):
                unresolved.add(name)
                return match.group(0)
            if used is not None:
                used.add(name)
            return str(resolved)

        return _PLACEHOLDER_RE.sub(_replace, value)
    if isinstance(value, list):
        return [_substitute_placeholders(item, variables, unresolved, used) for item in value]
    if isinstance(value, dict):
        return {key: _substitute_placeholders(item, variables, unresolved, used) for key, item in value.items()}
    return value


class EffectVerificationResolver:
    """把 qkv_effect 信号编译为受控 Verification Intent（不产出命令字符串）。"""

    resolver_id = EFFECT_RESOLVER_ID

    def compile(self, intent: SignalIntent) -> ResolutionPlan:
        args = dict(intent.args)
        catalog_version = resolution_catalog_version()

        def blocked(issues: list[ResolutionIssue]) -> ResolutionPlan:
            return ResolutionPlan(
                resolver_id=self.resolver_id,
                tool=intent.tool or EFFECT_TOOL,
                canonical_args=dict(intent.args),
                catalog_version=catalog_version,
                status=ResolutionStatus.BLOCKED,
                issues=issues,
            )

        forbidden = sorted(set(args) & EFFECT_FORBIDDEN_ARGS)
        if forbidden:
            return blocked(
                [
                    _issue(
                        "EFFECT_ARGS_FORBIDDEN",
                        f"qkv_effect 不接受自由执行/判定参数: {', '.join(forbidden)}",
                        field="acquire.args",
                    )
                ]
            )

        # 纵深防御：完整走一遍保存期同款语义门禁（封闭通道/封闭 matcher/窗口范围/
        # 观测原语 args 递归校验），防止绕过保存期的直接编译调用。
        ok, error = validate_acquire_args(EFFECT_TOOL, args)
        if not ok:
            return blocked([_issue("EFFECT_CONTRACT_INVALID", str(error), field="acquire.args")])

        expectation_raw = dict(args.get("expectation") or {})
        observation = dict(expectation_raw.get("observation") or {})
        try:
            snapshot = EffectExpectationSnapshot(
                usage=str(args.get("usage") or "remediation_verify"),
                observation_tool=str(observation.get("tool") or ""),
                observation_args=dict(observation.get("args") or {}),
                matcher=dict(expectation_raw.get("matcher") or {}),
                settle_seconds=int(expectation_raw.get("settle_seconds", 120)),
                window_seconds=int(expectation_raw.get("window_seconds", 900)),
                max_recheck=int(expectation_raw.get("max_recheck", 2)),
            )
        except PydanticValidationError as exc:
            return blocked([_issue("EFFECT_EXPECTATION_INVALID", f"期望锚点不可编译: {exc}", field="acquire.args.expectation")])

        host = str(args.get("host") or "").strip() or None
        timeout_raw = args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS)
        if isinstance(timeout_raw, bool) or not isinstance(timeout_raw, int):
            return blocked([_issue("EFFECT_TIMEOUT_INVALID", "timeout 必须是整数（秒，1-60）", field="acquire.args.timeout")])
        low, high = EFFECT_TIMEOUT_RANGE
        if not low <= timeout_raw <= high:
            return blocked(
                [
                    _issue(
                        "EFFECT_TIMEOUT_INVALID",
                        f"timeout 必须在 {low}-{high}（单次观测快速失败）: {timeout_raw}",
                        field="acquire.args.timeout",
                    )
                ]
            )

        verification_intent = EffectVerificationIntent(
            operation=EFFECT_OPERATION_VERIFY,
            expectation=snapshot,
            host_ref=host,
            timeout_seconds=timeout_raw,
            catalog_revision=catalog_version,
        )
        return ResolutionPlan(
            resolver_id=self.resolver_id,
            tool=intent.tool or EFFECT_TOOL,
            canonical_args={
                "verification_intent": verification_intent.model_dump(mode="json"),
                "timeout_seconds": timeout_raw,
            },
            # 刻意为空：本 Resolver 不产出任何命令字符串。
            argv_template=[],
            catalog_version=catalog_version,
        )

    def resolve(self, plan: ResolutionPlan, context: dict[str, Any] | None = None) -> ResolvedAcquisition:
        if plan.status is ResolutionStatus.BLOCKED:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=plan.issues,
            )

        payload = plan.canonical_args.get("verification_intent")
        try:
            intent = EffectVerificationIntent.model_validate(payload)
        except PydanticValidationError as exc:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=[_issue("EFFECT_INTENT_INVALID", f"Verification Intent 不可还原: {exc}")],
            )

        values = dict((context or {}).get("variables") or {})
        unresolved: set[str] = set()
        used: set[str] = set()
        snapshot = intent.expectation
        resolved_observation_args = _substitute_placeholders(snapshot.observation_args, values, unresolved, used)
        resolved_matcher = _substitute_placeholders(snapshot.matcher, values, unresolved, used)
        resolved_host: str | None = None
        if intent.host_ref:
            if intent.host_ref == VM_CONSOLE_HOST_PLACEHOLDER:
                resolved_host = _substitute_placeholders(intent.host_ref, values, unresolved, used)
                if resolved_host == VM_CONSOLE_HOST_PLACEHOLDER:
                    unresolved.add("HOST")
                    resolved_host = None
            else:
                resolved_host = intent.host_ref

        if unresolved:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.NEEDS_PROBE,
                catalog_version=plan.catalog_version,
                issues=[
                    ResolutionIssue(
                        code="VARIABLE_UNRESOLVED",
                        message=f"效果验证期望锚点变量未解析: {', '.join(sorted(unresolved))}",
                        field=",".join(sorted(unresolved)),
                        # needs_probe 是运行时待解析项，不是静态错误（review.py 头注）。
                        level="warning",
                    )
                ],
                evidence={"verification_intent": intent.model_dump(mode="json")},
            )

        # 解析后对观测原语参数再做一次安全校验（变量注入后的最终形态）。
        ok, error = validate_acquire_args(snapshot.observation_tool, resolved_observation_args)
        if not ok:
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=[
                    _issue(
                        "EFFECT_OBSERVATION_ARGS_INVALID",
                        f"解析后的观测原语参数不安全（{snapshot.observation_tool}）: {error}",
                        field="expectation.observation.args",
                    )
                ],
                evidence={"verification_intent": intent.model_dump(mode="json")},
            )
        if resolved_host is not None and not VM_CONSOLE_HOST_LITERAL_PATTERN.fullmatch(resolved_host):
            return ResolvedAcquisition(
                resolver_id=self.resolver_id,
                tool=plan.tool,
                status=ResolutionStatus.BLOCKED,
                catalog_version=plan.catalog_version,
                issues=[_issue("EFFECT_HOST_INVALID", f"解析后的节点标识不安全: {resolved_host}", field="host")],
                evidence={"verification_intent": intent.model_dump(mode="json")},
            )

        resolved_snapshot = snapshot.model_copy(
            update={"observation_args": resolved_observation_args, "matcher": resolved_matcher}
        )
        resolved_intent = intent.model_copy(
            update={"expectation": resolved_snapshot, "host_ref": resolved_host or None}
        )
        variables_used = {name: values.get(name) for name in sorted(used)}
        return ResolvedAcquisition(
            resolver_id=self.resolver_id,
            tool=plan.tool,
            status=ResolutionStatus.VERIFIED,
            # argv 为空、command 为 None 是刻意设计：执行端只消费 verification_intent。
            argv=[],
            command=None,
            resolution_rule="effect-fixed-verification-intent",
            catalog_version=plan.catalog_version,
            variables_used=variables_used,
            evidence={"verification_intent": resolved_intent.model_dump(mode="json")},
        )
