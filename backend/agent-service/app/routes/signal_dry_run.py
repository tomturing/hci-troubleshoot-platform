"""Signal 执行结果试运行内部 API。

试运行只处理调用方已获得的离线输出，严禁在此模块调用 Handler、Terminal Bridge
或任意 acquisition。QFK/QKV 的判定仍委托现有正式处理函数，避免浏览器或另一套
服务复制 Matcher、取值、AI 处理语义。
"""

from __future__ import annotations

import hashlib
import json
import time
import uuid
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, model_validator
from shared.observability.logger import get_logger
from shared.observability.metrics import SIGNAL_DRY_RUN_DURATION_SECONDS, SIGNAL_DRY_RUN_TOTAL
from shared.signals.ai_extractor import extract_ai_value, has_ai_extract
from shared.signals.ai_processing import ai_item_type, ai_output_type, ai_processing_config
from shared.signals.extractor import QFKExtractionError
from shared.signals.matcher import evaluate_matcher
from shared.signals.qkv_output_processing import QKVProcessingError, apply_output_processing_async

from app.config import settings

router = APIRouter(prefix="/internal", tags=["signal-dry-run"])
logger = get_logger("signal-dry-run")

MAX_PREVIEW_BYTES = 1024 * 1024


class PreviewDataset(BaseModel):
    """试运行输入只允许三种独立来源，不接受现场命令或 URL。"""

    dataset_id: str = Field(min_length=1, max_length=128)
    source_type: Literal["pasted", "fixture", "replay"]
    source_ref: str = Field(min_length=1, max_length=256)
    payload: str | list[dict[str, Any]] | dict[str, Any]

    @model_validator(mode="after")
    def validate_payload(self) -> PreviewDataset:
        if self.source_type == "pasted" and self.source_ref != "user-input":
            raise ValueError("临时样本的 source_ref 必须是 user-input")
        if self.source_type in {"fixture", "replay"} and not self.source_ref.startswith("sha256:"):
            raise ValueError("fixture/replay source_ref 必须绑定已发布 Bundle digest")
        encoded = json.dumps(self.payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        if len(encoded) > MAX_PREVIEW_BYTES:
            raise ValueError(f"试运行输入超过 {MAX_PREVIEW_BYTES} 字节上限")
        return self


class UnitRef(BaseModel):
    signal_id: str = Field(min_length=1, max_length=128)
    processing_index: int | None = Field(default=None, ge=0, le=1000)
    produce_index: int | None = Field(default=None, ge=0, le=1000)


class SignalDryRunRequest(BaseModel):
    """浏览器提交的是当前草稿快照，服务端重新计算 config_revision。"""

    draft_revision: str = Field(min_length=1, max_length=71)
    scope: Literal["qfk_execution_result", "qkv_variable_processing"]
    unit_ref: UnitRef
    verification_scope: Literal["signal", "ai_step"] = "signal"
    dataset: PreviewDataset
    signal: dict[str, Any]
    support_id: str = Field(min_length=1, max_length=64)
    kbd_revision: int = Field(ge=1)
    package_snapshot_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")
    observed_snapshot_digest: str | None = Field(default=None, pattern=r"^sha256:[0-9a-f]{64}$")

    @model_validator(mode="after")
    def validate_snapshot_cas(self) -> SignalDryRunRequest:
        if self.package_snapshot_digest != self.observed_snapshot_digest:
            raise ValueError("package_snapshot_digest 与 observed_snapshot_digest 必须一致")
        return self


class SignalDryRunResult(BaseModel):
    trace_id: str
    dataset_id: str
    unit_ref: UnitRef
    verification_scope: Literal["signal", "ai_step"]
    config_revision: str
    status: Literal["PASS", "FAIL", "UNKNOWN"]
    input_sha256: str
    value: Any = None
    matcher: dict[str, Any] | None = None
    evidence: str = ""
    evidence_lines: list[int] = Field(default_factory=list)
    derivation: dict[str, Any] = Field(default_factory=dict)
    error_code: str | None = None
    error_message: str | None = None
    ai_raw_response: dict[str, Any] | None = None


def _check_internal_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header.split(" ", 1)[1] != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="内部 Token 无效")


def _canonical_hash(value: Any) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _trace_id(request: Request) -> str:
    candidate = request.headers.get("X-Trace-Id", "").strip().lower()
    if len(candidate) == 32 and all(char in "0123456789abcdef" for char in candidate):
        return candidate
    return uuid.uuid4().hex


def _signal_id(signal: dict[str, Any]) -> str:
    value = str(signal.get("id") or "").strip()
    if not value:
        raise ValueError("草稿 Signal 缺少 id")
    return value


def _qfk_output(dataset: PreviewDataset, matcher: dict[str, Any] | None) -> str:
    if not isinstance(dataset.payload, str):
        raise ValueError("QFK 试运行输入必须是完整 stdout/stderr 文本")
    extract = matcher.get("extract") if isinstance(matcher, dict) else None
    # QFK 的 extract.source 表示正式执行中的 stdout/stderr 通道；临时输入在 UI 中
    # 已明确标为完整输出，当前以同一文本模拟该受限通道，不能改为执行命令补齐。
    _ = extract
    return dataset.payload


async def _evaluate_qfk(body: SignalDryRunRequest, *, ai_client: Any | None, db_session_factory: Any | None, trace_id: str) -> SignalDryRunResult:
    signal = body.signal
    acquire = signal.get("acquire")
    if not isinstance(acquire, dict) or not str(acquire.get("tool") or "").startswith("qfk_"):
        raise ValueError("scope=qfk_execution_result 必须绑定 QFK Signal")
    if _signal_id(signal) != body.unit_ref.signal_id:
        raise ValueError("unit_ref.signal_id 与草稿 Signal 不一致")
    matcher = signal.get("match")
    produces = ((signal.get("orchestrate") or {}).get("produces") or []) if isinstance(signal.get("orchestrate"), dict) else []
    output = _qfk_output(body.dataset, matcher if isinstance(matcher, dict) else None)
    input_sha = _canonical_hash({"source": body.dataset.source_type, "payload": output})

    if isinstance(matcher, dict):
        extract = matcher.get("extract")
        if body.verification_scope == "ai_step" and not has_ai_extract(extract):
            raise ValueError("AI_STEP_TARGET_REQUIRED: match.extract 未配置 ai_processing")
        matcher_input = output
        signal_matcher = matcher
        precomputed_values: list[float] | None = None
        precomputed_detail: dict[str, Any] | None = None
        evidence_lines: list[int] = []
        value: Any = None
        ai_raw_response: dict[str, Any] | None = None
        if has_ai_extract(extract):
            if ai_client is None:
                raise RuntimeError("QFK_AI_EXTRACT_UNAVAILABLE: 当前 Agent 未初始化 AI 客户端")
            config = ai_processing_config(extract) or {}
            configured_type = ai_output_type(config, "string")
            value_type = "array<number>" if configured_type == "array" and ai_item_type(config) == "number" else configured_type
            ai_result = await extract_ai_value(
                output,
                extract,
                value_type,
                ai_client,
                matcher=matcher,
                consumer="agent-service.signal_dry_run.ai_processing",
                signal_type="dry_run",
                conversation_id=f"dry-run:{trace_id}",
                signal_id=body.unit_ref.signal_id,
                kbd_revision=body.kbd_revision,
                db_session_factory=db_session_factory,
            )
            value = ai_result.value
            ai_raw_response = getattr(ai_result, "raw_response", None)
            evidence_lines = ai_result.evidence_line_numbers
            precomputed_detail = {
                "extract": {
                    "status": "ok",
                    "value_source": "ai_grounded",
                    "ai_value": ai_result.value,
                    "evidence_line_numbers": evidence_lines,
                    "evidence_lines": ai_result.evidence_lines,
                    "candidate_count": ai_result.candidate_count,
                    "prompt_revision": ai_result.prompt_revision,
                }
            }
            if configured_type == "number":
                precomputed_values = [float(ai_result.value)]
            elif configured_type == "array" and ai_item_type(config) == "number":
                precomputed_values = [float(item) for item in ai_result.value]
            else:
                matcher_input = json.dumps(ai_result.value, ensure_ascii=False) if isinstance(ai_result.value, (list, dict)) else str(ai_result.value)
                signal_matcher = dict(matcher)
                signal_matcher["extract"] = {"type": "text", "rows": {"mode": "all"}, "cardinality": "exactly_one", "source": "stdout"}
            # ai_step 只验证 AI 契约本身；最终 Matcher 属于 signal 范围，不能混入该结果。
            if body.verification_scope == "ai_step":
                return SignalDryRunResult(
                    trace_id=trace_id, dataset_id=body.dataset.dataset_id, unit_ref=body.unit_ref,
                    verification_scope=body.verification_scope, config_revision=body.draft_revision,
                    status="PASS", input_sha256=input_sha, value=ai_result.value,
                    evidence=ai_result.reason, evidence_lines=evidence_lines,
                    derivation={"ai_contract": precomputed_detail["extract"]},
                    ai_raw_response=ai_raw_response,
                )
        result = evaluate_matcher(signal_matcher, matcher_input, precomputed_values=precomputed_values, precomputed_detail=precomputed_detail)
        status = "UNKNOWN" if result.matched is None else ("PASS" if result.matched else "FAIL")
        return SignalDryRunResult(
            trace_id=trace_id, dataset_id=body.dataset.dataset_id, unit_ref=body.unit_ref,
            verification_scope=body.verification_scope, config_revision=body.draft_revision,
            status=status, input_sha256=input_sha, value=value, matcher=result.detail,
            evidence=result.evidence, evidence_lines=evidence_lines,
            ai_raw_response=ai_raw_response,
        )

    if not isinstance(produces, list) or not produces:
        raise ValueError("QFK Signal 必须配置 match 或 orchestrate.produces")

    if body.verification_scope == "ai_step":
        target_produce = None
        target_index = body.unit_ref.produce_index if body.unit_ref.produce_index is not None else body.unit_ref.processing_index
        if target_index is not None and 0 <= target_index < len(produces):
            candidate = produces[target_index]
            if isinstance(candidate, dict) and has_ai_extract(candidate.get("extract")):
                target_produce = candidate
            else:
                raise ValueError(f"AI_STEP_TARGET_REQUIRED: produces[{target_index}] 未配置 ai_processing")
        else:
            for p in produces:
                if isinstance(p, dict) and has_ai_extract(p.get("extract")):
                    target_produce = p
                    break
        if target_produce is None:
            raise ValueError("AI_STEP_TARGET_REQUIRED: produces 未配置 ai_processing")

        extract = target_produce.get("extract")
        if not isinstance(extract, dict):
            raise ValueError("produces 中的目标 AI 处理缺少 extract")
        if ai_client is None:
            raise RuntimeError("QFK_AI_EXTRACT_UNAVAILABLE: 当前 Agent 未初始化 AI 客户端")
        ai_result = await extract_ai_value(
            output,
            extract,
            str(target_produce.get("type") or "string"),
            ai_client,
            consumer="agent-service.signal_dry_run.ai_processing",
            signal_type="dry_run",
            conversation_id=f"dry-run:{trace_id}",
            signal_id=body.unit_ref.signal_id,
            kbd_revision=body.kbd_revision,
            db_session_factory=db_session_factory,
        )
        return SignalDryRunResult(
            trace_id=trace_id, dataset_id=body.dataset.dataset_id, unit_ref=body.unit_ref,
            verification_scope=body.verification_scope, config_revision=body.draft_revision,
            status="PASS", input_sha256=input_sha, value=ai_result.value,
            evidence=ai_result.reason, evidence_lines=ai_result.evidence_line_numbers,
            derivation={"ai_contract": {"produce": target_produce.get("name"), "value_source": "ai_grounded"}},
            ai_raw_response=getattr(ai_result, "raw_response", None),
        )

    values: dict[str, Any] = {}
    ai_raw_responses: list[dict[str, Any]] = []
    evidence_lines: list[int] = []
    ai_contracts: list[dict[str, Any]] = []
    from shared.signals.extractor import extract_value

    for index, produce in enumerate(produces):
        if not isinstance(produce, dict) or not str(produce.get("name") or "").strip():
            raise ValueError(f"produces[{index}] 缺少 name")
        extract = produce.get("extract")
        if not isinstance(extract, dict):
            raise ValueError(f"produces[{index}] 缺少 extract")
        if has_ai_extract(extract):
            if ai_client is None:
                raise RuntimeError("QFK_AI_EXTRACT_UNAVAILABLE: 当前 Agent 未初始化 AI 客户端")
            ai_result = await extract_ai_value(
                output,
                extract,
                str(produce.get("type") or "string"),
                ai_client,
                consumer="agent-service.signal_dry_run.ai_processing",
                signal_type="dry_run",
                conversation_id=f"dry-run:{trace_id}",
                signal_id=body.unit_ref.signal_id,
                kbd_revision=body.kbd_revision,
                db_session_factory=db_session_factory,
            )
            values[str(produce["name"])] = ai_result.value
            raw = getattr(ai_result, "raw_response", None)
            if isinstance(raw, dict):
                ai_raw_responses.append(raw)
            if ai_result.evidence_line_numbers:
                evidence_lines.extend(ai_result.evidence_line_numbers)
            ai_contracts.append({
                "produce": produce.get("name"),
                "value_source": "ai_grounded",
                "reason": ai_result.reason,
            })
        else:
            values[str(produce["name"])] = extract_value(
                output, extract, str(produce.get("type") or "string")
            )
    sorted_evidence_lines = sorted(set(evidence_lines))
    primary_ai_raw_response = ai_raw_responses[-1] if ai_raw_responses else None
    derivation_payload: dict[str, Any] = {"produces": list(values.keys())}
    if ai_contracts:
        derivation_payload["ai_contracts"] = ai_contracts

    return SignalDryRunResult(
        trace_id=trace_id, dataset_id=body.dataset.dataset_id, unit_ref=body.unit_ref,
        verification_scope=body.verification_scope, config_revision=body.draft_revision,
        status="PASS", input_sha256=input_sha, value=values,
        evidence="QFK 产出变量已按当前草稿完成提取。",
        evidence_lines=sorted_evidence_lines,
        derivation=derivation_payload,
        ai_raw_response=primary_ai_raw_response,
    )


def _normalize_qkv_records(payload: Any) -> list[dict[str, Any]]:
    """自适应解析与归一化 QKV 试运行输入为 records 字典列表。"""
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception as exc:
            raise ValueError("QKV 试运行输入必须是合法 JSON (records 数组或包含 data/items 的对象)") from exc
    if isinstance(payload, dict):
        items = payload.get("data") or payload.get("items")
        payload = items if isinstance(items, list) else [payload]
    if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
        raise ValueError("QKV 试运行输入必须是已投影变量 JSON records")
    return payload


async def _evaluate_qkv(body: SignalDryRunRequest, *, ai_client: Any | None, db_session_factory: Any | None, trace_id: str) -> SignalDryRunResult:
    signal = body.signal
    acquire = signal.get("acquire")
    if not isinstance(acquire, dict) or not str(acquire.get("tool") or "").startswith("qkv_"):
        raise ValueError("scope=qkv_variable_processing 必须绑定 QKV Signal")
    if _signal_id(signal) != body.unit_ref.signal_id:
        raise ValueError("unit_ref.signal_id 与草稿 Signal 不一致")

    from app.tools.qkv.parser import _extract_by_produces

    records = _normalize_qkv_records(body.dataset.payload)
    orchestrate = signal.get("orchestrate") if isinstance(signal.get("orchestrate"), dict) else {}
    processing = orchestrate.get("output_processing") or []
    produces = orchestrate.get("produces") or []

    # 1. 前置变量投影：若配置了 produces，先按声明提取有效变量（与生产环境 parser 对齐）
    if isinstance(produces, list) and produces:
        extracted = _extract_by_produces(records, produces)
        if not extracted:
            return SignalDryRunResult(
                trace_id=trace_id, dataset_id=body.dataset.dataset_id, unit_ref=body.unit_ref,
                verification_scope=body.verification_scope, config_revision=body.draft_revision,
                status="FAIL", input_sha256=_canonical_hash({"source": body.dataset.source_type, "payload": body.dataset.payload}),
                value=[],
                evidence="输入数据中未能按 produces 规格提取出任何有效变量。",
                derivation={"produces": [p.get("name") for p in produces if isinstance(p, dict) and p.get("name")]},
            )
        records = extracted

    # 2. 配置了 output_processing：在已投影变量上执行确定性/AI 后处理流水线
    if isinstance(processing, list) and processing:
        end_index = body.unit_ref.processing_index if body.unit_ref.processing_index is not None else len(processing) - 1
        if end_index >= len(processing):
            raise ValueError("processing_index 超出当前草稿范围")
        if body.verification_scope == "ai_step":
            target = processing[end_index]
            if not isinstance(target, dict) or target.get("mode") != "derive" or not ai_processing_config(target.get("extract")):
                raise ValueError("AI_STEP_TARGET_REQUIRED: ai_step 必须绑定带 ai_processing 的 derive 单元")
            if any(
                isinstance(item, dict) and item.get("mode") == "derive" and ai_processing_config(item.get("extract"))
                for item in processing[:end_index]
            ):
                raise ValueError("AI_STEP_DEPENDENCY_UNSUPPORTED: ai_step 的前序依赖必须是确定性 derive")
            selected = [item for item in processing[: end_index + 1] if isinstance(item, dict) and item.get("mode") == "derive"]
        else:
            selected = processing[: end_index + 1]
        processed = await apply_output_processing_async(
            records,
            selected,
            ai_client=ai_client,
            conversation_id=f"dry-run:{trace_id}",
            db_session_factory=db_session_factory,
        )
        statuses = [item.status for item in processed.assertions]
        status = "UNKNOWN" if "UNKNOWN" in statuses else ("FAIL" if "FAIL" in statuses else "PASS")
        return SignalDryRunResult(
            trace_id=trace_id, dataset_id=body.dataset.dataset_id, unit_ref=body.unit_ref,
            verification_scope=body.verification_scope, config_revision=body.draft_revision,
            status=status, input_sha256=_canonical_hash({"source": body.dataset.source_type, "payload": body.dataset.payload}),
            value=processed.records,
            derivation={"assertions": [item.__dict__ for item in processed.assertions], "processing_end_index": end_index},
            evidence="QKV 已投影 records 已按当前草稿及前序处理单元完成只读处理。",
        )

    # 3. 纯 Producer：仅配置 produces 变量提取规格
    if isinstance(produces, list) and produces:
        if body.verification_scope == "ai_step":
            raise ValueError("AI_STEP_TARGET_REQUIRED: QKV 纯生产者未配置 AI 处理单元")
        produced_keys = list(records[0].keys()) if records else []
        return SignalDryRunResult(
            trace_id=trace_id, dataset_id=body.dataset.dataset_id, unit_ref=body.unit_ref,
            verification_scope=body.verification_scope, config_revision=body.draft_revision,
            status="PASS", input_sha256=_canonical_hash({"source": body.dataset.source_type, "payload": body.dataset.payload}),
            value=records,
            evidence="QKV 产出变量已按当前草稿 produces 规格完成只读提取。",
            derivation={"produces": produced_keys, "record_count": len(records)},
        )

    raise ValueError("QKV Signal 必须配置 orchestrate.produces 或 orchestrate.output_processing")


async def evaluate_signal_dry_run(body: SignalDryRunRequest, *, ai_client: Any | None, trace_id: str, db_session_factory: Any | None = None) -> SignalDryRunResult:
    """领域入口，供 HTTP 路由与单元测试共用。"""

    computed_revision = _canonical_hash(body.signal)
    if body.draft_revision != computed_revision:
        raise ValueError("DRAFT_REVISION_MISMATCH: 草稿已变更，请重新试运行")
    if body.scope == "qfk_execution_result":
        return await _evaluate_qfk(body, ai_client=ai_client, db_session_factory=db_session_factory, trace_id=trace_id)
    return await _evaluate_qkv(body, ai_client=ai_client, db_session_factory=db_session_factory, trace_id=trace_id)


@router.post("/signal-dry-run", response_model=SignalDryRunResult)
async def signal_dry_run(request: Request, body: SignalDryRunRequest) -> SignalDryRunResult:
    """执行一次受版本和数据集绑定的只读 Signal 后处理。"""

    _check_internal_auth(request)
    trace_id = _trace_id(request)
    started = time.perf_counter()
    status, error_code = "UNKNOWN", ""
    try:
        ai_registry = getattr(request.app.state, "ai_registry", None)
        ai_client = ai_registry.get_client("htp-agent") if ai_registry is not None else None
        db_session_factory = getattr(request.app.state, "db_session_factory", None)
        result = await evaluate_signal_dry_run(body, ai_client=ai_client, db_session_factory=db_session_factory, trace_id=trace_id)
        status = result.status
        logger.info(
            event="signal_dry_run_completed", trace_id=trace_id, scope=body.scope,
            verification_scope=body.verification_scope, support_id=body.support_id,
            kbd_revision=body.kbd_revision, package_snapshot_digest=body.package_snapshot_digest,
            signal_id=body.unit_ref.signal_id,
            dataset_id=body.dataset.dataset_id, input_sha256=result.input_sha256, status=status,
        )
        return result
    except (QFKExtractionError, QKVProcessingError) as exc:
        error_code = exc.code
        raw_response = getattr(exc, "raw_response", None)
        logger.warning(
            event="signal_dry_run_rejected",
            trace_id=trace_id,
            scope=body.scope,
            error_code=error_code,
            has_raw_response=bool(raw_response),
        )
        status_code = 503 if error_code == "QFK_AI_PROCESSING_PROMPT_UNAVAILABLE" else 422
        raise HTTPException(
            status_code=status_code,
            detail={
                "code": error_code,
                "message": str(exc),
                "trace_id": trace_id,
                "ai_raw_response": raw_response,
            },
        ) from exc
    except ValueError as exc:
        error_code = str(exc).split(":", 1)[0] if ":" in str(exc) else "SIGNAL_DRY_RUN_INVALID"
        logger.warning(event="signal_dry_run_rejected", trace_id=trace_id, scope=body.scope, error_code=error_code)
        raise HTTPException(status_code=422, detail={"code": error_code, "message": str(exc), "trace_id": trace_id}) from exc
    except Exception as exc:
        error_code = getattr(exc, "code", "SIGNAL_DRY_RUN_FAILED")
        logger.exception(event="signal_dry_run_failed", trace_id=trace_id, scope=body.scope, error_code=error_code)
        raise HTTPException(status_code=500, detail={"code": error_code, "message": "试运行执行失败", "trace_id": trace_id}) from exc
    finally:
        SIGNAL_DRY_RUN_TOTAL.labels(body.scope, body.verification_scope, status, error_code).inc()
        SIGNAL_DRY_RUN_DURATION_SECONDS.labels(body.scope, status).observe(time.perf_counter() - started)
