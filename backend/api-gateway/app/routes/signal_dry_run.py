"""Admin Signal 试运行网关代理。

浏览器不能取得 Agent 内部 Token；网关固定注入内部身份并传播 W3C/响应调用链。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.observability.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/v1", tags=["signal-dry-run"])
logger = get_logger("gateway-signal-dry-run")


def _preview_result_digest(preview_body: dict) -> str:
    canonical = {key: value for key, value in preview_body.items() if key != "preview_token"}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


def _preview_input_digest(payload: dict) -> str:
    dataset = payload.get("dataset") if isinstance(payload.get("dataset"), dict) else {}
    canonical = {"source": dataset.get("source_type"), "payload": dataset.get("payload")}
    raw = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(raw).hexdigest()}"


async def _resolve_package_context(payload: dict, request: Request) -> dict:
    """把浏览器携带的 PackageSnapshot 转换为内部兼容 revision，并校验 observed CAS。"""

    package_digest = str(payload.get("package_snapshot_digest") or "")
    if not package_digest:
        return payload
    observed = str(payload.get("observed_snapshot_digest") or "")
    support_id = str(payload.get("support_id") or "")
    if observed != package_digest or not support_id:
        raise HTTPException(status_code=409, detail="工作快照身份缺失或已变化")
    headers = {"Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}"}
    trace_id = request.headers.get("X-Trace-Id")
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{settings.KB_SERVICE_URL.rstrip('/')}/api/v1/kbd/{support_id}/context",
                params={"scope": "working_draft"},
                headers=headers,
            )
    except httpx.RequestError as exc:
        raise HTTPException(status_code=503, detail="KBD 工作快照服务暂不可用") from exc
    try:
        context = response.json()
    except ValueError as exc:
        raise HTTPException(status_code=502, detail="KBD 工作快照返回无效响应") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=response.status_code, detail=context.get("detail", "KBD 工作快照不可用"))
    if context.get("package_snapshot_digest") != package_digest:
        raise HTTPException(status_code=409, detail="工作快照已变化，请刷新后重试")
    revision = context.get("source_knowledge_revision_no")
    if not isinstance(revision, int) or revision < 1:
        raise HTTPException(status_code=409, detail="工作快照缺少知识修订映射")
    resolved = dict(payload)
    resolved["kbd_revision"] = revision
    resolved["_package_context"] = context
    return resolved


async def _resolve_authoritative_dataset(payload: dict, request: Request) -> dict:
    """仅 Gateway 可将已发布 Bundle 的验证资产送入 Agent，拒绝浏览器自报 fixture/replay 内容。"""

    dataset = payload.get("dataset")
    unit_ref = payload.get("unit_ref")
    if not isinstance(dataset, dict) or not isinstance(unit_ref, dict):
        raise HTTPException(status_code=422, detail="试运行请求缺少 dataset 或 unit_ref")
    source_type = dataset.get("source_type")
    if source_type == "pasted":
        return payload
    if source_type not in {"fixture", "replay"}:
        raise HTTPException(status_code=422, detail="不支持的试运行数据来源")
    source_ref = str(dataset.get("source_ref") or "")
    bundle_digest, separator, _asset_id = source_ref.rpartition(":")
    signal_id = str(unit_ref.get("signal_id") or "")
    if not separator or not bundle_digest or not signal_id:
        raise HTTPException(status_code=422, detail="fixture/replay source_ref 必须指向已发布 Bundle 验证资产")
    headers = {"Authorization": f"Bearer {settings.HCI_SIM_CONTROL_TOKEN}"}
    trace_id = request.headers.get("X-Trace-Id")
    if trace_id:
        headers["X-Trace-Id"] = trace_id
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.get(
                f"{settings.HCI_SIM_URL.rstrip('/')}/v1/control-plane/bundles/{bundle_digest}/dry-run-datasets",
                params={"signal_id": signal_id, "source_type": source_type},
                headers=headers,
            )
    except httpx.RequestError as exc:
        logger.error(event="signal_dry_run_dataset_unavailable", source_type=source_type, bundle_digest=bundle_digest)
        raise HTTPException(status_code=503, detail="试运行数据集服务暂不可用") from exc
    if response.status_code != 200:
        raise HTTPException(status_code=422, detail="试运行数据集不存在或不再可用")
    body = response.json()
    candidates = body.get("datasets") if isinstance(body, dict) else None
    selected = next((item for item in candidates or [] if isinstance(item, dict) and item.get("source_ref") == source_ref), None)
    if selected is None:
        raise HTTPException(status_code=422, detail="试运行数据集与 Signal、Bundle 或来源不匹配")
    resolved = dict(payload)
    resolved_dataset = dict(dataset)
    resolved_dataset["dataset_id"] = selected["dataset_id"]
    resolved_dataset["source_ref"] = selected["source_ref"]
    resolved_dataset["payload"] = selected["payload"]
    resolved["dataset"] = resolved_dataset
    return resolved


def _sign_preview_result(preview_body: dict, payload: dict) -> str:
    """对已验证的 PASS 试运行结果签发 HMAC 令牌，防止浏览器篡改，支持秒级保存。"""

    token_claims = {
        "trace_id": str(preview_body.get("trace_id") or ""),
        "config_revision": str(preview_body.get("config_revision") or ""),
        "input_sha256": str(preview_body.get("input_sha256") or ""),
        "status": str(preview_body.get("status") or ""),
        "scope": str(payload.get("scope") or ""),
        "signal_id": str((payload.get("unit_ref") or {}).get("signal_id") or ""),
        "support_id": str(payload.get("support_id") or ""),
        "kbd_revision": str(payload.get("kbd_revision") or ""),
        "package_snapshot_digest": str(payload.get("package_snapshot_digest") or ""),
        "preview_result_digest": _preview_result_digest(preview_body),
        "exp": int(time.time()) + 900,
    }
    raw_claims = json.dumps(token_claims, sort_keys=True, separators=(",", ":")).encode("utf-8")
    sig = hmac.new(settings.INTERNAL_API_TOKEN.encode("utf-8"), raw_claims, hashlib.sha256).hexdigest()
    claims_b64 = base64.urlsafe_b64encode(raw_claims).decode("utf-8").rstrip("=")
    return f"{claims_b64}.{sig}"


def _verify_preview_token(token: str, preview_result: dict, dry_run_payload: dict) -> bool:
    """验证 preview_token 的完整性、有效期及与当前 dry_run 参数的一致性。"""

    if not token or "." not in token:
        return False
    claims_b64, _, sig = token.rpartition(".")
    try:
        padding = "=" * (-len(claims_b64) % 4)
        raw_claims = base64.urlsafe_b64decode((claims_b64 + padding).encode("utf-8"))
        claims = json.loads(raw_claims.decode("utf-8"))
    except Exception:
        return False
    expected_sig = hmac.new(settings.INTERNAL_API_TOKEN.encode("utf-8"), raw_claims, hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return False
    if claims.get("exp", 0) < int(time.time()):
        return False
    if claims.get("status") != "PASS":
        return False
    if claims.get("trace_id") != str(preview_result.get("trace_id") or ""):
        return False
    if not hmac.compare_digest(str(claims.get("input_sha256") or ""), _preview_input_digest(dry_run_payload)):
        return False
    if claims.get("signal_id") != str((dry_run_payload.get("unit_ref") or {}).get("signal_id") or ""):
        return False
    if str(claims.get("support_id") or "") != str(dry_run_payload.get("support_id") or ""):
        return False
    if str(claims.get("kbd_revision") or "") != str(dry_run_payload.get("kbd_revision") or ""):
        return False
    if str(claims.get("package_snapshot_digest") or "") != str(dry_run_payload.get("package_snapshot_digest") or ""):
        return False
    return hmac.compare_digest(
        str(claims.get("preview_result_digest") or ""),
        _preview_result_digest(preview_result),
    )


async def _preview(payload: dict, request: Request) -> JSONResponse:
    """以 Gateway 内部身份调用 Agent，不把服务间凭据交给浏览器。"""

    payload = await _resolve_package_context(payload, request)
    payload.pop("_package_context", None)
    payload = await _resolve_authoritative_dataset(payload, request)
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
    }
    for name in ("traceparent", "X-Trace-Id"):
        value = request.headers.get(name)
        if value:
            headers[name] = value
    try:
        async with httpx.AsyncClient(timeout=65.0) as client:
            response = await client.post(f"{settings.AGENT_SERVICE_URL}/internal/signal-dry-run", json=payload, headers=headers)
    except httpx.RequestError as exc:
        logger.error(event="signal_dry_run_upstream_unavailable", error=str(exc))
        raise HTTPException(status_code=503, detail="试运行服务暂不可用") from exc
    try:
        body = response.json()
    except ValueError:
        body = {"detail": "试运行服务返回无效响应"}
    if response.status_code == 200 and isinstance(body, dict) and body.get("status") == "PASS":
        body["preview_token"] = _sign_preview_result(body, payload)
    outgoing = JSONResponse(content=body, status_code=response.status_code)
    if trace_id := response.headers.get("X-Trace-Id"):
        outgoing.headers["X-Trace-Id"] = trace_id
    return outgoing


@router.post("/signals/dry-run")
async def proxy_signal_dry_run(request: Request) -> JSONResponse:
    """将当前草稿的只读处理请求代理到 Agent Service。"""

    payload = await request.json()
    if not isinstance(payload, dict):
        raise HTTPException(status_code=422, detail="试运行请求必须是对象")
    return await _preview(payload, request)


@router.post("/signals/dry-run/bundles/{bundle_digest}")
async def save_verified_preview_to_bundle(bundle_digest: str, request: Request) -> JSONResponse:
    """追加验证资产；优先使用已签名的 PASS 结果以秒级保存，签名无效时重新执行。"""

    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("dry_run"), dict):
        raise HTTPException(status_code=422, detail="dry_run 请求快照必填")
    dry_run = body["dry_run"]
    dry_run = await _resolve_package_context(dry_run, request)
    dry_run.pop("_package_context", None)
    dry_run = await _resolve_authoritative_dataset(dry_run, request)
    preview_token = body.get("preview_token")
    preview_result = body.get("preview_result")

    # 优先校验签名 Token，命中则直接秒级使用已验证的试运行结果，避免重复耗时调用大模型
    if (
        isinstance(preview_token, str)
        and isinstance(preview_result, dict)
        and _verify_preview_token(preview_token, preview_result, dry_run)
    ):
        preview_body = preview_result
    else:
        preview_response = await _preview(dry_run, request)
        preview_body = json.loads(preview_response.body)
        if preview_response.status_code != 200:
            return preview_response
        if preview_body.get("status") != "PASS":
            raise HTTPException(status_code=409, detail="只有 PASS 试运行结果可以保存到 Bundle 草稿")

    dataset = dry_run.get("dataset") if isinstance(dry_run.get("dataset"), dict) else {}
    unit_ref = dry_run.get("unit_ref") if isinstance(dry_run.get("unit_ref"), dict) else {}
    payload_data = dataset.get("raw_input") if dataset.get("raw_input") is not None else dataset.get("payload")
    asset = {
        "asset_id": f"va-{uuid.uuid4()}",
        "support_id": dry_run.get("support_id"),
        "kbd_revision": dry_run.get("kbd_revision"),
        "signal_id": unit_ref.get("signal_id"),
        "scope": dry_run.get("scope"),
        "source_type": dataset.get("source_type"),
        "payload": payload_data,
        "result_status": preview_body.get("status"),
        "config_revision": preview_body.get("config_revision"),
        "trace_id": preview_body.get("trace_id"),
    }
    headers = {
        "Authorization": f"Bearer {settings.HCI_SIM_CONTROL_TOKEN}",
        "Content-Type": "application/json",
        "X-HCI-Sim-Actor-ID": settings.HCI_SIM_EXPERT_EDITOR_ACTOR_ID,
        "X-HCI-Sim-Actor-Role": "expert",
        "X-Trace-ID": str(preview_body.get("trace_id") or uuid.uuid4().hex),
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{settings.HCI_SIM_URL.rstrip('/')}/v1/control-plane/bundles/{bundle_digest}/verification-assets",
                json={"asset": asset, "reason": "保存 Signal 试运行验证资产"}, headers=headers,
            )
    except httpx.RequestError as exc:
        logger.error(event="verification_asset_upstream_unavailable", error=str(exc), bundle_digest=bundle_digest)
        raise HTTPException(status_code=503, detail="Bundle 控制面暂不可用") from exc
    try:
        result = response.json()
    except ValueError:
        result = {"detail": "Bundle 控制面返回无效响应"}
    return JSONResponse(content=result, status_code=response.status_code)


@router.post("/signals/dry-run/verification-assets")
async def save_verified_preview_to_package(request: Request) -> JSONResponse:
    """把已签名 PASS 结果保存为当前 Package 工作稿的不可变验证凭证。"""

    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("dry_run"), dict):
        raise HTTPException(status_code=422, detail="dry_run 请求快照必填")
    dry_run = await _resolve_package_context(body["dry_run"], request)
    package_context = dry_run.pop("_package_context", None)
    if not isinstance(package_context, dict):
        raise HTTPException(status_code=422, detail="PackageSnapshot 身份必填")
    dry_run = await _resolve_authoritative_dataset(dry_run, request)
    preview_token = body.get("preview_token")
    preview_result = body.get("preview_result")
    if (
        isinstance(preview_token, str)
        and isinstance(preview_result, dict)
        and _verify_preview_token(preview_token, preview_result, dry_run)
    ):
        preview_body = preview_result
    else:
        preview_response = await _preview(dry_run, request)
        preview_body = json.loads(preview_response.body)
        if preview_response.status_code != 200:
            return preview_response
        if preview_body.get("status") != "PASS":
            raise HTTPException(status_code=409, detail="只有 PASS 试运行结果可以保存为验证凭证")

    dataset = dry_run.get("dataset") if isinstance(dry_run.get("dataset"), dict) else {}
    unit_ref = dry_run.get("unit_ref") if isinstance(dry_run.get("unit_ref"), dict) else {}
    raw_response = preview_body.get("ai_raw_response")
    raw_response_hash = None
    if isinstance(raw_response, dict):
        encoded = json.dumps(raw_response, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()
        raw_response_hash = f"sha256:{hashlib.sha256(encoded).hexdigest()}"
    processing_index = unit_ref.get("processing_index", unit_ref.get("produce_index", 0))
    verification_payload = {
        "observed_snapshot_digest": package_context.get("package_snapshot_digest"),
        "signal_id": unit_ref.get("signal_id"),
        "processing_index": processing_index if isinstance(processing_index, int) else 0,
        "dataset_id": dataset.get("dataset_id"),
        "input_digest": preview_body.get("input_sha256"),
        "deterministic_input": {
            "source_type": dataset.get("source_type"),
            "source_ref": dataset.get("source_ref"),
            "payload": dataset.get("payload"),
        },
        "ai_input": {
            "verification_scope": dry_run.get("verification_scope"),
            "signal": dry_run.get("signal"),
        },
        "raw_response_hash": raw_response_hash,
        "output_json": {
            "value": preview_body.get("value"),
            "matcher": preview_body.get("matcher"),
            "derivation": preview_body.get("derivation"),
        },
        "evidence_json": {
            "evidence": preview_body.get("evidence"),
            "evidence_lines": preview_body.get("evidence_lines") or [],
        },
        "downstream_result": {},
        "model": str(raw_response.get("model") or "deterministic") if isinstance(raw_response, dict) else "deterministic",
        "prompt_revision": package_context.get("prompt_revision"),
        "contract_version": preview_body.get("config_revision"),
        "run_id": preview_body.get("trace_id"),
        "result_status": "pass",
        "knowledge_snapshot_digest": package_context.get("knowledge_snapshot_digest"),
        "signal_spec_digest": package_context.get("signal_spec_digest"),
        "simulation_spec_digest": package_context.get("simulation_spec_digest"),
        "tool_contract_revision": package_context.get("tool_contract_revision"),
        "policy_revision": package_context.get("policy_revision"),
        "compiler_revision": package_context.get("compiler_revision"),
        "actor_id": settings.HCI_SIM_EXPERT_EDITOR_ACTOR_ID,
    }
    trace_id = str(preview_body.get("trace_id") or uuid.uuid4().hex)
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
        "X-Trace-ID": trace_id,
    }
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                f"{settings.KB_SERVICE_URL.rstrip('/')}/api/v1/kbd/{dry_run.get('support_id')}/working-draft/verification-assets",
                json=verification_payload,
                headers=headers,
            )
    except httpx.RequestError as exc:
        logger.error(event="package_verification_asset_upstream_unavailable", error=str(exc), trace_id=trace_id)
        raise HTTPException(status_code=503, detail="KBD 验证资产服务暂不可用") from exc
    try:
        result = response.json()
    except ValueError:
        result = {"detail": "KBD 验证资产服务返回无效响应"}
    return JSONResponse(content=result, status_code=response.status_code)
