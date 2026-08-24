"""qkv_vm_console 在线通道（conversation-service 侧）。

职责（设计文档 §5.1/§6.2）：
- 中继 agent-service 的固定截图操作到前端 Bridge WS（SSE 事件 vm_console_op）；
- 接收 terminal_bridge 直传的原始 PPM 制品（服务端到服务端，不经 WS/Redis），
  落盘加密卷前先做 SHA-256 校验与确定性近黑质量检测；
- 向前端推送近黑唤醒确认卡（interactive_request，kind=vm_console_wake_confirm）；
- 回传 Bridge 元数据结果到 Redis（供 agent-service BLPOP），原图字节不入 Redis。

安全：制品默认 sensitivity=confidential；字节下载仅限内部 Token；
任何环节失败 fail-closed，不降级 base64 over WS。
"""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel, Field
from shared.database.postgres import DatabaseManager
from shared.database.redis import RedisManager
from shared.observability.logger import get_logger
from shared.observability.metrics import (
    VM_CONSOLE_ARTIFACT_BYTES_TOTAL,
    VM_CONSOLE_CAPTURE_TOTAL,
    VM_CONSOLE_NEAR_BLACK_TOTAL,
    VM_CONSOLE_SECURITY_REJECTION_TOTAL,
)
from shared.observability.otel import get_current_trace_id
from shared.vision.near_black import analyze_ppm_near_black
from sqlalchemy import text

from app.routes.agent_exec import _check_user_session

logger = get_logger("vm-console-routes")
router = APIRouter(tags=["vm-console"])

INTERNAL_API_TOKEN = os.getenv("INTERNAL_API_TOKEN", "")
_ARTIFACT_DIR = Path(os.getenv("VM_CONSOLE_ARTIFACT_DIR", "./data/vm-console-artifacts"))
_MAX_UPLOAD_BYTES = 16 * 1024 * 1024  # 与 max_capture_bytes 对齐
_ARTIFACT_RETENTION_DAYS = int(os.getenv("VM_CONSOLE_ARTIFACT_RETENTION_DAYS", "30"))

_db_manager: DatabaseManager | None = None
_redis_manager: RedisManager | None = None


def set_dependencies(db: DatabaseManager, redis: RedisManager) -> None:
    """由 main.py 注入数据库与 Redis 依赖。"""

    global _db_manager, _redis_manager
    _db_manager = db
    _redis_manager = redis


async def _insert_audit_event(
    *,
    capture_id: str | None,
    event_type: str,
    actor: str | None = None,
    detail: dict | None = None,
) -> None:
    """写入 append-only 审计事件（§10.1）；失败不阻断主流程。"""

    if _db_manager is None:
        return
    try:
        async with _db_manager.async_session_factory() as session:
            case_id = None
            conversation_id = None
            if capture_id:
                row = (
                    await session.execute(
                        text(
                            "SELECT case_id, conversation_id::text FROM vm_console_capture "
                            "WHERE capture_id = CAST(:capture_id AS uuid)"
                        ),
                        {"capture_id": capture_id},
                    )
                ).mappings().first()
                if row:
                    case_id = row["case_id"]
                    conversation_id = row["conversation_id"]
            await session.execute(
                text(
                    """
                    INSERT INTO vm_console_audit_event (
                        capture_id, case_id, conversation_id, mode, event_type, actor, detail
                    ) VALUES (
                        CAST(:capture_id AS uuid), :case_id, CAST(:conversation_id AS uuid),
                        'online', :event_type, :actor, CAST(:detail AS jsonb)
                    )
                    """
                ),
                {
                    "capture_id": capture_id,
                    "case_id": case_id,
                    "conversation_id": conversation_id,
                    "event_type": event_type,
                    "actor": actor,
                    "detail": json.dumps(detail or {}, ensure_ascii=False),
                },
            )
            await session.commit()
    except Exception as exc:
        logger.warning("vm_console_audit_write_failed", event_type=event_type, error=str(exc))


async def _db_manager_get_conversation(capture_id: str) -> str | None:
    """按截图会话查询所属 conversation_id（推送 SSE 用）。"""

    if _db_manager is None:
        return None
    try:
        async with _db_manager.async_session_factory() as session:
            row = (
                await session.execute(
                    text(
                        "SELECT conversation_id::text FROM vm_console_capture "
                        "WHERE capture_id = CAST(:capture_id AS uuid)"
                    ),
                    {"capture_id": capture_id},
                )
            ).scalar_one_or_none()
            return row
    except Exception:
        return None


def _check_internal_auth(request: Request) -> None:
    auth_header = request.headers.get("Authorization", "")
    if not INTERNAL_API_TOKEN or not auth_header.startswith("Bearer ") or auth_header.split(" ", 1)[1] != INTERNAL_API_TOKEN:
        raise HTTPException(status_code=401, detail="内部 Token 无效")


# ─── 1. agent → bridge：固定截图操作中继 ────────────────────────────────


class VmConsoleOpRequest(BaseModel):
    """受限操作载荷：无自由文本命令字段。"""

    model_config = {"extra": "forbid"}

    capture_id: str = Field(..., pattern=r"^[0-9a-fA-F-]{36}$")
    exec_id: str
    operation: str = Field(..., pattern=r"^(capture_baseline|wake_down_key)$")
    host_node_id: str = Field(..., pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
    vm_id: str = Field(..., pattern=r"^[0-9]{1,20}$")
    node_ip: str | None = None
    case_id: str | None = None
    timeout_seconds: int = Field(default=60, ge=1, le=60)
    role: str = Field(default="baseline", pattern=r"^(baseline|recapture|wake)$")
    artifact_policy: str = "vm_console_v1"
    catalog_revision: str | None = None
    trace_id: str | None = None


@router.post("/internal/conversations/{conversation_id}/vm-console-op", status_code=202)
async def push_vm_console_op(request: Request, conversation_id: uuid.UUID, body: VmConsoleOpRequest):
    """把固定操作经 SSE 中继到前端，由前端转发给 terminal_bridge WS。"""

    _check_internal_auth(request)
    if _redis_manager is None or _redis_manager.client is None:
        raise HTTPException(status_code=503, detail="Redis 服务未就绪")

    trace_id = body.trace_id or get_current_trace_id()
    pending = body.model_dump()
    pending["conversation_id"] = str(conversation_id)
    await _redis_manager.set(
        f"vm_console_op:{body.exec_id}",
        json.dumps(pending, ensure_ascii=False),
        ex=body.timeout_seconds + 60,
    )

    event_data = {
        "captureId": body.capture_id,
        "execId": body.exec_id,
        "operation": body.operation,
        "hostNodeId": body.host_node_id,
        "vmId": body.vm_id,
        "nodeIp": body.node_ip,
        "caseId": body.case_id,
        "timeoutSeconds": body.timeout_seconds,
        "role": body.role,
        "artifactPolicy": body.artifact_policy,
        "catalogRevision": body.catalog_revision,
        "traceId": trace_id,
    }
    sse_pusher = getattr(request.app.state, "sse_pusher", None)
    if sse_pusher:
        await sse_pusher.push_event(
            conversation_id=str(conversation_id), event_type="vm_console_op", data=event_data
        )
    else:
        logger.warning("vm_console_sse_pusher_unavailable", exec_id=body.exec_id)
    logger.info(
        "vm_console_op_pushed",
        conversation_id=str(conversation_id),
        capture_id=body.capture_id,
        exec_id=body.exec_id,
        operation=body.operation,
        role=body.role,
        trace_id=trace_id,
    )
    return {"ok": True, "exec_id": body.exec_id, "message": "截图操作已推送"}


# ─── 2. bridge → 平台：原始 PPM 制品直传 ────────────────────────────────


@router.post("/internal/vm-console/artifacts/{capture_id}", status_code=201)
async def upload_vm_console_artifact(
    request: Request,
    capture_id: uuid.UUID,
    kind: str = "ppm",
    role: str = "baseline",
):
    """接收 Bridge 直传的原始截图字节：SHA-256 校验 + 近黑质量 + 落盘登记。"""

    _check_internal_auth(request)
    if kind != "ppm":
        raise HTTPException(status_code=400, detail="仅支持原始 ppm 制品上传")
    if role not in {"baseline", "recapture"}:
        raise HTTPException(status_code=400, detail="role 仅支持 baseline/recapture")
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")

    body = await request.body()
    if not body:
        raise HTTPException(status_code=400, detail="制品内容为空")
    if len(body) > _MAX_UPLOAD_BYTES:
        VM_CONSOLE_SECURITY_REJECTION_TOTAL.labels(reason="image_too_large", mode="online").inc()
        raise HTTPException(status_code=413, detail="制品超过 16MiB 上限（IMAGE_INVALID）")

    declared_sha256 = (request.headers.get("X-Capture-Sha256") or "").lower()
    actual_sha256 = hashlib.sha256(body).hexdigest()
    if declared_sha256 and declared_sha256 != actual_sha256:
        VM_CONSOLE_SECURITY_REJECTION_TOTAL.labels(reason="sha256_mismatch", mode="online").inc()
        raise HTTPException(status_code=400, detail="制品 SHA-256 不一致（IMAGE_INVALID）")

    # 确定性近黑质量检测（与离线 Go 采集器同算法修订）。
    quality = analyze_ppm_near_black(body)
    if not quality.get("parse_ok"):
        VM_CONSOLE_SECURITY_REJECTION_TOTAL.labels(reason="image_invalid", mode="online").inc()
        raise HTTPException(status_code=400, detail=f"制品不是合法 P6 PPM（IMAGE_INVALID）: {quality.get('parse_error')}")

    artifact_id = uuid.uuid4()
    _ARTIFACT_DIR.mkdir(parents=True, exist_ok=True)
    storage_path = _ARTIFACT_DIR / f"{artifact_id}.ppm"
    storage_path.write_bytes(body)

    trace_id = get_current_trace_id() or ""
    async with _db_manager.async_session_factory() as session:
        await session.execute(
            text(
                """
                INSERT INTO vm_console_capture_artifact (
                    artifact_id, capture_id, kind, sha256, media_type, size_bytes,
                    width, height, storage_ref, sensitivity, source, trace_id,
                    expires_at
                ) VALUES (
                    CAST(:artifact_id AS uuid), CAST(:capture_id AS uuid), 'ppm',
                    :sha256, 'image/x-portable-pixmap', :size_bytes,
                    :width, :height, :storage_ref, 'confidential', 'online', :trace_id,
                    now() + make_interval(days => :retention_days)
                )
                """
            ),
            {
                "artifact_id": str(artifact_id),
                "capture_id": str(capture_id),
                "sha256": actual_sha256,
                "size_bytes": len(body),
                "width": (quality.get("metrics") or {}).get("width"),
                "height": (quality.get("metrics") or {}).get("height"),
                "storage_ref": str(storage_path),
                "trace_id": trace_id,
                "retention_days": _ARTIFACT_RETENTION_DAYS,
            },
        )
        slot = "baseline_artifact_id" if role == "baseline" else "recapture_artifact_id"
        await session.execute(
            text(
                f"""
                UPDATE vm_console_capture
                SET {slot} = CAST(:artifact_id AS uuid),
                    effective_artifact_id = CAST(:artifact_id AS uuid),
                    quality_metrics = CAST(:quality AS jsonb),
                    status = CASE WHEN status IN ('failed','expired','cancelled') THEN status ELSE 'baseline_uploaded' END,
                    updated_at = now()
                WHERE capture_id = CAST(:capture_id AS uuid)
                """
            ),
            {
                "artifact_id": str(artifact_id),
                "capture_id": str(capture_id),
                "quality": json.dumps(quality, ensure_ascii=False),
            },
        )
        await session.commit()

    await _insert_audit_event(
        capture_id=str(capture_id), event_type="upload_completed",
        detail={"sha256": actual_sha256, "size_bytes": len(body), "role": role,
                "near_black": bool(quality.get("near_black"))},
    )
    VM_CONSOLE_ARTIFACT_BYTES_TOTAL.labels(kind="ppm", mode="online").inc(len(body))
    VM_CONSOLE_NEAR_BLACK_TOTAL.labels(near_black=str(bool(quality.get("near_black"))).lower(), mode="online").inc()
    VM_CONSOLE_CAPTURE_TOTAL.labels(stage="upload", status="ok", mode="online").inc()
    # 推送基线截图就绪事件（客户端结果卡的第一阶段数据）。
    sse_pusher = getattr(request.app.state, "sse_pusher", None)
    if sse_pusher:
        conv_row = await _db_manager_get_conversation(str(capture_id))
        if conv_row:
            await sse_pusher.push_event(
                conversation_id=conv_row,
                event_type="vm_console_capture",
                data={
                    "captureId": str(capture_id),
                    "role": role,
                    "artifactId": str(artifact_id),
                    "nearBlack": bool(quality.get("near_black")),
                    "sizeBytes": len(body),
                    "sha256": actual_sha256,
                },
            )
    logger.info(
        "vm_console_artifact_stored",
        capture_id=str(capture_id),
        artifact_id=str(artifact_id),
        role=role,
        sha256=actual_sha256,
        size_bytes=len(body),
        near_black=quality.get("near_black"),
        trace_id=trace_id,
    )
    return {
        "ok": True,
        "artifact_id": str(artifact_id),
        "sha256": actual_sha256,
        "near_black": bool(quality.get("near_black")),
    }


@router.get("/internal/vm-console/artifacts/{artifact_id}/bytes")
async def download_vm_console_artifact(request: Request, artifact_id: uuid.UUID):
    """内部鉴权读取制品原图字节（视觉提取用）；原图不进入公网固定 URL。"""

    _check_internal_auth(request)
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    async with _db_manager.async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    "SELECT storage_ref, media_type FROM vm_console_capture_artifact WHERE artifact_id = CAST(:id AS uuid)"
                ),
                {"id": str(artifact_id)},
            )
        ).mappings().first()
        # 查看原图必须单独记审计（§6.2）：读取方与时间落在制品记录上。
        await session.execute(
            text(
                """
                UPDATE vm_console_capture_artifact
                SET last_read_at = now(), last_read_by = 'internal:agent-service'
                WHERE artifact_id = CAST(:id AS uuid)
                """
            ),
            {"id": str(artifact_id)},
        )
        await session.commit()
    if row is None:
        raise HTTPException(status_code=404, detail="制品不存在")
    path = Path(str(row["storage_ref"]))
    if not path.is_file():
        raise HTTPException(status_code=410, detail="制品文件已过期或不可用")
    await _insert_audit_event(
        capture_id=None, event_type="artifact_read", actor="internal:agent-service",
        detail={"artifact_id": str(artifact_id), "purpose": "vision_extraction"},
    )
    return Response(content=path.read_bytes(), media_type=str(row["media_type"]))


# ─── 3. 唤醒确认卡推送 ───────────────────────────────────────────────────


class WakeConfirmRequest(BaseModel):
    model_config = {"extra": "forbid"}

    capture_id: str
    case_id: str | None = None
    host_node_id: str
    vm_id: str
    wake_token: str
    timeout_seconds: int = Field(default=300, ge=10, le=600)


@router.post("/internal/conversations/{conversation_id}/vm-console-wake-confirm", status_code=202)
async def push_wake_confirmation(request: Request, conversation_id: uuid.UUID, body: WakeConfirmRequest):
    """向前端推送唤醒确认卡（固定文案说明 sendkey down 的影响）。"""

    _check_internal_auth(request)
    sse_pusher = getattr(request.app.state, "sse_pusher", None)
    event_data = {
        "kind": "vm_console_wake_confirm",
        "requestId": f"vm-console-wake-{body.capture_id}",
        "captureId": body.capture_id,
        "caseId": body.case_id,
        "hostNodeId": body.host_node_id,
        "vmId": body.vm_id,
        "wakeToken": body.wake_token,
        "timeoutSeconds": body.timeout_seconds,
        "title": "控制台截图接近黑屏，是否尝试唤醒？",
        "message": (
            "首张控制台截图接近黑屏。是否向虚拟机发送一次“向下方向键”尝试唤醒后重新截图？"
            "此操作可能改变虚拟机当前界面的焦点或选择，但不会发送任意命令。"
        ),
    }
    if sse_pusher:
        await sse_pusher.push_event(
            conversation_id=str(conversation_id), event_type="interactive_request", data=event_data
        )
    logger.info(
        "vm_console_wake_confirm_pushed",
        conversation_id=str(conversation_id),
        capture_id=body.capture_id,
    )
    return {"ok": True, "message": "唤醒确认卡已推送"}


# ─── 4. bridge → 前端 → 平台：元数据结果回传 ────────────────────────────


class VmConsoleResultRequest(BaseModel):
    """Bridge 回传的元数据（不含图片字节）。"""

    model_config = {"extra": "forbid"}

    capture_id: str
    exec_id: str
    operation: str
    exit_code: int | None = None
    sha256: str | None = None
    size_bytes: int | None = Field(default=None, ge=0)
    upload_status: str | None = None
    error_type: str | None = None
    duration_ms: int | None = Field(default=None, ge=0)
    timed_out: bool = False
    trace_id: str | None = None


@router.post("/api/conversations/{conversation_id}/vm-console-result")
async def submit_vm_console_result(conversation_id: uuid.UUID, body: VmConsoleResultRequest):
    """前端把 Bridge 的 vm_console_result 元数据回传；合并制品信息后入 Redis。"""

    if _redis_manager is None or _redis_manager.client is None:
        raise HTTPException(status_code=503, detail="Redis 服务未就绪")

    payload = body.model_dump()
    # 合并 conversation-service 已登记的制品信息与近黑质量（Bridge 不携带）。
    if _db_manager is not None:
        try:
            async with _db_manager.async_session_factory() as session:
                row = (
                    await session.execute(
                        text(
                            """
                            SELECT a.artifact_id::text, a.sha256, c.quality_metrics
                            FROM vm_console_capture_artifact a
                            JOIN vm_console_capture c ON c.capture_id = a.capture_id
                            WHERE a.capture_id = CAST(:capture_id AS uuid)
                            ORDER BY a.created_at DESC LIMIT 1
                            """
                        ),
                        {"capture_id": body.capture_id},
                    )
                ).mappings().first()
                if row:
                    payload["artifact_id"] = row["artifact_id"]
                    payload["sha256"] = row["sha256"]
                    metrics = row["quality_metrics"] if isinstance(row["quality_metrics"], dict) else {}
                    payload["quality"] = metrics
                    payload["near_black"] = bool(metrics.get("near_black"))
        except Exception as exc:
            logger.warning("vm_console_result_merge_failed", error=str(exc), capture_id=body.capture_id)

    await _redis_manager.client.lpush(
        f"vm_console_result:{body.exec_id}", json.dumps(payload, ensure_ascii=False)
    )
    await _redis_manager.client.expire(f"vm_console_result:{body.exec_id}", 300)
    await _redis_manager.client.delete(f"vm_console_op:{body.exec_id}")
    logger.info(
        "vm_console_result_received",
        conversation_id=str(conversation_id),
        capture_id=body.capture_id,
        exec_id=body.exec_id,
        operation=body.operation,
        upload_status=body.upload_status,
        error_type=body.error_type,
    )
    return {"ok": True, "exec_id": body.exec_id, "message": "截图结果已回传"}


class VmConsoleObservationRequest(BaseModel):
    """agent-service 视觉提取完成后的观察推送（仅受限 Schema 字段）。"""

    model_config = {"extra": "forbid"}

    capture_id: str
    observation_status: str = "observed"
    display_state: str = "unknown"
    summary: str = ""
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    needs_human_review: bool = False
    artifact_id: str | None = None
    wake_executed: bool = False
    error_code: str | None = None


@router.post("/internal/conversations/{conversation_id}/vm-console-observation", status_code=202)
async def push_vm_console_observation(
    request: Request, conversation_id: uuid.UUID, body: VmConsoleObservationRequest
):
    """把视觉观察结果推送到客户端（完成态结果卡数据源）。"""

    _check_internal_auth(request)
    sse_pusher = getattr(request.app.state, "sse_pusher", None)
    if sse_pusher:
        await sse_pusher.push_event(
            conversation_id=str(conversation_id),
            event_type="vm_console_observation",
            data={
                "captureId": body.capture_id,
                "observationStatus": body.observation_status,
                "displayState": body.display_state,
                "summary": body.summary,
                "confidence": body.confidence,
                "needsHumanReview": body.needs_human_review,
                "artifactId": body.artifact_id,
                "wakeExecuted": body.wake_executed,
                "errorCode": body.error_code,
            },
        )
    return {"ok": True}


@router.get("/api/conversations/{conversation_id}/vm-console-artifacts/{artifact_id}")
async def download_vm_console_thumbnail(
    request: Request,
    conversation_id: uuid.UUID,
    artifact_id: uuid.UUID,
    user_id: str = Depends(_check_user_session),
):
    """授权缩略图下载（§6.2：短时签名访问 + 查看原图单独记审计）。

    仅允许下载属于当前会话的制品；每次访问更新 last_read_at/last_read_by。
    """

    if _db_manager is None:
        raise HTTPException(status_code=503, detail="数据库未就绪")
    async with _db_manager.async_session_factory() as session:
        row = (
            await session.execute(
                text(
                    """
                    SELECT a.storage_ref, a.media_type, c.conversation_id::text
                    FROM vm_console_capture_artifact a
                    JOIN vm_console_capture c ON c.capture_id = a.capture_id
                    WHERE a.artifact_id = CAST(:artifact_id AS uuid)
                    """
                ),
                {"artifact_id": str(artifact_id)},
            )
        ).mappings().first()
        if row is None or str(row["conversation_id"]) != str(conversation_id):
            raise HTTPException(status_code=404, detail="制品不存在或不属于当前会话")
        await session.execute(
            text(
                """
                UPDATE vm_console_capture_artifact
                SET last_read_at = now(), last_read_by = :reader
                WHERE artifact_id = CAST(:artifact_id AS uuid)
                """
            ),
            {"artifact_id": str(artifact_id), "reader": f"user:{user_id}"},
        )
        await session.commit()
    path = Path(str(row["storage_ref"]))
    if not path.is_file():
        raise HTTPException(status_code=410, detail="制品文件已过期或不可用")
    await _insert_audit_event(
        capture_id=None, event_type="artifact_read", actor=f"user:{user_id}",
        detail={"artifact_id": str(artifact_id), "conversation_id": str(conversation_id),
                "purpose": "thumbnail"},
    )
    return Response(content=path.read_bytes(), media_type=str(row["media_type"]))
