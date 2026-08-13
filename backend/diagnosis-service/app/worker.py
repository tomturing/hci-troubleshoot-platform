"""Diagnosis Worker（诊断工作进程）入口。"""

import asyncio
import os
import socket
import time
import uuid
from pathlib import Path

from prometheus_client import Counter, Gauge, Histogram, start_http_server
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import text

from app.auth import ActorContext
from app.config import settings
from app.dependencies import build_envelope_encryption, build_object_storage
from app.domain.evidence_bundle import _clear_directory
from app.services.bundle_processor import BundleProcessor
from app.services.deletion_service import DiagnosisDeletionService

logger = get_logger("diagnosis-worker")
TASKS_TOTAL = Counter("diagnosis_worker_tasks_total", "诊断 Worker 任务结果", ["status"])
TASK_DURATION = Histogram("diagnosis_worker_task_duration_seconds", "诊断 Worker 单任务耗时")
QUEUE_DEPTH = Gauge("diagnosis_worker_queue_depth", "等待处理的诊断任务数")
MAINTENANCE_TOTAL = Counter("diagnosis_worker_maintenance_total", "诊断生命周期维护结果", ["operation"])


async def recover_stuck_tasks(database: DatabaseManager) -> int:
    """恢复进程崩溃后超时停留在 running 的任务。"""

    async for session in database.get_session():
        result = await session.execute(
            text(
                """
                UPDATE diagnosis_processing_job
                SET status = 'pending', locked_by = NULL, locked_at = NULL,
                    failure_code = 'worker_lease_expired',
                    failure_message = 'Worker 租约超时，任务已自动恢复',
                    updated_at = CURRENT_TIMESTAMP
                WHERE status = 'running'
                  AND locked_at < CURRENT_TIMESTAMP - make_interval(secs => :stale_seconds)
                RETURNING task_id
                """
            ),
            {"stale_seconds": settings.DIAGNOSIS_WORKER_STALE_SECONDS},
        )
        return len(result.all())
    return 0


async def clean_orphan_work_directories() -> int:
    """清理异常退出遗留的 Worker 明文目录。"""

    work_root = Path(settings.DIAGNOSIS_OBJECT_STORAGE_ROOT).resolve() / "work"
    if not work_root.is_dir():
        return 0
    removed = 0
    for path in work_root.iterdir():
        if not path.is_dir() or path.is_symlink():
            continue
        await asyncio.to_thread(_clear_directory, path)
        removed += 1
    return removed


async def next_task(database: DatabaseManager) -> str | None:
    """使用 SKIP LOCKED 读取一个可执行任务标识。"""

    async for session in database.get_session():
        depth_result = await session.execute(
            text("SELECT COUNT(*) FROM diagnosis_processing_job WHERE status = 'pending' AND available_at <= CURRENT_TIMESTAMP")
        )
        QUEUE_DEPTH.set(depth_result.scalar_one())
        result = await session.execute(
            text(
                """
                SELECT task_id
                FROM diagnosis_processing_job
                WHERE status = 'pending'
                  AND available_at <= CURRENT_TIMESTAMP
                  AND attempts < max_attempts
                ORDER BY available_at, created_at
                FOR UPDATE SKIP LOCKED
                LIMIT 1
                """
            )
        )
        task_id = result.scalar_one_or_none()
        return str(task_id) if task_id else None
    return None


async def process_task(database: DatabaseManager, task_id: str, worker_id: str) -> None:
    """在独立事务中处理一个任务。"""

    async for session in database.get_session():
        processor = BundleProcessor(
            session=session,
            storage=build_object_storage(),
            encryption=build_envelope_encryption(),
            worker_id=worker_id,
        )
        started = time.monotonic()
        try:
            await processor.process(task_id=task_id)
            TASKS_TOTAL.labels(status="completed").inc()
        except BaseException:
            TASKS_TOTAL.labels(status="failed").inc()
            raise
        finally:
            TASK_DURATION.observe(time.monotonic() - started)


async def run_lifecycle_maintenance(database: DatabaseManager) -> None:
    """清理过期分片并执行受 Legal Hold 保护的到期删除。"""

    storage = build_object_storage()
    expired_upload_ids: list[str] = []
    async for session in database.get_session():
        expired = await session.execute(
            text(
                """
                UPDATE diagnosis_upload_session
                SET status = 'expired', updated_at = CURRENT_TIMESTAMP
                WHERE expires_at <= CURRENT_TIMESTAMP
                  AND status IN ('initiated', 'uploading', 'failed', 'completing')
                RETURNING upload_id
                """
            )
        )
        expired_upload_ids = [str(row[0]) for row in expired.all()]
    for upload_id in expired_upload_ids:
        await storage.delete_multipart(upload_id)
    if expired_upload_ids:
        MAINTENANCE_TOTAL.labels(operation="expired_upload_cleanup").inc(len(expired_upload_ids))

    async for session in database.get_session():
        await session.execute(
            text(
                """
                INSERT INTO diagnosis_deletion_job (
                    deletion_id, tenant_id, session_id, requested_by, deletion_results, trace_id
                )
                SELECT gen_random_uuid(), s.tenant_id, s.session_id, 'retention-janitor',
                       '{"request_reason":"retention_expired"}'::jsonb, s.trace_id
                FROM diagnosis_session s
                WHERE s.legal_hold = false
                  AND s.status <> 'deleted'
                  AND EXISTS (
                      SELECT 1 FROM diagnostic_evidence_bundle b
                      WHERE b.session_id = s.session_id
                        AND b.processing_status <> 'deleted'
                  )
                  AND NOT EXISTS (
                      SELECT 1 FROM diagnostic_evidence_bundle b
                      WHERE b.session_id = s.session_id
                        AND b.processing_status <> 'deleted'
                        AND (b.retention_until > CURRENT_TIMESTAMP OR b.legal_hold = true)
                  )
                ON CONFLICT (session_id) DO NOTHING
                """
            )
        )
        pending_result = await session.execute(
            text(
                """
                SELECT tenant_id, session_id
                FROM diagnosis_deletion_job
                WHERE status IN ('deletion_pending', 'deletion_failed') AND attempts < 3
                ORDER BY created_at
                LIMIT 20
                """
            )
        )
        pending = [(str(row[0]), str(row[1])) for row in pending_result.all()]
    for tenant_id, session_id in pending:
        async for session in database.get_session():
            actor = ActorContext(
                tenant_id=tenant_id,
                user_id="retention-janitor",
                roles=frozenset({"diagnosis_worker"}),
            )
            await DiagnosisDeletionService(session=session, storage=storage).execute(
                actor=actor,
                session_id=session_id,
            )
            MAINTENANCE_TOTAL.labels(operation="retention_delete").inc()


async def run() -> None:
    """持续轮询持久化任务队列。"""

    if settings.DIAGNOSIS_OBJECT_STORAGE_MODE != "local":
        raise RuntimeError("当前镜像未配置生产对象存储适配器，默认拒绝启动 Worker")
    database = DatabaseManager(settings.DATABASE_URL)
    start_http_server(settings.DIAGNOSIS_WORKER_METRICS_PORT)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    recovered = await recover_stuck_tasks(database)
    cleaned_work_directories = await clean_orphan_work_directories()
    logger.info(
        event="diagnosis_worker_started",
        worker_id=worker_id,
        recovered_tasks=recovered,
        cleaned_work_directories=cleaned_work_directories,
    )
    try:
        next_maintenance_at = 0.0
        while True:
            if time.monotonic() >= next_maintenance_at:
                try:
                    await run_lifecycle_maintenance(database)
                except Exception as exc:
                    logger.exception(event="diagnosis_worker_maintenance_failed", error=exc)
                    MAINTENANCE_TOTAL.labels(operation="failed").inc()
                next_maintenance_at = time.monotonic() + settings.DIAGNOSIS_WORKER_MAINTENANCE_SECONDS
            task_id = await next_task(database)
            if task_id is None:
                await asyncio.sleep(settings.DIAGNOSIS_WORKER_POLL_SECONDS)
                continue
            try:
                await process_task(database, task_id, worker_id)
            except Exception as exc:
                logger.exception(
                    event="diagnosis_worker_task_failed",
                    task_id=task_id,
                    worker_id=worker_id,
                    error=exc,
                )
    finally:
        await database.close()


if __name__ == "__main__":
    asyncio.run(run())
