"""FastAPI 服务：受理同步任务、查询进度、取消。

状态全部放在进程内存：单实例部署，重启后任务丢失，用户重新点一次即可。
这是有意的取舍——为内部工具引入持久化队列，复杂度远高于它解决的问题。

单实例 + 内存状态带来两个必须显式处理的点：
1. 重启后 .tmp 里的半成品必须清空，否则会一直占着磁盘；
2. 取消要真的杀掉子进程，不能只把状态改成 cancelled。
"""

from __future__ import annotations

import asyncio
import logging
import shutil
import time
import uuid
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Config
from .extractor import extract
from .fetcher import fetch
from .models import (
    RUNNING_PHASES,
    ErrorCode,
    ExtractStats,
    Phase,
    TicketError,
    validate_ticket_id,
)
from .publisher import cleanup_workspace, prune_trash, publish

logger = logging.getLogger("ticket-sync")

# 宿主机时区固定为东八区，接口时间统一带偏移量，前端不做时区推断。
TZ = ZoneInfo("Asia/Shanghai")
VERSION = "1.0.0"


class JobConflict(Exception):
    """同一工单已有任务在执行。"""


@dataclass
class Job:
    """一次同步任务的运行期快照。"""

    ticket_id: str
    job_id: str
    phase: Phase
    accepted_at: datetime
    updated_at: datetime
    started_at: datetime | None = None
    received_bytes: int = 0
    total_bytes: int = 0
    extracted_bytes: int = 0
    file_count: int = 0
    current_archive: str | None = None
    error_code: ErrorCode | None = None
    message: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)

    def to_payload(self) -> dict[str, Any]:
        return {
            "ticketId": self.ticket_id,
            "phase": self.phase.value,
            "receivedBytes": self.received_bytes,
            "totalBytes": self.total_bytes,
            "extractedBytes": self.extracted_bytes,
            "fileCount": self.file_count,
            "currentArchive": self.current_archive,
            "errorCode": self.error_code.value if self.error_code else None,
            "message": self.message,
            "acceptedAt": self.accepted_at.isoformat(),
            "startedAt": self.started_at.isoformat() if self.started_at else None,
            "updatedAt": self.updated_at.isoformat(),
        }


class JobRegistry:
    """任务表与并发闸门。

    - 同一工单串行：重复提交返回 409，避免两个 rsync 抢同一个目标目录；
    - 全局最多 concurrency 个任务同时跑，超出的在 queued 等信号量。
    """

    def __init__(self, cfg: Config) -> None:
        self.cfg = cfg
        self.jobs: dict[str, Job] = {}
        self._slots = asyncio.Semaphore(cfg.concurrency)
        self._lock = asyncio.Lock()

    async def create(self, ticket_id: str, request_id: str = "-") -> Job:
        async with self._lock:
            existing = self.jobs.get(ticket_id)
            if existing is not None and existing.phase in RUNNING_PHASES:
                raise JobConflict

            now = _now()
            job = Job(
                ticket_id=ticket_id,
                job_id=uuid.uuid4().hex[:8],
                phase=Phase.QUEUED,
                accepted_at=now,
                updated_at=now,
            )
            self.jobs[ticket_id] = job

        # request_id 一路带进任务日志，一次点击就能串起受理与执行两段。
        logger.info(
            "request_id=%s ticket=%s job=%s 受理 排队中 当前任务数=%d",
            request_id,
            ticket_id,
            job.job_id,
            len(self.jobs),
        )
        job.task = asyncio.create_task(self._run(job), name=f"ticket-sync:{ticket_id}")
        return job

    def get(self, ticket_id: str) -> Job | None:
        return self.jobs.get(ticket_id)

    async def cancel(self, ticket_id: str) -> bool:
        job = self.jobs.get(ticket_id)
        if job is None:
            return False
        if job.phase not in RUNNING_PHASES:
            # 已结束的任务取消是幂等操作，不视为错误。
            return True

        if job.task is not None and not job.task.done():
            job.task.cancel()
            try:
                await job.task
            except asyncio.CancelledError:
                pass

        job.phase = Phase.CANCELLED
        job.message = "已取消"
        job.updated_at = _now()
        logger.info("ticket=%s job=%s 已取消", ticket_id, job.job_id)
        return True

    def active_count(self) -> int:
        return sum(1 for job in self.jobs.values() if job.phase in RUNNING_PHASES)

    async def _run(self, job: Job) -> None:
        ticket_id = job.ticket_id
        work_dir = self.cfg.tmp_dir() / f"{ticket_id}-{job.job_id}"

        try:
            async with self._slots:
                job.started_at = _now()
                job.phase = Phase.FETCHING
                job.updated_at = _now()

                archive = await fetch(
                    ticket_id,
                    self.cfg,
                    work_dir,
                    job.job_id,
                    on_progress=self._progress_writer(job),
                )

                if not self.cfg.archive_password:
                    raise TicketError(
                        ErrorCode.BAD_PASSWORD,
                        "未配置归档密码",
                        "TICKET_ARCHIVE_PASSWORD 为空",
                    )

                job.phase = Phase.EXTRACTING
                job.updated_at = _now()

                stats = await extract(
                    ticket_id,
                    archive,
                    work_dir,
                    self.cfg,
                    job.job_id,
                    on_progress=self._extract_writer(job),
                    on_archive=self._archive_writer(job),
                )

                replaced = publish(work_dir, self.cfg.root / ticket_id, self.cfg.trash_dir())
                prune_trash(self.cfg.trash_dir())

                job.extracted_bytes = stats.bytes
                job.file_count = stats.files
                job.current_archive = None
                job.phase = Phase.READY
                job.updated_at = _now()
                logger.info(
                    "ticket=%s job=%s 完成 层数=%d 文件=%d 字节=%d 旧版本=%s",
                    ticket_id,
                    job.job_id,
                    stats.depth,
                    stats.files,
                    stats.bytes,
                    replaced or "无",
                )

        except JobConflict:
            # 不会发生在执行路径上，保留分支避免异常逃逸。
            raise
        except TicketError as error:
            job.phase = Phase.FAILED
            job.error_code = error.code
            job.message = error.message
            job.updated_at = _now()
            logger.error("ticket=%s job=%s 失败 %s detail=%s", ticket_id, job.job_id, error, error.detail)
        except asyncio.CancelledError:
            job.phase = Phase.CANCELLED
            job.message = "已取消"
            job.updated_at = _now()
            raise
        except Exception as error:  # noqa: BLE001 - 兜底保证任务不会卡在 running
            job.phase = Phase.FAILED
            job.error_code = ErrorCode.EXTRACT_FAILED
            job.message = "同步失败，请查看服务日志"
            job.updated_at = _now()
            logger.exception("ticket=%s job=%s 未预期异常 %s", ticket_id, job.job_id, error)
        finally:
            # 成功时 work_dir 已被 rename 走，这里是空操作；
            # 失败与取消时清理半成品，避免 .tmp 累积。
            cleanup_workspace(work_dir)

    def _progress_writer(self, job: Job):
        def callback(received: int, total: int) -> None:
            job.received_bytes = received
            if total > 0:
                job.total_bytes = total
            job.updated_at = _now()

        return callback

    def _extract_writer(self, job: Job):
        def callback(stats: ExtractStats) -> None:
            job.extracted_bytes = stats.bytes
            job.file_count = stats.files
            job.updated_at = _now()

        return callback

    def _archive_writer(self, job: Job):
        def callback(name: str) -> None:
            job.current_archive = name
            job.updated_at = _now()

        return callback


class JobCreatePayload(BaseModel):
    """创建任务请求。字段名与设计文档 7.1 对齐。"""

    model_config = ConfigDict(populate_by_name=True)

    # 不设长度约束：过短或超长同样由 validate_ticket_id 统一判为 400
    # invalid-ticket-id，避免一部分非法输入走 FastAPI 的 422，前端要多判一种。
    ticket_id: str = Field(alias="ticketId")


@asynccontextmanager
async def lifespan(app: FastAPI):
    _configure_logging()
    cfg = Config.from_env()
    _prepare_dirs(cfg)
    registry = JobRegistry(cfg)

    app.state.cfg = cfg
    app.state.registry = registry

    logger.info(
        "服务启动 version=%s root=%s concurrency=%d listen=%s:%d",
        VERSION,
        cfg.root,
        cfg.concurrency,
        cfg.listen_host,
        cfg.listen_port,
    )

    yield

    pending = [
        job.task
        for job in list(registry.jobs.values())
        if job.task is not None and not job.task.done()
    ]
    for task in pending:
        task.cancel()
    if pending:
        # 必须等取消真正落地：kill 子进程是在 CancelledError 处理里完成的，
        # 不等就直接关事件循环，会留下仍在写盘的 rsync/unzip 孤儿进程。
        await asyncio.wait(pending, timeout=10)
    logger.info("服务退出 已终止任务数=%d", len(pending))


app = FastAPI(title="HCI 工单日志同步", version=VERSION, lifespan=lifespan)


@app.middleware("http")
async def request_trace(request: Request, call_next):
    """为每个请求生成调用链 ID，便于把一次点击串成完整路径。"""
    request_id = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:12]
    request.state.request_id = request_id
    started = time.monotonic()

    response = await call_next(request)

    elapsed_ms = (time.monotonic() - started) * 1000
    response.headers["X-Request-ID"] = request_id
    # 探活每 10 秒一次，记日志只会淹没真正的用户请求。
    if request.url.path != "/health":
        logger.info(
            "request_id=%s method=%s path=%s status=%d elapsed_ms=%.0f",
            request_id,
            request.method,
            request.url.path,
            response.status_code,
            elapsed_ms,
        )
    return response


@app.exception_handler(TicketError)
async def handle_ticket_error(_request: Request, exc: TicketError) -> JSONResponse:
    return JSONResponse(
        status_code=400,
        content={"error": exc.code.value, "message": exc.message},
    )


@app.post("/jobs", status_code=202)
async def create_job(payload: JobCreatePayload, request: Request):
    registry: JobRegistry = request.app.state.registry
    ticket_id = validate_ticket_id(payload.ticket_id)

    try:
        job = await registry.create(ticket_id, request.state.request_id)
    except JobConflict:
        return JSONResponse(
            status_code=409,
            content={"error": "job-running", "message": "该工单正在同步中，请等待完成"},
        )

    return {
        "ticketId": job.ticket_id,
        "phase": job.phase.value,
        "acceptedAt": job.accepted_at.isoformat(),
    }


@app.get("/jobs/{ticket_id}")
async def get_job(ticket_id: str, request: Request):
    registry: JobRegistry = request.app.state.registry
    job = registry.get(ticket_id)
    if job is None:
        return JSONResponse(
            status_code=404,
            content={"error": "not-found", "message": "没有该工单的同步任务"},
        )
    return job.to_payload()


@app.delete("/jobs/{ticket_id}", status_code=204)
async def cancel_job(ticket_id: str, request: Request):
    registry: JobRegistry = request.app.state.registry
    found = await registry.cancel(ticket_id)
    if not found:
        return JSONResponse(
            status_code=404,
            content={"error": "not-found", "message": "没有该工单的同步任务"},
        )
    return Response(status_code=204)


@app.get("/health")
async def health(request: Request) -> dict[str, Any]:
    cfg: Config = request.app.state.cfg
    registry: JobRegistry = request.app.state.registry
    return {
        "status": "ok",
        "version": VERSION,
        "diskFreeBytes": shutil.disk_usage(cfg.root).free,
        "activeJobs": registry.active_count(),
    }


def _prepare_dirs(cfg: Config) -> None:
    """启动自检：目录可写、.tmp 清空。任何一个不满足都直接启动失败。"""
    cfg.root.mkdir(parents=True, exist_ok=True)
    cfg.tmp_dir().mkdir(parents=True, exist_ok=True)
    cfg.trash_dir().mkdir(parents=True, exist_ok=True)

    probe = cfg.root / f".write-probe-{uuid.uuid4().hex[:6]}"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError as exc:
        raise SystemExit(f"产物根目录不可写: {cfg.root} ({exc})") from exc
    finally:
        probe.unlink(missing_ok=True)

    for entry in cfg.tmp_dir().iterdir():
        if entry.is_dir():
            shutil.rmtree(entry, ignore_errors=True)
        else:
            entry.unlink(missing_ok=True)


def _configure_logging() -> None:
    """uvicorn 已配置 root logger 时不重复添加 handler。"""
    if logging.getLogger().handlers:
        return
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )


def _now() -> datetime:
    # 去掉微秒：接口里毫秒级精度没有意义，还会让时间字符串变长。
    return datetime.now(TZ).replace(microsecond=0)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("app.main:app", host="127.0.0.1", port=18080, reload=False)
