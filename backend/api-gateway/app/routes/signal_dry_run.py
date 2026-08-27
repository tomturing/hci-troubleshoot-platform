"""Admin Signal 试运行网关代理。

浏览器不能取得 Agent 内部 Token；网关固定注入内部身份并传播 W3C/响应调用链。
"""

from __future__ import annotations

import json
import uuid

import httpx
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse
from shared.observability.logger import get_logger

from app.config import settings

router = APIRouter(prefix="/api/v1", tags=["signal-dry-run"])
logger = get_logger("gateway-signal-dry-run")


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


async def _preview(payload: dict, request: Request) -> JSONResponse:
    """以 Gateway 内部身份调用 Agent，不把服务间凭据交给浏览器。"""

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
    """重新执行 dry-run 后追加验证资产，浏览器提交的预览结果不具备信任权。"""

    body = await request.json()
    if not isinstance(body, dict) or not isinstance(body.get("dry_run"), dict):
        raise HTTPException(status_code=422, detail="dry_run 请求快照必填")
    preview_response = await _preview(body["dry_run"], request)
    preview_body = json.loads(preview_response.body)
    if preview_response.status_code != 200:
        return preview_response
    if preview_body.get("status") != "PASS":
        raise HTTPException(status_code=409, detail="只有 PASS 试运行结果可以保存到 Bundle 草稿")
    dry_run = body["dry_run"]
    dataset = dry_run.get("dataset") if isinstance(dry_run.get("dataset"), dict) else {}
    unit_ref = dry_run.get("unit_ref") if isinstance(dry_run.get("unit_ref"), dict) else {}
    asset = {
        "asset_id": f"va-{uuid.uuid4()}",
        "support_id": dry_run.get("support_id"),
        "kbd_revision": dry_run.get("kbd_revision"),
        "signal_id": unit_ref.get("signal_id"),
        "scope": dry_run.get("scope"),
        "source_type": dataset.get("source_type"),
        "payload": dataset.get("payload"),
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
