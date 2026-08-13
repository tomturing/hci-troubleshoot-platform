"""KBD 持久化批量任务的状态归约契约。"""

from collections import UserDict, defaultdict
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import admin
from fastapi import HTTPException
from pydantic import ValidationError

_batch_interruption_error = admin._batch_interruption_error
_batch_item_row_to_dict = admin._batch_item_row_to_dict
_batch_row_to_dict = admin._batch_row_to_dict
_batch_status_from_counts = admin._batch_status_from_counts
_extract_signals_one = admin._extract_signals_one
_approve_one = admin._approve_one
_reject_one = admin._reject_one


def test_batch_status_is_completed_when_all_items_succeed():
    assert _batch_status_from_counts(succeeded=3, failed=0) == "completed"


def test_batch_status_is_partial_failed_when_results_are_mixed():
    assert _batch_status_from_counts(succeeded=2, failed=1) == "partial_failed"


def test_batch_status_is_failed_when_all_items_fail():
    assert _batch_status_from_counts(succeeded=0, failed=3) == "failed"


def test_batch_status_is_interrupted_when_any_item_was_interrupted():
    assert _batch_status_from_counts(succeeded=2, failed=1, interrupted=3) == "interrupted"


def test_batch_interruption_is_retryable_and_has_stable_code():
    error = _batch_interruption_error("service_restart")

    assert error["status"] == 503
    assert error["code"] == "BATCH_PROCESS_INTERRUPTED"
    assert error["retryable"] is True
    assert "重新提交失败条目" in error["message"]


def test_batch_response_exposes_retryable_count():
    row = UserDict(
        {
            "batch_id": "batch-1",
            "job_type": "extract_signals",
            "status": "partial_failed",
            "requested_kbd_ids": [1, 2],
            "request_json": {"reviewer_id": 7, "review_note": "统一审核备注"},
            "total_count": 2,
            "completed_count": 2,
            "succeeded_count": 0,
            "failed_count": 2,
            "interrupted_count": 0,
            "retryable_count": 1,
            "retry_of_batch_id": None,
            "retried_by_batch_id": None,
            "work_total_count": 2,
            "work_completed_count": 2,
            "work_failed_count": 2,
            "trace_id": "a" * 32,
            "created_at": None,
            "started_at": None,
            "completed_at": None,
            "updated_at": None,
        }
    )

    response = _batch_row_to_dict(row)
    assert response["retryable_count"] == 1
    assert response["request_json"]["reviewer_id"] == 7


def test_batch_item_exposes_current_kbd_state_and_publish_failure_reason():
    row = UserDict(
        {
            "item_id": 91,
            "kbd_id": 9,
            "support_id": "43487",
            "title": "测试 KBD",
            "kbd_status": "draft",
            "lock_version": 7,
            "status": "failed",
            "result_json": {},
            "error_json": {
                "status": 422,
                "code": "SIGNAL_REVIEW_BLOCKED",
                "message": {
                    "code": "SIGNAL_REVIEW_BLOCKED",
                    "message": "关键信号未通过发布审查",
                    "review": {"status": "blocked"},
                },
                "retryable": False,
            },
            "trace_id": "a" * 32,
        }
    )

    response = _batch_item_row_to_dict(row)

    assert response["kbd_status"] == "draft"
    assert response["lock_version"] == 7
    assert response["error_code"] == "SIGNAL_REVIEW_BLOCKED"
    assert response["error_message"] == "关键信号未通过发布审查"
    assert response["error_retryable"] is False


def test_batch_approve_requires_lock_version_snapshot_for_every_kbd():
    with pytest.raises(ValidationError, match="缺少 KBD 提交快照"):
        admin.BatchApproveRequest(kbd_ids=[9], reviewer_id=7, entries={})

    with pytest.raises(ValidationError, match="lock_version 无效"):
        admin.BatchApproveRequest(kbd_ids=[9], reviewer_id=7, entries={"9": {"category_id": "虚拟机-017"}})


@pytest.mark.asyncio
async def test_kbd_list_exposes_lock_version_for_batch_approve_snapshot():
    """列表选择项必须携带并发版本，否则前端无法安全提交异步发布。"""

    count_result = MagicMock()
    count_result.scalar.return_value = 1
    data_result = MagicMock()
    data_result.mappings.return_value.all.return_value = [
        defaultdict(
            lambda: None,
            {
                "id": 9,
                "support_id": "43487",
                "title": "测试 KBD",
                "lock_version": 7,
                "status": "draft",
                "images_json": [],
                "signals_json": [],
            },
        )
    ]
    session = AsyncMock()
    session.execute.side_effect = [count_result, data_result]
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    database_manager = MagicMock()
    database_manager.async_session_factory.return_value = session_context

    with patch.object(admin, "_db_manager", database_manager), patch.object(admin, "_check_auth"):
        response = await admin.list_kbd_entries(MagicMock())

    assert response["entries"][0]["lock_version"] == 7
    assert "e.lock_version" in str(session.execute.await_args_list[1].args[0])


@pytest.mark.asyncio
async def test_kbd_list_filters_test_samples_by_metadata_instead_of_kbd_id():
    """诊断样例检索必须依赖显式样例集属性，不能写死任何 KBD 主键。"""

    count_result = MagicMock()
    count_result.scalar.return_value = 0
    data_result = MagicMock()
    data_result.mappings.return_value.all.return_value = []
    session = AsyncMock()
    session.execute.side_effect = [count_result, data_result]
    session_context = AsyncMock()
    session_context.__aenter__.return_value = session
    database_manager = MagicMock()
    database_manager.async_session_factory.return_value = session_context

    with patch.object(admin, "_db_manager", database_manager), patch.object(admin, "_check_auth"):
        await admin.list_kbd_entries(MagicMock(), sample_suite="diagnosis-signal-matrix-v1")

    count_sql = str(session.execute.await_args_list[0].args[0])
    count_params = session.execute.await_args_list[0].args[1]
    assert "metadata->>'sample_suite' = :sample_suite" in count_sql
    assert count_params["sample_suite"] == "diagnosis-signal-matrix-v1"


@pytest.mark.asyncio
async def test_batch_approve_reuses_single_approve_contract_with_snapshot():
    approve_response = admin.KbdApproveResponse(
        success=True,
        kbd_id=9,
        status="published",
        embedding_generated=True,
        published_at="2026-08-12T10:00:00Z",
    )
    approve_mock = AsyncMock(return_value=approve_response)
    with patch.object(admin, "approve_kbd_entry", approve_mock):
        result = await _approve_one(
            9,
            request_context={
                "reviewer_id": 7,
                "review_note": "批量确认",
                "entries": {"9": {"category_id": "虚拟机-017", "lock_version": 3}},
            },
        )

    request, kbd_id, body = approve_mock.await_args.args
    assert request.headers["Authorization"].startswith("Bearer ")
    assert kbd_id == 9
    assert body.reviewer_id == 7
    assert body.review_note == "批量确认"
    assert body.category_id == "虚拟机-017"
    assert body.lock_version == 3
    assert result["status"] == "published"


@pytest.mark.asyncio
async def test_batch_reject_reuses_single_reject_contract():
    reject_mock = AsyncMock(return_value={"success": True, "kbd_id": 11, "status": "rejected"})
    with patch.object(admin, "reject_kbd_entry", reject_mock):
        result = await _reject_one(
            11,
            request_context={"reviewer_id": 8, "review_note": "信号证据不足"},
        )

    request, kbd_id, body = reject_mock.await_args.args
    assert request.headers["Authorization"].startswith("Bearer ")
    assert kbd_id == 11
    assert body.reviewer_id == 8
    assert body.review_note == "信号证据不足"
    assert result["status"] == "rejected"


@pytest.mark.asyncio
async def test_all_rejected_error_contains_candidate_details():
    rejected = {
        "signal": {"id": "sig_001", "acquire": {"tool": "qfk_platform"}},
        "reason_code": "run_failed",
        "reason": "判定器缺少必填项 extract",
    }
    with (
        patch(
            "app.routes.extract_signals.extract_signals_for_kbd",
            new=AsyncMock(
                return_value={
                    "signals_count": 0,
                    "rejected_count": 1,
                    "rejected": [rejected],
                    "proposal_revision_id": 450,
                }
            ),
        ),
        patch.object(admin, "_db_manager", object()),
    ):
        with pytest.raises(HTTPException) as exc_info:
            await _extract_signals_one(1081)

    assert exc_info.value.detail["code"] == "SIGNAL_ALL_REJECTED"
    assert exc_info.value.detail["proposal_revision_id"] == 450
    assert exc_info.value.detail["rejected_candidates"] == [rejected]
    assert "判定器缺少必填项 extract" in exc_info.value.detail["message"]
