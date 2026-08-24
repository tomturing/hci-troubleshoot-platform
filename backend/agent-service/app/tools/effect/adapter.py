"""qkv_effect 条件型效果验证生产者的在线专用适配器。

设计来源：`docs/solution/agent/效果验证生产者信号设计与需求.md`。

职责链：策略开关 → Shared Resolution Runtime 编译不可变 Verification Intent
（fail-closed）→ 创建效果验证记录 → 稳定窗口等待 → 观测循环（观测原语委派 +
封闭 matcher 求值 + 三态合成，窗口内有限复核）→ 变量产出。

三态合成原则（§3.4）：观测有效且规则通过=achieved；观测有效但规则未通过=
not_achieved；观测失败/负证据不足/窗口耗尽=inconclusive——绝不向两侧坍缩。
"""

from __future__ import annotations

import asyncio
import json
import os
import uuid
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

from shared.observability.logger import get_logger
from shared.resolution.models import ResolutionStatus, SignalIntent
from shared.resolution.runtime import get_resolution_runtime
from shared.signals.matcher import evaluate_matcher

from app.tools.effect import store as effect_store
from app.tools.qkv.parser import parse_frontend_value
from app.tools.qkv.signal import FrontendQueryType, FrontendSignal

logger = get_logger("effect-verification-adapter")

EFFECT_ENABLED_ENV = "EFFECT_VERIFICATION_ENABLED"
ADAPTER_VERSION = "effect-adapter-v1"
# 复核间隔下限（秒）：不小于观测原语自身超时的量级，避免无效打爆。
RECHECK_INTERVAL_MIN_SECONDS = 30


def effect_enabled() -> bool:
    """执行层策略门禁（默认关闭；契约与发布不受开关影响）。"""

    return os.environ.get(EFFECT_ENABLED_ENV, "false").lower() in ("1", "true", "yes", "on")


@dataclass
class EffectVerificationResult:
    """效果验证执行结果。字段与 QKVResult 对齐，供 _fill_pool_from_qkv 消费。"""

    success: bool
    query: str = "effect"
    keyword: str = ""
    # 固定意图标识，非可执行命令：本适配器绝不产出命令字符串。
    command: str = "effect_verification://fixed-operation"
    values: list[dict[str, Any]] = field(default_factory=list)
    error: str | None = None
    error_code: str | None = None
    exec_id: str | None = None
    verification_id: str | None = None
    resolution: dict[str, Any] = field(default_factory=dict)

    def to_observation(self) -> str:
        """给 ReAct/判定层的标准化观察文本。"""
        if not self.success:
            return f"效果验证失败（{self.error_code or 'UNKNOWN'}）：{self.error or '未知错误'}"
        payload = self.values[0] if self.values else {}
        verdict = str(payload.get("effect_status") or "inconclusive")
        return f"效果验证判定：{verdict}（复核记录 verification_id={self.verification_id}）"


async def _run_observation(
    observation_tool: str,
    observation_args: dict[str, Any],
    *,
    conversation_id: str,
    node_ip: str | None,
    exec_id: str | None,
    env_context: dict[str, str],
    db_session_factory: Any,
    case_id: str,
    session_id: str,
) -> tuple[bool, str, str | None]:
    """委派观测原语执行，返回 (观测是否有效, 观测输出文本, 错误描述)。

    观测全部复用已批准的只读采集路径：alert/task/dialog 走 qkv_exec 受控参数
    分支；vm_console 走专用截图适配器。绝不新开命令面。
    """

    if observation_tool == "qkv_vm_console":
        from app.tools.vm_console.adapter import run_vm_console_signal

        observation_signal = {
            "acquire": {"tool": "qkv_vm_console", "args": dict(observation_args)},
            "orchestrate": {"produces": []},
        }
        result = await run_vm_console_signal(
            observation_signal,
            env_context,
            conversation_id=conversation_id,
            case_id=case_id,
            session_id=session_id,
            exec_id=exec_id,
            db_session_factory=db_session_factory,
        )
        if not result.success:
            return False, "", result.error or result.error_code or "控制台观测失败"
        return True, _encode_observation_text(result.values), None

    from app.tools.qkv.engine import qkv_exec

    frontend_signal = FrontendSignal.from_dict(
        {
            "acquire": {"tool": observation_tool, "args": dict(observation_args)},
            "orchestrate": {"produces": []},
        }
    )
    result = await qkv_exec(
        signal=frontend_signal,
        conversation_id=conversation_id,
        node_ip=node_ip,
        exec_id=exec_id,
    )
    if not result.success:
        return False, "", result.error or "观测原语执行失败"
    # values 为空 ≠ 观测失败：查询成功且确无记录正是负证据（exists/expected=false）
    # 的有效观测域。观测失败（success=False）与确无结果在此显式区分；空结果编码为
    # 空文本，禁止用 "[]" 冒充——否则 exists matcher 会把空 JSON 数组误判为存在记录。
    return True, _encode_observation_text(result.values), None


def _encode_observation_text(values: list[dict[str, Any]]) -> str:
    """把观测原语的结构化结果编码为 matcher 求值文本；空结果集编码为空串。"""

    if not values:
        return ""
    return json.dumps(values, ensure_ascii=False)


async def _push_effect_result_card(
    *,
    conversation_id: str,
    case_id: str,
    verification_id: str,
    signal_id: str | None,
    verdict: str,
    usage: str,
    check_count: int,
    error_code: str | None,
    checked_at: str,
    trace_id: str,
) -> None:
    """把三态判定结果卡推送到 conversation-service（SSE + message 历史）。

    best-effort：推送失败仅告警，不影响判定与变量回写。conversation_id 必须是
    合法 UUID（会话真实 ID）；诊断运行以 session_id 兜底时跳过推送。
    """

    try:
        uuid.UUID(conversation_id)
    except (ValueError, AttributeError):
        return
    try:
        from shared.utils.internal_http import InternalHTTPClient

        from app.config import settings

        async with InternalHTTPClient(base_url=settings.CONVERSATION_SERVICE_URL, timeout=5.0) as client:
            await client.post(
                f"/internal/conversations/{conversation_id}/effect-result",
                json={
                    "verification_id": verification_id,
                    "case_id": case_id or "unknown",
                    "signal_id": signal_id,
                    "verdict": verdict,
                    "usage": usage,
                    "check_count": max(1, check_count),
                    "error_code": error_code,
                    "checked_at": checked_at,
                    "trace_id": trace_id,
                },
            )
    except Exception as exc:
        logger.warning(
            event="effect_result_card_push_failed",
            verification_id=verification_id,
            error=str(exc),
        )


def _resolve_effect_match(
    matcher: dict[str, Any], observation_text: str, matcher_result: Any
) -> bool | None:
    """效果验证上下文的 matcher 结果裁决。

    QFK 上下文中空 stdout 意味着“命令可能失败、不可定值”（matched=None），这是
    正确的；但效果验证的观测原语已自证有效（success=True）时，空结果是**确定性
    负证据**。仅对 exists 判定做该补齐（present=False → matched = not expected），
    其余类型保持 inconclusive，禁止坍缩。
    """

    if matcher_result.matched is not None:
        return matcher_result.matched
    if observation_text.strip():
        return None
    detail_error = str((matcher_result.detail or {}).get("error") or "")
    if str(matcher.get("type") or "") == "exists" and detail_error.startswith("QFK_OUTPUT_EMPTY"):
        expected = bool(matcher.get("expected", True))
        matcher_result.evidence = (
            f"{matcher_result.evidence}\n【效果验证补齐】观测原语已自证有效且结果确为空，负证据确定性成立"
        )
        return not expected
    return None


async def run_effect_verification_signal(
    signal: dict[str, Any],
    env_context: dict[str, str],
    *,
    conversation_id: str,
    case_id: str,
    session_id: str = "",
    exec_id: str | None = None,
    db_session_factory: Any,
    user_id: str | None = None,
) -> EffectVerificationResult:
    """执行 qkv_effect 信号的完整在线复核（settle + 有限 recheck + 三态判定）。"""

    acquire = signal.get("acquire") or {}
    args = dict(acquire.get("args") or {})
    produces = (signal.get("orchestrate") or {}).get("produces") or []
    signal_id = str(signal.get("id") or "") or None
    trace_id = str(uuid.uuid4().hex)

    # ── 0. 策略开关：§12 平台确认项闭环前执行层默认关闭 ──
    if not effect_enabled():
        logger.warning(event="effect_verification_disabled_by_policy", signal_id=signal_id)
        return EffectVerificationResult(
            success=False,
            error="效果验证执行层未启用（EFFECT_VERIFICATION_ENABLED=false）",
            error_code="EFFECT_POLICY_DISABLED",
            exec_id=exec_id,
        )

    # ── 1. 编译不可变 Verification Intent（fail-closed）──
    plan, acquisition = get_resolution_runtime().compile_and_resolve(
        SignalIntent(resolver_id="effect", tool="qkv_effect", args=args, source="kbd_differential"),
        {"variables": dict(env_context)},
    )
    if acquisition.status is ResolutionStatus.BLOCKED:
        message = "；".join(issue.message for issue in acquisition.issues) or "Verification Intent 编译失败"
        logger.warning(event="effect_verification_blocked", signal_id=signal_id, reason=message)
        return EffectVerificationResult(
            success=False,
            error=message,
            error_code="EXPECTATION_CONTRACT_INVALID",
            exec_id=exec_id,
            resolution={"plan_status": plan.status.value},
        )
    if acquisition.status is ResolutionStatus.NEEDS_PROBE:
        message = "；".join(issue.message for issue in acquisition.issues) or "期望锚点变量未解析"
        logger.warning(event="effect_verification_needs_probe", signal_id=signal_id, reason=message)
        return EffectVerificationResult(
            success=False,
            error=message,
            error_code="EXPECTATION_SOURCE_MISSING",
            exec_id=exec_id,
            resolution={"plan_status": plan.status.value},
        )

    intent_payload = (acquisition.evidence or {}).get("verification_intent") or {}
    expectation = dict(intent_payload.get("expectation") or {})
    observation_tool = str(expectation.get("observation_tool") or "")
    observation_args = dict(expectation.get("observation_args") or {})
    matcher = dict(expectation.get("matcher") or {})
    settle_seconds = int(expectation.get("settle_seconds") or 0)
    window_seconds = int(expectation.get("window_seconds") or 900)
    max_recheck = int(expectation.get("max_recheck") or 0)
    usage = str(args.get("usage") or "remediation_verify")
    host = str(args.get("host") or "") or None
    timeout_seconds = int(args.get("timeout") or 60)

    verification_id = str(uuid.uuid4())
    now = datetime.now(UTC)

    # ── 2. 创建效果验证记录（期望快照冻结入库）──
    if db_session_factory is not None:
        try:
            async with db_session_factory() as session:
                await effect_store.create_verification_record(
                    session,
                    verification_id=verification_id,
                    tenant_id=None,
                    case_id=case_id,
                    diagnosis_run_id=session_id or None,
                    conversation_id=conversation_id or None,
                    signal_id=signal_id,
                    usage=usage,
                    action_exec_id=exec_id,
                    expectation_snapshot=expectation,
                    target_verification={"host": host, "resolved_at": now.isoformat()},
                    source_kbd_id=str(signal.get("source_kbd_id") or "") or None,
                    source_kbd_revision=str(signal.get("source_kbd_revision") or "") or None,
                    tool_catalog_revision=acquisition.catalog_version,
                    trace_id=trace_id,
                    next_check_at=(now + timedelta(seconds=settle_seconds)).isoformat(),
                )
                await effect_store.update_verification_status(
                    session, verification_id, "expectation_resolved"
                )
        except Exception as exc:  # 落库失败不阻断判定，但必须留痕
            logger.warning(event="effect_verification_store_failed", error=str(exc), signal_id=signal_id)

    async def _store_status(status: str, **kwargs: Any) -> None:
        if db_session_factory is None:
            return
        try:
            async with db_session_factory() as session:
                await effect_store.update_verification_status(session, verification_id, status, **kwargs)
        except Exception as exc:
            logger.warning(event="effect_verification_store_failed", error=str(exc), status=status)

    async def _store_check(check_seq: int, **kwargs: Any) -> None:
        if db_session_factory is None:
            return
        try:
            async with db_session_factory() as session:
                await effect_store.insert_check_record(
                    session, verification_id=verification_id, check_seq=check_seq, **kwargs
                )
        except Exception as exc:
            logger.warning(event="effect_verification_store_failed", error=str(exc), check_seq=check_seq)

    # ── 3. 稳定窗口：避开效果潜伏期造成的假“未达预期” ──
    if settle_seconds > 0:
        await _store_status("settle_pending")
        await asyncio.sleep(settle_seconds)

    # ── 4. 观测循环：1 次首判 + max_recheck 次复核，总预算 window_seconds ──
    deadline = datetime.now(UTC) + timedelta(seconds=window_seconds)
    recheck_interval = max(timeout_seconds, RECHECK_INTERVAL_MIN_SECONDS)
    node_ip = str(env_context.get("node_ip") or "") or None
    verdict: str = "inconclusive"
    last_error_code: str | None = None
    last_error: str | None = None
    check_seq = 0
    total_checks = 1 + max(0, max_recheck)

    while check_seq < total_checks:
        if datetime.now(UTC) >= deadline:
            last_error_code = "WINDOW_EXPIRED_INCONCLUSIVE"
            last_error = "复核窗口耗尽，判定仍两可"
            break
        check_seq += 1
        await _store_status("observing")
        observed_ok, observation_text, observation_error = await _run_observation(
            observation_tool,
            observation_args,
            conversation_id=conversation_id,
            node_ip=node_ip,
            exec_id=exec_id,
            env_context=env_context,
            db_session_factory=db_session_factory,
            case_id=case_id,
            session_id=session_id,
        )

        if not observed_ok:
            # 观测失败不等于“未达预期”：记 ERROR，允许复核窗口内重试。
            last_error_code = "OBSERVATION_ERROR"
            last_error = observation_error
            await _store_check(
                check_seq,
                trigger_source="scheduler",
                observation_status="error",
                observation_summary=str(observation_error or "")[:2000],
                matcher_evidence=None,
                check_verdict="inconclusive",
                error_code="OBSERVATION_ERROR",
                trace_id=trace_id,
            )
            verdict = "inconclusive"
        else:
            matcher_result = evaluate_matcher(matcher, observation_text)
            matched = _resolve_effect_match(matcher, observation_text, matcher_result)
            if matched is None:
                # 无法确定性求值（取值配置缺失等）：观察不足，禁止坍缩。
                last_error_code = "NEGATIVE_EVIDENCE_INSUFFICIENT"
                last_error = matcher_result.evidence or "matcher 无法确定性求值"
                await _store_check(
                    check_seq,
                    trigger_source="scheduler",
                    observation_status="insufficient",
                    observation_summary=observation_text[:2000],
                    matcher_evidence=matcher_result.evidence,
                    check_verdict="inconclusive",
                    error_code="NEGATIVE_EVIDENCE_INSUFFICIENT",
                    trace_id=trace_id,
                )
                verdict = "inconclusive"
            elif matched:
                verdict = "achieved"
                await _store_check(
                    check_seq,
                    trigger_source="scheduler",
                    observation_status="valid",
                    observation_summary=observation_text[:2000],
                    matcher_evidence=matcher_result.evidence,
                    check_verdict="achieved",
                    trace_id=trace_id,
                )
                break
            else:
                verdict = "not_achieved"
                await _store_check(
                    check_seq,
                    trigger_source="scheduler",
                    observation_status="valid",
                    observation_summary=observation_text[:2000],
                    matcher_evidence=matcher_result.evidence,
                    check_verdict="not_achieved",
                    trace_id=trace_id,
                )
                break

        # 仍有复核配额且窗口未耗尽：等待复核间隔后继续。
        if check_seq < total_checks and datetime.now(UTC) < deadline:
            await _store_status(
                "recheck_scheduled",
                increment_recheck=True,
                next_check_at=(datetime.now(UTC) + timedelta(seconds=recheck_interval)).isoformat(),
            )
            await asyncio.sleep(min(recheck_interval, max(0, (deadline - datetime.now(UTC)).total_seconds())))

    # ── 5. 终态落库 ──
    final_status = f"verdict_{verdict}"
    await _store_status(
        final_status,
        verdict=verdict,
        error_code=last_error_code,
        error_summary=last_error,
        clear_next_check=True,
        completed=True,
    )

    checked_at = datetime.now(UTC).isoformat()

    # ── 6. 结果卡推送（best-effort）：SSE + message 历史，失败不阻断判定 ──
    await _push_effect_result_card(
        conversation_id=conversation_id,
        case_id=case_id,
        verification_id=verification_id,
        signal_id=signal_id,
        verdict=verdict,
        usage=usage,
        check_count=check_seq,
        error_code=last_error_code,
        checked_at=checked_at,
        trace_id=trace_id,
    )
    verdict_payload = {
        "verdict": verdict,
        "checked_at": checked_at,
        "evidence_ref": f"effect_verification:{verification_id}",
        "usage": usage,
        "error_code": last_error_code,
        "check_count": check_seq,
    }
    values = parse_frontend_value(FrontendQueryType.EFFECT, json.dumps(verdict_payload, ensure_ascii=False), produces)
    logger.info(
        event="effect_verification_completed",
        signal_id=signal_id,
        verification_id=verification_id,
        verdict=verdict,
        check_count=check_seq,
    )
    return EffectVerificationResult(
        success=True,
        values=values,
        exec_id=exec_id,
        verification_id=verification_id,
        resolution={
            "verdict": verdict,
            "error_code": last_error_code,
            "check_count": check_seq,
            "catalog_version": acquisition.catalog_version,
        },
    )
