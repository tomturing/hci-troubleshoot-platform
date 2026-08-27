"""QFK/QKV 统一 AI 信号后处理器。

确定性取值先构造候选输入，AI 只做可选再加工。两种 AI 处理方式共用同一响应契约，
平台完成结构、类型、证据和下游消费者校验。
"""

from __future__ import annotations

import hashlib
import json
import re
import time
from dataclasses import dataclass
from typing import Any

from shared.observability.langfuse import update_observation
from shared.observability.logger import get_logger
from shared.observability.metrics import (
    AI_PROCESSING_VALIDATION_FAILURES_TOTAL,
    QFK_AI_PROCESSING_DURATION_SECONDS,
    QFK_AI_PROCESSINGS_TOTAL,
)
from shared.observability.otel import get_current_trace_id
from shared.signals.ai_processing import (
    ai_output_type,
    ai_processing_config,
    ai_processing_mode,
    validate_ai_processing_config,
    validate_ai_response,
)
from shared.signals.extractor import ExtractionResult, QFKExtractionError, extract_output_values
from shared.utils.prompt_loader import StrictPromptLoader

logger = get_logger("signal-ai-processor")

MAX_AI_EXTRACT_INPUT_BYTES = 64 * 1024
MAX_AI_EXTRACT_LINES = 200
AI_PROCESSING_PROMPT_NAME = "ai_processing"
AI_PROCESSING_PROMPT_PLACEHOLDERS = ["mode", "output_type"]


@dataclass(frozen=True)
class AIExtractionResult:
    """兼容 QFK/QKV 结果字段的统一 AI 后处理结果。"""

    value: Any
    raw_value: Any
    evidence_line_numbers: list[int]
    evidence_lines: list[str]
    candidate_count: int
    instruction: str
    reason: str
    output_type: str
    response_hash: str | None = None
    response_chars: int = 0
    prompt_name: str = AI_PROCESSING_PROMPT_NAME
    prompt_revision: str | None = None


def has_ai_extract(spec: Any) -> bool:
    """判断 Extract 是否声明了新 AI 后处理或历史兼容配置。"""

    return isinstance(spec, dict) and ai_processing_config(spec) is not None


def ai_value_type_for_matcher(matcher_type: str) -> str | None:
    normalized = str(matcher_type or "").strip().lower()
    if normalized in {"threshold", "delta", "trend"}:
        return "array<number>"
    return None


def _deterministic_spec(spec: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in spec.items() if key not in {"ai_processing", "ai_extract", "value_mode"}}


def _validate_ai_config(spec: dict[str, Any], expected_type: str) -> tuple[dict[str, Any], str, str, str]:
    config = ai_processing_config(spec)
    legacy = isinstance(spec, dict) and "ai_processing" not in spec and "ai_extract" in spec
    try:
        mode = validate_ai_processing_config(config)
    except ValueError as exc:
        raise QFKExtractionError("QFK_AI_PROCESSING_INVALID_SPEC", str(exc)) from exc
    assert config is not None
    instruction = str(config["instruction"]).strip()
    output_type = expected_type if legacy else ai_output_type(config, expected_type)
    if expected_type == "array<number>" or output_type == "array<number>":
        output_type = "array"
        config = {**config, "output_type": "array", "item_type": "number"}
    return config, instruction, mode, output_type


def _keyword_candidate_lines(result: ExtractionResult, matcher: dict[str, Any] | None) -> tuple[list[int], list[str]]:
    numbers = list(result.selected_line_numbers)
    lines = list(result.selected_lines)
    if not matcher or str(matcher.get("type") or "") != "keyword":
        return numbers, lines
    pattern = matcher.get("pattern")
    keywords = [pattern] if isinstance(pattern, str) else list(pattern or [])
    keywords = [item for item in keywords if isinstance(item, str) and item]
    mode = str(matcher.get("mode") or "or")
    if not keywords or mode == "not":
        raise QFKExtractionError("QFK_AI_PROCESSING_NO_CANDIDATE", "AI 处理没有可用的正向关键字候选")
    selected = []
    for number, line in zip(numbers, lines, strict=True):
        hit = all(item in line for item in keywords) if mode == "and" else any(item in line for item in keywords)
        if hit:
            selected.append((number, line))
    if not selected:
        raise QFKExtractionError("QFK_AI_PROCESSING_NO_CANDIDATE", "没有单行满足 AI 处理候选条件")
    return [item[0] for item in selected], [item[1] for item in selected]


def _validate_candidate_budget(numbers: list[int], lines: list[str]) -> None:
    if not lines:
        raise QFKExtractionError("QFK_AI_PROCESSING_NO_CANDIDATE", "AI 处理没有可用的完整输出行")
    if len(lines) > MAX_AI_EXTRACT_LINES:
        raise QFKExtractionError("QFK_AI_PROCESSING_INPUT_TOO_LARGE", f"AI 候选行超过 {MAX_AI_EXTRACT_LINES} 条")
    payload = json.dumps([{"line": n, "text": line} for n, line in zip(numbers, lines, strict=True)], ensure_ascii=False)
    if len(payload.encode("utf-8")) > MAX_AI_EXTRACT_INPUT_BYTES:
        raise QFKExtractionError("QFK_AI_PROCESSING_INPUT_TOO_LARGE", f"AI 候选输出超过 {MAX_AI_EXTRACT_INPUT_BYTES} 字节")


def _output_is_grounded(output: Any, evidence_lines: list[str]) -> bool:
    """原文取值必须能在证据原文中逐字（或数字规范化后）回查。"""

    quotes = "\n".join(evidence_lines)
    values = output if isinstance(output, list) else [output]
    for value in values:
        literal = str(value)
        if literal in quotes:
            continue
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            tokens = re.findall(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", quotes)
            if any(float(token) == float(value) for token in tokens):
                continue
        return False
    return True


def _upgrade_legacy_response(payload: Any, candidates: dict[str, str]) -> Any:
    """读取旧版本响应并立刻转换为统一契约；新运行路径不再产生旧字段。"""

    if not isinstance(payload, dict) or "status" in payload:
        return payload
    if payload.get("ok") is True and "value" in payload:
        output = payload["value"]
        if isinstance(output, str) and "," in output:
            parts = [item.strip() for item in output.split(",") if item.strip()]
            if parts and all(re.fullmatch(r"[+-]?(?:\d+(?:\.\d+)?|\.\d+)", item) for item in parts):
                output = [float(item) for item in parts]
        refs = payload.get("evidence_lines") or []
        evidence = [
            {"ref": f"line:{int(number)}", "quote": candidates[f"line:{int(number)}"]}
            for number in refs
            if f"line:{int(number)}" in candidates
        ]
        return {"status": "success", "output": output, "evidence": evidence, "reason": "兼容历史 AI 响应"}
    if payload.get("ok") is True and isinstance(payload.get("records"), list):
        values = [item.get("source_value") for item in payload["records"] if isinstance(item, dict) and "source_value" in item]
        refs = [number for item in payload["records"] if isinstance(item, dict) for number in item.get("evidence_lines") or []]
        evidence = [
            {"ref": f"line:{int(number)}", "quote": candidates[f"line:{int(number)}"]}
            for number in refs
            if f"line:{int(number)}" in candidates
        ]
        return {"status": "success", "output": values, "evidence": evidence, "reason": "兼容历史 AI 响应"}
    return payload


async def _load_ai_processing_system_prompt(
    db_session_factory: Any,
    *,
    mode: str,
    output_type: str,
    consumer: str = "agent-service.signal.ai_processing",
    conversation_id: str,
    case_id: str,
) -> tuple[str, str | None]:
    """从统一 Prompt 管理链路加载 AI 后处理系统 Prompt。

    AI 后处理不能在数据库不可用时退回代码内置 Prompt，否则管理员看到的版本与
    实际执行版本会分离。返回渲染后的文本和模板内容指纹（动态资源 revision 由统一加载器审计）。
    """

    if db_session_factory is None:
        raise QFKExtractionError("QFK_AI_PROCESSING_PROMPT_UNAVAILABLE", "AI 后处理 Prompt 管理数据库不可用")
    try:
        async with db_session_factory() as session:
            template = await StrictPromptLoader.load_and_validate(
                session,
                AI_PROCESSING_PROMPT_NAME,
                AI_PROCESSING_PROMPT_PLACEHOLDERS,
                consumer=consumer,
                conversation_id=conversation_id,
                case_id=case_id,
                trace_id=get_current_trace_id(),
            )
            rendered = template.format(mode=mode, output_type=output_type)
            # 加载器已将实际 system_prompt revision 写入动态资源审计；这里保留内容
            # 指纹，便于 Langfuse 在不暴露完整模板正文的情况下关联本次调用。
            return rendered, hashlib.sha256(template.encode("utf-8")).hexdigest()
    except QFKExtractionError:
        raise
    except Exception as exc:
        raise QFKExtractionError("QFK_AI_PROCESSING_PROMPT_UNAVAILABLE", f"AI 后处理 Prompt 加载失败: {exc}") from exc


async def _extract_ai_value_impl(
    output: str,
    spec: dict[str, Any],
    value_type: str,
    ai_client: Any,
    *,
    matcher: dict[str, Any] | None = None,
    consumer: str = "agent-service.signal.ai_processing",
    signal_type: str = "signal",
    conversation_id: str = "",
    case_id: str = "",
    run_id: str = "",
    signal_id: str = "",
    kbd_revision: int | str | None = None,
    db_session_factory: Any | None = None,
) -> AIExtractionResult:
    from shared.observability.langfuse import observe_llm_generation

    config, instruction, mode, output_type = _validate_ai_config(spec, value_type)
    legacy = "ai_processing" not in spec and "ai_extract" in spec
    if ai_client is None:
        raise QFKExtractionError("QFK_AI_PROCESSING_UNAVAILABLE", "AI 处理客户端不可用")
    selected = extract_output_values(output, _deterministic_spec(spec), "string")
    line_numbers, lines = _keyword_candidate_lines(selected, matcher)
    _validate_candidate_budget(line_numbers, lines)
    candidates = {f"line:{number}": line for number, line in zip(line_numbers, lines, strict=True)}
    system_prompt, prompt_revision = await _load_ai_processing_system_prompt(
        db_session_factory,
        mode=mode,
        output_type=output_type,
        consumer=consumer,
        conversation_id=conversation_id,
        case_id=case_id,
    )
    with observe_llm_generation(
        operation="ai_processing",
        model=ai_client.default_model if hasattr(ai_client, "default_model") else "unknown",
        input={"instruction": instruction, "mode": mode, "output_type": output_type, "candidate_count": len(lines), "candidate_lines": [{"ref": ref, "content": text[:200]} for ref, text in list(candidates.items())[:10]]},
        metadata={"conversation_id": conversation_id, "case_id": case_id, "run_id": run_id, "signal_id": signal_id, "signal_type": signal_type, "kbd_revision": kbd_revision, "value_type": value_type, "mode": mode, "output_type": output_type, "prompt_name": AI_PROCESSING_PROMPT_NAME, "prompt_revision": prompt_revision, "otel_trace_id": get_current_trace_id()},
    ) as observation:
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": json.dumps({"instruction": instruction, "mode": mode, "output_type": output_type, "candidates": [{"ref": ref, "content": text} for ref, text in candidates.items()]}, ensure_ascii=False)},
        ]
        raw_response = ""
        payload: Any = None
        try:
            response = await ai_client.invoke(messages=messages, tools=None, user_id=conversation_id, case_id=case_id, response_format={"type": "json_object"}, temperature=0, top_p=1)
            raw_response = str(getattr(response, "content", "") or "")
            payload = json.loads(raw_response) if raw_response else None
        except Exception as exc:
            update_observation(observation, output={"status": "failed", "error": str(exc), "raw_response": raw_response[:2000]}, metadata={"response_chars": len(raw_response), "json_parse_failed": True})
            raise QFKExtractionError("QFK_AI_PROCESSING_FAILED", f"AI 处理调用或 JSON 解析失败: {exc}") from exc
        try:
            payload = _upgrade_legacy_response(payload, candidates)
            if not isinstance(payload, dict) or payload.get("status") != "success":
                reason = payload.get("reason") if isinstance(payload, dict) else "返回不是成功结构"
                raise QFKExtractionError("QFK_AI_PROCESSING_FAILED", f"AI 无法处理：{reason or '未说明原因'}")
            validated = validate_ai_response(payload, config, candidates, value_type)
            evidence_numbers = [int(item.ref.split(":", 1)[1]) for item in validated.evidence if item.ref.startswith("line:")]
            evidence_lines = [candidates[item.ref] for item in validated.evidence]
            if mode == "extract":
                evidence_text = [item.quote for item in validated.evidence]
                if not _output_is_grounded(validated.output, evidence_text):
                    # 输出候选行摘要便于排查根因（可能是数据收集层问题导致 candidate_lines 污染）
                    candidates_preview = list(candidates.values())[:5]
                    error_msg = f"原文取值 {validated.output!r} 无法从 evidence 原文回查。候选行（前 {len(candidates_preview)} 条）：{candidates_preview}"
                    update_observation(
                        observation,
                        output={
                            "status": "failed",
                            "error_code": "QFK_AI_EXTRACT_UNGROUNDED" if legacy else "QFK_AI_PROCESSING_UNGROUNDED",
                            "error_message": error_msg,
                            "raw_response": raw_response[:2000] if raw_response else None,
                            "parsed_payload": payload,
                            "output_value": validated.output,
                            "evidence_lines": evidence_text[:5],
                            "candidate_lines": candidates_preview,
                        },
                        metadata={"validation_failed": True},
                    )
                    raise QFKExtractionError(
                        "QFK_AI_EXTRACT_UNGROUNDED" if legacy else "QFK_AI_PROCESSING_UNGROUNDED",
                        error_msg,
                    )
            update_observation(observation, output={"status": "succeeded", "output": validated.output, "evidence": [item.__dict__ for item in validated.evidence], "reason": validated.reason, "raw_response": raw_response[:2000]}, metadata={"response_chars": len(raw_response), "response_hash": hashlib.sha256(raw_response.encode("utf-8", errors="replace")).hexdigest(), "prompt_name": AI_PROCESSING_PROMPT_NAME, "prompt_revision": prompt_revision})
        except QFKExtractionError:
            raise
        except ValueError as exc:
            AI_PROCESSING_VALIDATION_FAILURES_TOTAL.labels(
                reason="response_schema", mode=mode, output_type=output_type
            ).inc()
            update_observation(observation, output={"status": "failed", "error": str(exc), "raw_response": raw_response[:2000], "parsed_payload": payload}, metadata={"response_chars": len(raw_response), "validation_failed": True})
            raise QFKExtractionError("QFK_AI_PROCESSING_INVALID_RESPONSE", str(exc)) from exc
    return AIExtractionResult(value=validated.output, raw_value=validated.output, evidence_line_numbers=evidence_numbers, evidence_lines=evidence_lines, candidate_count=len(lines), instruction=instruction, reason=validated.reason, output_type=output_type, response_hash=hashlib.sha256(raw_response.encode("utf-8", errors="replace")).hexdigest(), response_chars=len(raw_response), prompt_revision=prompt_revision)


async def extract_ai_value(
    output: str,
    spec: dict[str, Any],
    value_type: str,
    ai_client: Any,
    *,
    matcher: dict[str, Any] | None = None,
    consumer: str = "agent-service.signal.ai_processing",
    signal_type: str = "signal",
    conversation_id: str = "",
    case_id: str = "",
    run_id: str = "",
    signal_id: str = "",
    kbd_revision: int | str | None = None,
    db_session_factory: Any | None = None,
) -> AIExtractionResult:
    started = time.perf_counter()
    config = ai_processing_config(spec)
    mode = ai_processing_mode(config)
    common = {
        "conversation_id": conversation_id or None,
        "case_id": case_id or None,
        "value_type": value_type,
        "ai_mode": mode,
        "signal_type": signal_type,
        "consumer": consumer,
        "output_bytes": len(output.encode("utf-8", errors="replace")),
    }
    logger.info(event="signal_ai_processing_started", **common)
    try:
        result = await _extract_ai_value_impl(
            output,
            spec,
            value_type,
            ai_client,
            matcher=matcher,
            consumer=consumer,
            signal_type=signal_type,
            conversation_id=conversation_id,
            case_id=case_id,
            run_id=run_id,
            signal_id=signal_id,
            kbd_revision=kbd_revision,
            db_session_factory=db_session_factory,
        )
    except QFKExtractionError as exc:
        duration = time.perf_counter() - started
        QFK_AI_PROCESSINGS_TOTAL.labels(mode=mode if mode in {"extract", "derive"} else "invalid", status="failed", error_code=exc.code).inc()
        QFK_AI_PROCESSING_DURATION_SECONDS.labels(mode=mode if mode in {"extract", "derive"} else "invalid", status="failed").observe(duration)
        logger.warning(event="signal_ai_processing_failed", error_code=exc.code, error_message=str(exc), duration_ms=round(duration * 1000, 3), run_id=run_id or None, signal_id=signal_id or None, kbd_revision=kbd_revision, **common)
        raise
    duration = time.perf_counter() - started
    QFK_AI_PROCESSINGS_TOTAL.labels(mode=mode if mode in {"extract", "derive"} else "invalid", status="succeeded", error_code="").inc()
    QFK_AI_PROCESSING_DURATION_SECONDS.labels(mode=mode if mode in {"extract", "derive"} else "invalid", status="succeeded").observe(duration)
    logger.info(event="signal_ai_processing_finished", status="succeeded", candidate_count=result.candidate_count, evidence_refs=result.evidence_line_numbers, response_chars=result.response_chars, response_hash=result.response_hash, prompt_name=result.prompt_name, prompt_revision=result.prompt_revision, duration_ms=round(duration * 1000, 3), run_id=run_id or None, signal_id=signal_id or None, kbd_revision=kbd_revision, **common)
    return result
