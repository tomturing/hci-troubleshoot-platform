"""qkv_vm_console 内部 API（设计文档 §5.3）。

内部契约，不直接暴露给浏览器或客户；Bearer = INTERNAL_API_TOKEN。

端点：
- POST /internal/vm-console/captures                      创建并执行基线截图
- POST /internal/vm-console/captures/{id}/wake-and-recapture
                                                          校验一次性确认令牌后唤醒重截
- GET  /internal/vm-console/captures/{id}                 状态/制品/视觉/审计摘要
- POST /internal/vm-console/captures/{id}/analyze         在已验证制品上重试视觉提取
"""

from __future__ import annotations

import hashlib
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.tools.vm_console import store

router = APIRouter(prefix="/internal/vm-console", tags=["vm-console"])


class CaptureCreateRequest(BaseModel):
    """输入仅为受限上下文标识；不接受任何命令/路径/按键字段。"""

    model_config = {"extra": "forbid"}

    case_id: str
    host_ref: str
    vm_ref: str
    signal_id: str | None = None
    conversation_id: str | None = None
    diagnosis_run_id: str | None = None
    node_ip: str | None = None
    timeout: int = Field(default=60, ge=1, le=60)


class WakeRequest(BaseModel):
    model_config = {"extra": "forbid"}

    wake_token: str = Field(..., min_length=8, max_length=128)
    confirmed_by: str = "interactive_user"


def _check_internal_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer ") or auth_header.split(" ", 1)[1] != settings.INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="内部 Token 无效")


def _session_factory(request: Request) -> Any:
    factory = getattr(request.app.state, "db_session_factory", None)
    if factory is None:
        raise HTTPException(status_code=503, detail="数据库会话不可用")
    return factory


@router.post("/captures")
async def create_capture(payload: CaptureCreateRequest, request: Request) -> dict[str, Any]:
    """创建并执行基线截图（编排委托专用适配器；本端点是受控入口）。"""

    _check_internal_auth(request)
    from app.tools.vm_console.adapter import capture_enabled, run_vm_console_signal

    if not capture_enabled():
        raise HTTPException(status_code=403, detail="VM_CONSOLE_DISABLED_BY_POLICY")

    signal = {
        "id": payload.signal_id or "",
        "acquire": {
            "tool": "qkv_vm_console",
            "args": {
                "host": payload.host_ref,
                "vm_id": payload.vm_ref,
                "capture_mode": "baseline_then_optional_wake",
                "timeout": payload.timeout,
            },
        },
        "orchestrate": {"produces": []},
    }
    result = await run_vm_console_signal(
        signal,
        {"HOST": payload.host_ref, "VM_ID": payload.vm_ref, "node_ip": payload.node_ip or ""},
        conversation_id=payload.conversation_id or "",
        case_id=payload.case_id,
        session_id=payload.diagnosis_run_id or "",
        db_session_factory=_session_factory(request),
        scp_client=None,
    )
    return {
        "ok": result.success,
        "capture_id": result.capture_id,
        "exec_id": result.exec_id,
        "error_code": result.error_code,
        "error": result.error,
        "values": result.values,
    }


@router.post("/captures/{capture_id}/wake-and-recapture")
async def wake_and_recapture(capture_id: str, payload: WakeRequest, request: Request) -> dict[str, Any]:
    """校验一次性确认令牌后执行固定 wake_down_key 与重截图。

    令牌原子消费：重放/重复/超时一律 409。
    """

    _check_internal_auth(request)
    factory = _session_factory(request)

    wake_token_hash = hashlib.sha256(payload.wake_token.encode()).hexdigest()
    async with factory() as session:
        record = await store.get_capture_record(session, capture_id)
        if record is None:
            raise HTTPException(status_code=404, detail="截图会话不存在")
        consumed = await store.consume_wake_token(
            session, capture_id, wake_token_hash, payload.confirmed_by
        )
    if not consumed:
        raise HTTPException(status_code=409, detail="WAKE_TOKEN_REUSED_OR_EXPIRED")

    from shared.resolution.vm_console import build_wake_intent
    from shared.utils.internal_http import InternalHTTPClient

    from app.tools.vm_console.adapter import run_wake_and_recapture

    intent = build_wake_intent(str(record["host_node_id"]), str(record["vm_id"]))
    http_client = InternalHTTPClient(service_name="conversation-service")
    outcome = await run_wake_and_recapture(
        http_client,
        db_session_factory=factory,
        capture_id=capture_id,
        conversation_id=str(record.get("conversation_id") or ""),
        case_id=str(record.get("case_id") or ""),
        host_node_id=str(record["host_node_id"]),
        vm_id=str(record["vm_id"]),
        node_ip=None,
        timeout_seconds=60,
        intent=intent,
        trace_id=str(record.get("trace_id") or ""),
    )
    return {"ok": outcome.get("success", False), "artifact_id": outcome.get("artifact_id"), "error": outcome.get("error")}


@router.get("/captures/{capture_id}")
async def get_capture(capture_id: str, request: Request) -> dict[str, Any]:
    """获取结构化状态、制品引用、视觉结果和审计摘要。"""

    _check_internal_auth(request)
    async with _session_factory(request) as session:
        record = await store.get_capture_record(session, capture_id)
        artifact = await store.get_artifact_by_capture(session, capture_id, kind="ppm")
    if record is None:
        raise HTTPException(status_code=404, detail="截图会话不存在")
    # 原图不经此端点返回；仅返回制品引用与脱敏摘要。
    record.pop("target_verification", None)
    return {"capture": _json_safe(record), "artifact": _json_safe(artifact) if artifact else None}


@router.post("/captures/{capture_id}/analyze")
async def analyze_capture(capture_id: str, request: Request) -> dict[str, Any]:
    """仅在已验证的制品上启动/重试视觉提取。"""

    _check_internal_auth(request)
    factory = _session_factory(request)

    async with factory() as session:
        record = await store.get_capture_record(session, capture_id)
        if record is None:
            raise HTTPException(status_code=404, detail="截图会话不存在")
        artifact = await store.get_artifact_by_capture(
            session, capture_id, kind="ppm"
        ) or await store.get_artifact_by_capture(session, capture_id, kind="png")
    if artifact is None:
        raise HTTPException(status_code=409, detail="尚无已验证制品，不能执行视觉提取")

    from shared.utils.internal_http import InternalHTTPClient

    from app.tools.vm_console.adapter import _fetch_artifact_bytes
    from app.tools.vm_console.vision_extractor import extract_observation

    http_client = InternalHTTPClient(service_name="conversation-service")
    ppm_bytes = await _fetch_artifact_bytes(http_client, str(artifact["artifact_id"]))
    if ppm_bytes is None:
        raise HTTPException(status_code=409, detail="制品字节不可读取")

    observation = await extract_observation(
        ppm_bytes, artifact_id=str(artifact["artifact_id"]), trace_id=str(record.get("trace_id") or "")
    )
    payload = observation.model_dump(mode="json")
    async with factory() as session:
        await store.update_capture_status(
            session, capture_id, "completed", vision_result=payload, completed=True
        )
    return {"ok": True, "observation": payload}


def _json_safe(value: Any) -> Any:
    """把数据库记录中的非 JSON 原生类型转为可序列化形态。"""

    if isinstance(value, dict):
        return {key: _json_safe(item) for key, item in value.items()}
    if isinstance(value, (str, int, float, bool)) or value is None:
        return value
    return str(value)
