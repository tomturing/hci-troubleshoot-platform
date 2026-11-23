"""Diagnosis Worker（诊断工作进程）入口。

退出语义（滚动发布的关键）：
收到 SIGTERM 后**不再取新任务**，把正在跑的任务再给一小段时间收尾；
收不完就把任务放回 pending（清锁 + available_at 立即），新副本立刻接手。
这样发布既不丢任务，也不用等 stale 租约超时（默认 900s）才恢复。
"""

import asyncio
import os
import signal
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


async def release_task(database: DatabaseManager, task_id: str, worker_id: str, reason: str) -> bool:
    """把本 worker 正在跑的任务放回队列，让新副本立刻接手。

    只改自己锁住的行（locked_by 条件）：滚动发布时可能有多个 worker 同时退出，
    没有这个条件就会把别人正在跑的任务抢回 pending，造成同任务并行执行。
    """
    async for session in database.get_session():
        result = await session.execute(
            text(
                """
                UPDATE diagnosis_processing_job
                SET status = 'pending', locked_by = NULL, locked_at = NULL,
                    available_at = CURRENT_TIMESTAMP,
                    failure_code = :reason,
                    failure_message = 'Worker 收到退出信号，任务已交还给队列',
                    updated_at = CURRENT_TIMESTAMP
                WHERE task_id = :task_id
                  AND status = 'running'
                  AND locked_by = :worker_id
                RETURNING task_id
                """
            ),
            {"task_id": task_id, "worker_id": worker_id, "reason": reason},
        )
        released = len(result.all()) > 0
        TASKS_TOTAL.labels(status="released" if released else "release_missed").inc()
        logger.info(
            event="diagnosis_worker_task_released",
            message="退出前把在跑任务交还队列",
            task_id=task_id,
            worker_id=worker_id,
            released=released,
        )
        return released
    return False


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


# 收到退出信号后留给在跑任务收尾的时间。必须明显小于 helm 的 grace
# （diagnosis-worker=180s），还要挤出交还队列与关闭连接池的余量。
SHUTDOWN_DRAIN_SECONDS = 60.0


def install_shutdown_signal_handlers(stop: asyncio.Event) -> None:
    """SIGTERM/SIGINT → 置停止标志，让主循环收口。

    worker 是独立进程（不经 uvicorn），没有这个处理就只能等 kubelet 到点 SIGKILL：
    在跑的任务被直接丢弃，要等 stale 租约（默认 900s）才被捡回，用户看到的是
    「任务一直 running 然后失败」。
    """
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # Windows 事件循环不支持
            signal.signal(sig, lambda *_: stop.set())


async def run() -> None:
    """持续轮询持久化任务队列。"""

    if settings.DIAGNOSIS_OBJECT_STORAGE_MODE != "local":
        raise RuntimeError("当前镜像未配置生产对象存储适配器，默认拒绝启动 Worker")
    database = DatabaseManager(settings.DATABASE_URL)
    start_http_server(settings.DIAGNOSIS_WORKER_METRICS_PORT)
    worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"
    recovered = await recover_stuck_tasks(database)
    cleaned_work_directories = await clean_orphan_work_directories()
    stop = asyncio.Event()
    install_shutdown_signal_handlers(stop)
    logger.info(
        event="diagnosis_worker_started",
        worker_id=worker_id,
        recovered_tasks=recovered,
        cleaned_work_directories=cleaned_work_directories,
    )
    try:
        next_maintenance_at = 0.0
        while not stop.is_set():
            if time.monotonic() >= next_maintenance_at:
                try:
                    await run_lifecycle_maintenance(database)
                except Exception as exc:
                    logger.exception(event="diagnosis_worker_maintenance_failed", error=exc)
                    MAINTENANCE_TOTAL.labels(operation="failed").inc()
                next_maintenance_at = time.monotonic() + settings.DIAGNOSIS_WORKER_MAINTENANCE_SECONDS
            if stop.is_set():
                break
            task_id = await next_task(database)
            if task_id is None:
                # 用 wait_for 而不是 sleep：否则发布时要白等一个完整轮询周期才响应退出。
                try:
                    await asyncio.wait_for(stop.wait(), timeout=settings.DIAGNOSIS_WORKER_POLL_SECONDS)
                except asyncio.TimeoutError:
                    pass
                continue
            task = asyncio.create_task(process_task(database, task_id, worker_id))
            stop_wait = asyncio.create_task(stop.wait())
            try:
                done, _ = await asyncio.wait({task, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
                if task in done:
                    # 正常跑完：异常在这里就地记录，和原逻辑一致。
                    exc = task.exception()
                    if exc is not None:
                        logger.exception(
                            event="diagnosis_worker_task_failed",
                            task_id=task_id,
                            worker_id=worker_id,
                            error=exc,
                        )
                    continue
                # 收到退出信号但任务还在跑：先给它一段收尾时间
                logger.info(
                    event="diagnosis_worker_draining_task",
                    message="收到退出信号，等待在跑任务收尾",
                    task_id=task_id,
                    worker_id=worker_id,
                    drain_seconds=SHUTDOWN_DRAIN_SECONDS,
                )
                drained = False
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=SHUTDOWN_DRAIN_SECONDS)
                    drained = True
                except asyncio.TimeoutError:
                    drained = False
                if not drained:
                    # 收不完就交还队列：新副本立刻接手，不必等 900s 租约超时。
                    await release_task(database, task_id, worker_id, "worker_graceful_shutdown")
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            except Exception as exc:
                logger.exception(
                    event="diagnosis_worker_task_failed",
                    task_id=task_id,
                    worker_id=worker_id,
                    error=exc,
                )
            finally:
                stop_wait.cancel()
    finally:
        logger.info(event="diagnosis_worker_stopped", worker_id=worker_id)
        await database.close()


if __name__ == "__main__":
    asyncio.run(run())
