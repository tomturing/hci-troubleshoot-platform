"""vm_console_capture / vm_console_capture_artifact 表的数据访问。

表结构见 database/desired_schema.sql（conversation-service 模块区）与迁移
030_add_vm_console_capture.sql。状态机字段语义对齐设计文档 §5.4；全部更新为
append-only 审计友好写法（只前进、不回退）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 状态机合法状态集合（§5.4）。
CAPTURE_STATUSES = frozenset(
    {
        "created",
        "inventory_verified",
        "baseline_capturing",
        "baseline_captured",
        "quality_checked",
        "baseline_uploaded",
        "vision_analyzing",
        "completed",
        "wake_confirmation_pending",
        "wake_declined",
        "waking",
        "recapturing",
        "failed",
        "expired",
        "cancelled",
    }
)

# §8 失败语义错误码全集。
ERROR_CODES = frozenset(
    {
        "TARGET_CONTEXT_MISSING",
        "TARGET_OWNERSHIP_MISMATCH",
        "VM_NOT_RUNNING",
        "MONITOR_UNAVAILABLE",
        "BASELINE_CAPTURE_FAILED",
        "ARTIFACT_UPLOAD_FAILED",
        "IMAGE_INVALID",
        "WAKE_CONFIRMATION_REQUIRED",
        "WAKE_DECLINED",
        "WAKE_FAILED",
        "VISION_UNAVAILABLE_BY_POLICY",
        "VISION_UNCERTAIN",
        "VM_CONSOLE_DISABLED_BY_POLICY",
        "BRIDGE_TIMEOUT",
    }
)


async def create_capture_record(
    session: AsyncSession,
    *,
    capture_id: str,
    tenant_id: str | None,
    case_id: str,
    diagnosis_run_id: str | None,
    conversation_id: str | None,
    signal_id: str | None,
    host_node_id: str,
    vm_id: str,
    target_verification: dict[str, Any],
    source_kbd_id: str | None,
    source_kbd_revision: str | None,
    tool_catalog_revision: str | None,
    adapter_version: str,
    trace_id: str,
    exec_id: str | None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO vm_console_capture (
                capture_id, tenant_id, case_id, diagnosis_run_id, conversation_id,
                signal_id, mode, host_node_id, vm_id, target_verification,
                source_kbd_id, source_kbd_revision, tool_catalog_revision, adapter_version,
                status, wake_state, trace_id, exec_id
            ) VALUES (
                CAST(:capture_id AS uuid), :tenant_id, :case_id, :diagnosis_run_id,
                CAST(:conversation_id AS uuid), :signal_id, 'online',
                :host_node_id, :vm_id, CAST(:target_verification AS jsonb),
                :source_kbd_id, :source_kbd_revision, :tool_catalog_revision, :adapter_version,
                'created', 'not_needed', :trace_id, :exec_id
            )
            """
        ),
        {
            "capture_id": capture_id,
            "tenant_id": tenant_id,
            "case_id": case_id,
            "diagnosis_run_id": diagnosis_run_id,
            "conversation_id": conversation_id,
            "signal_id": signal_id,
            "host_node_id": host_node_id,
            "vm_id": vm_id,
            "target_verification": json.dumps(target_verification, ensure_ascii=False),
            "source_kbd_id": source_kbd_id,
            "source_kbd_revision": source_kbd_revision,
            "tool_catalog_revision": tool_catalog_revision,
            "adapter_version": adapter_version,
            "trace_id": trace_id,
            "exec_id": exec_id,
        },
    )
    await session.commit()


async def update_capture_status(
    session: AsyncSession,
    capture_id: str,
    status: str,
    *,
    error_code: str | None = None,
    error_summary: str | None = None,
    quality_metrics: dict[str, Any] | None = None,
    wake_state: str | None = None,
    wake_token_hash: str | None = None,
    clear_wake_token: bool = False,
    wake_confirmed_by: str | None = None,
    baseline_artifact_id: str | None = None,
    recapture_artifact_id: str | None = None,
    effective_artifact_id: str | None = None,
    vision_result: dict[str, Any] | None = None,
    completed: bool = False,
) -> None:
    """状态前进式更新；error_code 必须在 §8 错误码全集内。"""

    if status not in CAPTURE_STATUSES:
        raise ValueError(f"非法截图状态: {status}")
    if error_code and error_code not in ERROR_CODES:
        raise ValueError(f"非法错误码: {error_code}")

    sets = ["status = :status", "updated_at = now()"]
    params: dict[str, Any] = {"capture_id": capture_id, "status": status}
    if error_code is not None:
        sets.append("error_code = :error_code")
        params["error_code"] = error_code
    if error_summary is not None:
        sets.append("error_summary = :error_summary")
        params["error_summary"] = error_summary
    if quality_metrics is not None:
        sets.append("quality_metrics = CAST(:quality_metrics AS jsonb)")
        params["quality_metrics"] = json.dumps(quality_metrics, ensure_ascii=False)
    if wake_state is not None:
        sets.append("wake_state = :wake_state")
        params["wake_state"] = wake_state
    if wake_token_hash is not None:
        sets.append("wake_token_hash = :wake_token_hash")
        params["wake_token_hash"] = wake_token_hash
    if clear_wake_token:
        sets.append("wake_token_hash = NULL")
    if wake_confirmed_by is not None:
        sets.append("wake_confirmed_by = :wake_confirmed_by")
        sets.append("wake_confirmed_at = now()")
        params["wake_confirmed_by"] = wake_confirmed_by
    if baseline_artifact_id is not None:
        sets.append("baseline_artifact_id = CAST(:baseline_artifact_id AS uuid)")
        params["baseline_artifact_id"] = baseline_artifact_id
    if recapture_artifact_id is not None:
        sets.append("recapture_artifact_id = CAST(:recapture_artifact_id AS uuid)")
        params["recapture_artifact_id"] = recapture_artifact_id
    if effective_artifact_id is not None:
        sets.append("effective_artifact_id = CAST(:effective_artifact_id AS uuid)")
        params["effective_artifact_id"] = effective_artifact_id
    if vision_result is not None:
        sets.append("vision_result = CAST(:vision_result AS jsonb)")
        sets.append("vision_model_revision = :vision_model_revision")
        sets.append("vision_prompt_revision = :vision_prompt_revision")
        sets.append("vision_vocabulary_revision = :vision_vocabulary_revision")
        sets.append("vision_confidence = :vision_confidence")
        params["vision_result"] = json.dumps(vision_result, ensure_ascii=False)
        params["vision_model_revision"] = vision_result.get("model_revision")
        params["vision_prompt_revision"] = vision_result.get("prompt_revision")
        params["vision_vocabulary_revision"] = vision_result.get("display_state_vocabulary_revision")
        params["vision_confidence"] = vision_result.get("confidence")
    if completed:
        sets.append("completed_at = now()")

    await session.execute(
        text(f"UPDATE vm_console_capture SET {', '.join(sets)} WHERE capture_id = CAST(:capture_id AS uuid)"),
        params,
    )
    await session.commit()


async def consume_wake_token(
    session: AsyncSession, capture_id: str, wake_token_hash: str, confirmed_by: str
) -> bool:
    """一次性唤醒令牌原子消费（§D7）：rowcount=0 → 重放/重复/超时。"""

    result = await session.execute(
        text(
            """
            UPDATE vm_console_capture
            SET wake_state = 'confirmed',
                wake_token_hash = NULL,
                wake_confirmed_by = :confirmed_by,
                wake_confirmed_at = now(),
                updated_at = now()
            WHERE capture_id = CAST(:capture_id AS uuid)
              AND wake_state = 'confirmation_pending'
              AND wake_token_hash = :wake_token_hash
            """
        ),
        {"capture_id": capture_id, "wake_token_hash": wake_token_hash, "confirmed_by": confirmed_by},
    )
    await session.commit()
    return (result.rowcount or 0) == 1


async def get_capture_record(session: AsyncSession, capture_id: str) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT capture_id::text, tenant_id, case_id, diagnosis_run_id,
                   conversation_id::text, signal_id, mode, host_node_id, vm_id,
                   target_verification, status, error_code, error_summary,
                   baseline_artifact_id::text, recapture_artifact_id::text,
                   effective_artifact_id::text, quality_metrics, wake_state,
                   wake_confirmed_by, wake_confirmed_at, wake_result,
                   vision_result, vision_model_revision, vision_confidence,
                   source_kbd_id, source_kbd_revision, tool_catalog_revision,
                   adapter_version, trace_id, exec_id,
                   created_at, updated_at, completed_at
            FROM vm_console_capture
            WHERE capture_id = CAST(:capture_id AS uuid)
            """
        ),
        {"capture_id": capture_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def get_artifact_by_capture(
    session: AsyncSession, capture_id: str, kind: str = "ppm"
) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT artifact_id::text, capture_id::text, kind, sha256, media_type,
                   size_bytes, width, height, storage_ref, sensitivity, source, trace_id
            FROM vm_console_capture_artifact
            WHERE capture_id = CAST(:capture_id AS uuid) AND kind = :kind
            ORDER BY created_at DESC
            LIMIT 1
            """
        ),
        {"capture_id": capture_id, "kind": kind},
    )
    row = result.mappings().first()
    return dict(row) if row else None


async def insert_audit_event(
    session: AsyncSession,
    *,
    capture_id: str | None,
    event_type: str,
    case_id: str | None = None,
    conversation_id: str | None = None,
    actor: str | None = None,
    detail: dict | None = None,
    trace_id: str | None = None,
    mode: str = "online",
) -> None:
    """写入 append-only 审计事件（§10.1）。detail 只允许哈希与元数据。"""

    await session.execute(
        text(
            """
            INSERT INTO vm_console_audit_event (
                capture_id, case_id, conversation_id, mode, event_type, actor, detail, trace_id
            ) VALUES (
                CAST(:capture_id AS uuid), :case_id, CAST(:conversation_id AS uuid),
                :mode, :event_type, :actor, CAST(:detail AS jsonb), :trace_id
            )
            """
        ),
        {
            "capture_id": capture_id,
            "case_id": case_id,
            "conversation_id": conversation_id,
            "mode": mode,
            "event_type": event_type,
            "actor": actor,
            "detail": json.dumps(detail or {}, ensure_ascii=False),
            "trace_id": trace_id,
        },
    )
    await session.commit()
