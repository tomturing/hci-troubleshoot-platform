"""KBD 批量任务 PostgreSQL 生命周期集成测试。"""

import os
from unittest.mock import patch

import pytest
from app.routes.admin import (
    _create_batch_job,
    _run_batch_job,
    reconcile_interrupted_batch_jobs,
    retry_batch_job,
    set_dependencies,
)
from fastapi import BackgroundTasks
from shared.database.postgres import DatabaseManager
from sqlalchemy import text
from starlette.requests import Request

pytestmark = pytest.mark.skipif(
    os.environ.get("RUN_KB_POSTGRES_INTEGRATION") != "1",
    reason="需要显式开启本地 PostgreSQL 集成测试",
)


@pytest.mark.asyncio
async def test_batch_job_persists_progress_and_item_result():
    database_url = os.environ["TEST_DATABASE_URL"]
    database = DatabaseManager(database_url)
    set_dependencies(database)
    batch_id = None

    try:
        async with database.async_session_factory() as session:
            kbd_id = (await session.execute(text("SELECT id FROM kbd_entry ORDER BY id LIMIT 1"))).scalar_one()

        batch_id = await _create_batch_job([kbd_id], "reanalyze_images", "trace-kbd-batch-integration")

        async def processor(item_kbd_id: int, on_progress, _trace_id: str) -> dict:
            await on_progress(0, 0, 1)
            await on_progress(1, 0, 1)
            return {"kbd_id": item_kbd_id, "done": 1, "failed": 0}

        await _run_batch_job(batch_id, processor, "trace-kbd-batch-integration")

        async with database.async_session_factory() as session:
            job = (
                (
                    await session.execute(
                        text("""
                        SELECT status, total_count, completed_count, succeeded_count, failed_count,
                               work_total_count, work_completed_count, work_failed_count
                        FROM kbd_batch_job WHERE batch_id = CAST(:batch_id AS uuid)
                    """),
                        {"batch_id": str(batch_id)},
                    )
                )
                .mappings()
                .one()
            )
            item = (
                (
                    await session.execute(
                        text("""
                        SELECT status, result_json, error_json,
                               work_total_count, work_completed_count, work_failed_count
                        FROM kbd_batch_job_item WHERE batch_id = CAST(:batch_id AS uuid)
                    """),
                        {"batch_id": str(batch_id)},
                    )
                )
                .mappings()
                .one()
            )

        assert dict(job) == {
            "status": "completed",
            "total_count": 1,
            "completed_count": 1,
            "succeeded_count": 1,
            "failed_count": 0,
            "work_total_count": 1,
            "work_completed_count": 1,
            "work_failed_count": 0,
        }
        assert item["status"] == "succeeded"
        assert item["result_json"] == {"kbd_id": kbd_id, "done": 1, "failed": 0}
        assert item["error_json"] == {}
        assert (item["work_total_count"], item["work_completed_count"], item["work_failed_count"]) == (1, 1, 0)
    finally:
        if batch_id is not None:
            async with database.async_session_factory() as session:
                await session.execute(
                    text("DELETE FROM kbd_batch_job WHERE batch_id = CAST(:batch_id AS uuid)"),
                    {"batch_id": str(batch_id)},
                )
                await session.commit()
        await database.close()


@pytest.mark.asyncio
async def test_approve_batch_persists_and_passes_request_snapshot():
    database_url = os.environ["TEST_DATABASE_URL"]
    database = DatabaseManager(database_url)
    set_dependencies(database)
    batch_id = None

    try:
        async with database.async_session_factory() as session:
            kbd_id = (await session.execute(text("SELECT id FROM kbd_entry ORDER BY id LIMIT 1"))).scalar_one()
        snapshot = {
            "reviewer_id": 7,
            "review_note": "批量确认",
            "entries": {str(kbd_id): {"lock_version": 3, "category_id": "虚拟机-017"}},
        }
        batch_id = await _create_batch_job(
            [kbd_id],
            "approve",
            "trace-kbd-batch-approve",
            request_json=snapshot,
        )
        observed_context = None

        async def processor(item_kbd_id: int, _on_progress, _trace_id: str, *, request_context) -> dict:
            nonlocal observed_context
            observed_context = request_context
            return {"kbd_id": item_kbd_id, "status": "published"}

        await _run_batch_job(batch_id, processor, "trace-kbd-batch-approve")

        async with database.async_session_factory() as session:
            job = (
                await session.execute(
                    text("SELECT job_type, status, request_json FROM kbd_batch_job WHERE batch_id = :batch_id"),
                    {"batch_id": batch_id},
                )
            ).mappings().one()
        assert job["job_type"] == "approve"
        assert job["status"] == "completed"
        assert job["request_json"] == snapshot
        assert observed_context == snapshot
    finally:
        if batch_id is not None:
            async with database.async_session_factory() as session:
                await session.execute(text("DELETE FROM kbd_batch_job WHERE batch_id = :batch_id"), {"batch_id": batch_id})
                await session.commit()
        await database.close()


@pytest.mark.asyncio
async def test_retry_batch_only_copies_failed_and_interrupted_items():
    database_url = os.environ["TEST_DATABASE_URL"]
    database = DatabaseManager(database_url)
    set_dependencies(database)
    source_batch_id = None
    retry_batch_id = None

    try:
        async with database.async_session_factory() as session:
            kbd_ids = list(
                (await session.execute(text("SELECT id FROM kbd_entry ORDER BY id LIMIT 3"))).scalars().all()
            )
        if len(kbd_ids) < 3:
            pytest.skip("至少需要 3 条 KBD 数据验证重试筛选")

        source_batch_id = await _create_batch_job(kbd_ids, "extract_signals", "trace-kbd-batch-retry-source")
        async with database.async_session_factory() as session:
            await session.execute(
                text("""
                    UPDATE kbd_batch_job_item
                    SET status = CASE kbd_id
                        WHEN :succeeded_id THEN 'succeeded'
                        WHEN :failed_id THEN 'failed'
                        ELSE 'interrupted'
                    END,
                    completed_at = CURRENT_TIMESTAMP
                    WHERE batch_id = CAST(:batch_id AS uuid)
                """),
                {
                    "batch_id": str(source_batch_id),
                    "succeeded_id": kbd_ids[0],
                    "failed_id": kbd_ids[1],
                },
            )
            await session.execute(
                text("""
                    UPDATE kbd_batch_job
                    SET status = 'interrupted', completed_count = 3,
                        succeeded_count = 1, failed_count = 1, interrupted_count = 1,
                        completed_at = CURRENT_TIMESTAMP
                    WHERE batch_id = CAST(:batch_id AS uuid)
                """),
                {"batch_id": str(source_batch_id)},
            )
            await session.commit()

        request = Request({"type": "http", "method": "POST", "path": "/", "headers": []})
        background_tasks = BackgroundTasks()
        with patch("app.routes.admin._check_auth"):
            response = await retry_batch_job(request, source_batch_id, background_tasks)
        retry_batch_id = response.batch_id

        async with database.async_session_factory() as session:
            retry_job = (
                (
                    await session.execute(
                        text("""
                        SELECT retry_of_batch_id, total_count
                        FROM kbd_batch_job WHERE batch_id = CAST(:batch_id AS uuid)
                    """),
                        {"batch_id": str(retry_batch_id)},
                    )
                )
                .mappings()
                .one()
            )
            retried_ids = list(
                (
                    await session.execute(
                        text("""
                            SELECT kbd_id FROM kbd_batch_job_item
                            WHERE batch_id = CAST(:batch_id AS uuid) ORDER BY item_id
                        """),
                        {"batch_id": str(retry_batch_id)},
                    )
                )
                .scalars()
                .all()
            )

        assert retry_job["retry_of_batch_id"] == source_batch_id
        assert retry_job["total_count"] == 2
        assert retried_ids == kbd_ids[1:]
        assert len(background_tasks.tasks) == 1
    finally:
        async with database.async_session_factory() as session:
            if retry_batch_id is not None:
                await session.execute(
                    text("DELETE FROM kbd_batch_job WHERE batch_id = CAST(:batch_id AS uuid)"),
                    {"batch_id": str(retry_batch_id)},
                )
            if source_batch_id is not None:
                await session.execute(
                    text("DELETE FROM kbd_batch_job WHERE batch_id = CAST(:batch_id AS uuid)"),
                    {"batch_id": str(source_batch_id)},
                )
            await session.commit()
        await database.close()


@pytest.mark.asyncio
async def test_interrupted_batch_job_is_reconciled_to_retryable_terminal_state():
    database_url = os.environ["TEST_DATABASE_URL"]
    database = DatabaseManager(database_url)
    set_dependencies(database)
    batch_id = None

    try:
        async with database.async_session_factory() as session:
            kbd_id = (await session.execute(text("SELECT id FROM kbd_entry ORDER BY id LIMIT 1"))).scalar_one()

        batch_id = await _create_batch_job([kbd_id], "extract_signals", "trace-kbd-batch-interrupted")
        async with database.async_session_factory() as session:
            await session.execute(
                text("""
                    UPDATE kbd_batch_job SET status = 'running', started_at = CURRENT_TIMESTAMP
                    WHERE batch_id = CAST(:batch_id AS uuid)
                """),
                {"batch_id": str(batch_id)},
            )
            await session.execute(
                text("""
                    UPDATE kbd_batch_job_item SET status = 'running', started_at = CURRENT_TIMESTAMP
                    WHERE batch_id = CAST(:batch_id AS uuid)
                """),
                {"batch_id": str(batch_id)},
            )
            await session.commit()

        result = await reconcile_interrupted_batch_jobs(
            reason="service_restart",
            stale_after_seconds=0,
            batch_id=batch_id,
        )

        async with database.async_session_factory() as session:
            job = (
                (
                    await session.execute(
                        text("""
                        SELECT status, completed_count, succeeded_count, failed_count,
                               interrupted_count, completed_at
                        FROM kbd_batch_job WHERE batch_id = CAST(:batch_id AS uuid)
                    """),
                        {"batch_id": str(batch_id)},
                    )
                )
                .mappings()
                .one()
            )
            item = (
                (
                    await session.execute(
                        text("""
                        SELECT status, error_json, work_total_count, work_completed_count, work_failed_count
                        FROM kbd_batch_job_item WHERE batch_id = CAST(:batch_id AS uuid)
                    """),
                        {"batch_id": str(batch_id)},
                    )
                )
                .mappings()
                .one()
            )

        assert result == {"jobs": 1, "items": 1}
        assert job["status"] == "interrupted"
        assert (
            job["completed_count"],
            job["succeeded_count"],
            job["failed_count"],
            job["interrupted_count"],
        ) == (1, 0, 0, 1)
        assert job["completed_at"] is not None
        assert item["status"] == "interrupted"
        assert item["error_json"]["code"] == "BATCH_PROCESS_INTERRUPTED"
        assert item["error_json"]["retryable"] is True
        assert (item["work_total_count"], item["work_completed_count"], item["work_failed_count"]) == (0, 0, 0)
    finally:
        if batch_id is not None:
            async with database.async_session_factory() as session:
                await session.execute(
                    text("DELETE FROM kbd_batch_job WHERE batch_id = CAST(:batch_id AS uuid)"),
                    {"batch_id": str(batch_id)},
                )
                await session.commit()
        await database.close()
