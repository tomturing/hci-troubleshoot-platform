"""effect_verification / effect_verification_check 表的数据访问。

表结构见 database/desired_schema.sql（conversation-service 模块区）与迁移
032_add_effect_verification.sql。状态机字段语义对齐设计文档 §5.4；全部更新为
append-only 审计友好写法（只前进、不回退）。
"""

from __future__ import annotations

import json
from typing import Any

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

# 状态机合法状态集合（设计文档 §5.4）。
EFFECT_STATUSES = frozenset(
    {
        "created",
        "expectation_resolved",
        "settle_pending",
        "observing",
        "recheck_scheduled",
        "verdict_achieved",
        "verdict_not_achieved",
        "verdict_inconclusive",
        "failed",
        "cancelled",
    }
)

# §8 失败语义错误码全集（verdict_not_achieved 是判定结论，不是错误码）。
ERROR_CODES = frozenset(
    {
        "EXPECTATION_SOURCE_MISSING",
        "EXPECTATION_CONTRACT_INVALID",
        "TARGET_CONTEXT_MISSING",
        "ACTION_REF_MISSING",
        "SETTLE_PENDING",
        "OBSERVATION_ERROR",
        "NEGATIVE_EVIDENCE_INSUFFICIENT",
        "WINDOW_EXPIRED_INCONCLUSIVE",
        "EFFECT_POLICY_DISABLED",
        "ORPHANED_BY_RESTART",
    }
)

# 三态判定词表（effect-verdict-v1）。
VERDICTS = frozenset({"achieved", "not_achieved", "inconclusive"})


async def create_verification_record(
    session: AsyncSession,
    *,
    verification_id: str,
    tenant_id: str | None,
    case_id: str,
    diagnosis_run_id: str | None,
    conversation_id: str | None,
    signal_id: str | None,
    usage: str,
    action_exec_id: str | None,
    expectation_snapshot: dict[str, Any],
    target_verification: dict[str, Any],
    source_kbd_id: str | None,
    source_kbd_revision: str | None,
    tool_catalog_revision: str | None,
    trace_id: str,
    next_check_at: str | None = None,
) -> None:
    await session.execute(
        text(
            """
            INSERT INTO effect_verification (
                verification_id, tenant_id, case_id, diagnosis_run_id, conversation_id,
                signal_id, usage, action_exec_id, expectation_snapshot, target_verification,
                source_kbd_id, source_kbd_revision, tool_catalog_revision,
                status, trace_id, next_check_at
            ) VALUES (
                CAST(:verification_id AS uuid), :tenant_id, :case_id, :diagnosis_run_id,
                CAST(:conversation_id AS uuid), :signal_id, :usage, :action_exec_id,
                CAST(:expectation_snapshot AS jsonb), CAST(:target_verification AS jsonb),
                :source_kbd_id, :source_kbd_revision, :tool_catalog_revision,
                'created', :trace_id, CAST(:next_check_at AS timestamptz)
            )
            """
        ),
        {
            "verification_id": verification_id,
            "tenant_id": tenant_id,
            "case_id": case_id,
            "diagnosis_run_id": diagnosis_run_id,
            "conversation_id": conversation_id,
            "signal_id": signal_id,
            "usage": usage,
            "action_exec_id": action_exec_id,
            "expectation_snapshot": json.dumps(expectation_snapshot, ensure_ascii=False),
            "target_verification": json.dumps(target_verification, ensure_ascii=False),
            "source_kbd_id": source_kbd_id,
            "source_kbd_revision": source_kbd_revision,
            "tool_catalog_revision": tool_catalog_revision,
            "trace_id": trace_id,
            "next_check_at": next_check_at,
        },
    )
    await session.commit()


async def update_verification_status(
    session: AsyncSession,
    verification_id: str,
    status: str,
    *,
    verdict: str | None = None,
    error_code: str | None = None,
    error_summary: str | None = None,
    increment_recheck: bool = False,
    next_check_at: str | None = None,
    clear_next_check: bool = False,
    completed: bool = False,
) -> None:
    """状态前进式更新；error_code/verdict 必须在封闭词表内。"""

    if status not in EFFECT_STATUSES:
        raise ValueError(f"非法效果验证状态: {status}")
    if error_code and error_code not in ERROR_CODES:
        raise ValueError(f"非法错误码: {error_code}")
    if verdict and verdict not in VERDICTS:
        raise ValueError(f"非法判定取值: {verdict}")

    sets = ["status = :status", "updated_at = now()"]
    params: dict[str, Any] = {"verification_id": verification_id, "status": status}
    if verdict is not None:
        sets.append("verdict = :verdict")
        params["verdict"] = verdict
    if error_code is not None:
        sets.append("error_code = :error_code")
        params["error_code"] = error_code
    if error_summary is not None:
        sets.append("error_summary = :error_summary")
        params["error_summary"] = error_summary
    if increment_recheck:
        sets.append("recheck_count = recheck_count + 1")
    if next_check_at is not None:
        sets.append("next_check_at = CAST(:next_check_at AS timestamptz)")
        params["next_check_at"] = next_check_at
    if clear_next_check:
        sets.append("next_check_at = NULL")
    if completed:
        sets.append("completed_at = now()")

    await session.execute(
        text(
            "UPDATE effect_verification SET "
            + ", ".join(sets)
            + " WHERE verification_id = CAST(:verification_id AS uuid)"
        ),
        params,
    )
    await session.commit()


async def insert_check_record(
    session: AsyncSession,
    *,
    verification_id: str,
    check_seq: int,
    trigger_source: str,
    observation_status: str,
    observation_summary: str | None,
    matcher_evidence: str | None,
    check_verdict: str | None,
    error_code: str | None = None,
    trace_id: str | None = None,
) -> None:
    """写入一次观测判定记录（append-only 时间线）。"""

    if observation_status not in {"valid", "error", "insufficient"}:
        raise ValueError(f"非法观测状态: {observation_status}")
    if check_verdict and check_verdict not in VERDICTS:
        raise ValueError(f"非法判定取值: {check_verdict}")
    await session.execute(
        text(
            """
            INSERT INTO effect_verification_check (
                verification_id, check_seq, trigger_source, observation_status,
                observation_summary, matcher_evidence, check_verdict, error_code, trace_id
            ) VALUES (
                CAST(:verification_id AS uuid), :check_seq, :trigger_source, :observation_status,
                :observation_summary, :matcher_evidence, :check_verdict, :error_code, :trace_id
            )
            """
        ),
        {
            "verification_id": verification_id,
            "check_seq": check_seq,
            "trigger_source": trigger_source,
            "observation_status": observation_status,
            "observation_summary": observation_summary,
            "matcher_evidence": matcher_evidence,
            "check_verdict": check_verdict,
            "error_code": error_code,
            "trace_id": trace_id,
        },
    )
    await session.commit()


async def get_verification_record(session: AsyncSession, verification_id: str) -> dict[str, Any] | None:
    result = await session.execute(
        text(
            """
            SELECT verification_id::text, tenant_id, case_id, diagnosis_run_id,
                   conversation_id::text, signal_id, usage, action_exec_id,
                   expectation_snapshot, target_verification, status, verdict,
                   verdict_vocabulary_revision, recheck_count, next_check_at,
                   error_code, error_summary,
                   source_kbd_id, source_kbd_revision, tool_catalog_revision,
                   trace_id, created_at, updated_at, completed_at
            FROM effect_verification
            WHERE verification_id = CAST(:verification_id AS uuid)
            """
        ),
        {"verification_id": verification_id},
    )
    row = result.mappings().first()
    return dict(row) if row else None
