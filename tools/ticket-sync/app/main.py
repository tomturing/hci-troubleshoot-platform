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
from datetime import datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

from .config import Config
from .extractor import extract
from .fetcher import (
    RemoteArchive,
    fetch,
    list_remote_entries,
    list_today_archives,
    looks_like_non_log_bundle,
)
from .maintenance import cleanup_expired, enforce_capacity, has_usable_logs, write_sync_meta
from .models import (
    RUNNING_PHASES,
    TICKET_RE,
    ErrorCode,
    ExtractStats,
    Phase,
    TicketError,
    validate_ticket_id,
)
from .publisher import cleanup_workspace, prune_trash, publish
from .store import Store

logger = logging.getLogger("ticket-sync")

# 宿主机时区固定为东八区，接口时间统一带偏移量，前端不做时区推断。
TZ = ZoneInfo("Asia/Shanghai")
VERSION = "1.0.0"

# 失败退避阶梯（秒）：10min → 30min → 2h → 6h，之后固定 6h。
# 存在的理由：自动巡检每 30 秒一轮，而"本地目录不存在"会被当成待同步。
# 一个必然失败的包（层数超限、体积超限、坏包）因此会被无限重拉：
# 实测单工单 192 次、累计约 450 GiB 无效流量，并把正常工单挤在队列后面。
RETRY_BACKOFF_SECONDS = (600, 1800, 7200, 21600)


class JobConflict(Exception):
    """同一工单已有任务在执行。"""


@dataclass
class RetryState:
    """按工单累计的失败退避状态。

    存内存而非磁盘：与服务重启后任务表丢失的口径一致，重启即重新给一次机会。
    """

    failures: int = 0
    next_retry_at: datetime | None = None

    def delay_for_next(self) -> int:
        index = min(self.failures, len(RETRY_BACKOFF_SECONDS) - 1)
        return RETRY_BACKOFF_SECONDS[index]


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
        # 退避状态按工单保留，跨任务生命周期，直到该工单成功发布。
        self._retries: dict[str, RetryState] = {}
        # 持久化：任务历史与产物用量。内存表仍是运行态权威，DB 只是镜像。
        self._store = Store(cfg.state_db())
        interrupted = self._store.mark_interrupted()
        if interrupted:
            logger.warning("上次进程遗留 %d 个未完成任务，已标记为 %s", interrupted, Phase.INTERRUPTED.value)
        self._store.prune_jobs()
        self._history: dict[str, Job] = {}
        self._load_history()

    def close(self) -> None:
        self._store.close()

    def _load_history(self) -> None:
        """把 SQLite 里的任务历史还原成轻量 Job，供 `/artifacts`、`/jobs` 查询。

        这些 Job 没有 asyncio.Task、也不在 RUNNING_PHASES 里，只用于展示与追溯。
        """
        for row in self._store.load_jobs():
            try:
                job = Job(
                    ticket_id=row["ticket_id"],
                    job_id=row["job_id"],
                    phase=Phase(row["phase"]),
                    accepted_at=datetime.fromtimestamp(row["accepted_at"], TZ),
                    updated_at=datetime.fromtimestamp(row["updated_at"], TZ),
                    started_at=(
                        datetime.fromtimestamp(row["started_at"], TZ) if row["started_at"] else None
                    ),
                    received_bytes=row["received_bytes"],
                    total_bytes=row["total_bytes"],
                    extracted_bytes=row["extracted_bytes"],
                    file_count=row["file_count"],
                    current_archive=row["current_archive"],
                    error_code=ErrorCode(row["error_code"]) if row["error_code"] else None,
                    message=row["message"],
                )
            except (ValueError, KeyError, TypeError) as exc:
                logger.warning("跳过无法还原的历史任务 ticket=%s error=%s", row.get("ticket_id"), exc)
                continue
            self._history[job.ticket_id] = job

    def _persist(self, job: Job) -> None:
        """把任务快照镜像到 SQLite。写失败只记日志，不影响同步主流程。"""
        self._store.upsert_job(
            ticket_id=job.ticket_id,
            job_id=job.job_id,
            phase=job.phase.value,
            accepted_at=job.accepted_at,
            updated_at=job.updated_at,
            message=job.message,
            received_bytes=job.received_bytes,
            total_bytes=job.total_bytes,
            extracted_bytes=job.extracted_bytes,
            file_count=job.file_count,
            current_archive=job.current_archive,
            error_code=job.error_code.value if job.error_code else None,
            started_at=job.started_at,
        )

    async def create(self, ticket_id: str, request_id: str = "-") -> Job:
        async with self._lock:
            job = self._create_locked(ticket_id)
        self._start(job, request_id)
        return job

    async def create_auto(self, archive: RemoteArchive) -> Job | None:
        """为尚未同步或远端已更新的归档创建自动任务。

        检查本地产物和注册任务与创建动作共用同一把锁，扫描轮询与手动按钮不会
        在检查与入队之间产生竞态。返回 None 表示当前无需创建任务。
        """
        async with self._lock:
            existing = self.jobs.get(archive.ticket_id)
            if existing is not None and existing.phase in RUNNING_PHASES:
                return None

            final_dir = self.cfg.root / archive.ticket_id
            if final_dir.is_dir():
                # 已经发布成功，历史退避记录可以清掉。
                self._retries.pop(archive.ticket_id, None)
                return None

            state = self._retries.get(archive.ticket_id)
            if state is not None and state.next_retry_at is not None and _now() < state.next_retry_at:
                # 仍在退避窗口内：本轮巡检跳过，避免把同一个坏包反复重拉。
                return None

            job = self._create_locked(archive.ticket_id)
        self._start(job, "auto")
        logger.info(
            "ticket=%s job=%s 自动发现待同步归档 path=%s size=%d mtime=%.3f",
            archive.ticket_id,
            job.job_id,
            archive.path,
            archive.size,
            archive.mtime,
        )
        return job

    def _create_locked(self, ticket_id: str) -> Job:
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
        self._persist(job)
        return job

    def _start(self, job: Job, request_id: str) -> None:
        # request_id 一路带进任务日志，一次点击就能串起受理与执行两段。
        logger.info(
            "request_id=%s ticket=%s job=%s 受理 排队中 当前任务数=%d",
            request_id,
            job.ticket_id,
            job.job_id,
            len(self.jobs),
        )
        job.task = asyncio.create_task(self._run(job), name=f"ticket-sync:{job.ticket_id}")

    def get(self, ticket_id: str) -> Job | None:
        # 内存优先；重启后内存为空，用 SQLite 历史回答"上次同步到哪一步"。
        return self.jobs.get(ticket_id) or self._history.get(ticket_id)

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
        self._persist(job)
        logger.info("ticket=%s job=%s 已取消", ticket_id, job.job_id)
        return True

    def active_count(self) -> int:
        return sum(1 for job in self.jobs.values() if job.phase in RUNNING_PHASES)

    def active_ticket_ids(self) -> set[str]:
        return {job.ticket_id for job in self.jobs.values() if job.phase in RUNNING_PHASES}

    def _forget_ticket(self, ticket_id: str) -> None:
        """产物被删除后同步清理记账与历史，避免 DB 与磁盘长期漂移。"""
        self._store.delete_artifact(ticket_id)
        self._history.pop(ticket_id, None)

    async def prune_expired(self, now: datetime) -> int:
        """清理过期产物。

        两个必须遵守的约束：
        1. 磁盘遍历与 rmtree 都是阻塞 IO（冷缓存下全盘统计可达数分钟），
           必须放进线程池。直接在协程里跑会把事件循环钉死，实测 2026-10-31
           00:00:07 到 00:03:35 之间服务完全静默，连 /health 都没有响应。
        2. 不在注册表锁内执行。清理期间不应阻塞入队与前端查询；删除目标已经
           排除活跃工单，而发布是原子 rename，最坏情况只是多删一个旧目录。
        """
        active = self.active_ticket_ids()
        return await asyncio.to_thread(
            cleanup_expired,
            self.cfg.root,
            now,
            self.cfg.retention_days,
            active,
            self._forget_ticket,
        )

    async def prune_capacity(self, now: datetime) -> tuple[int, int]:
        """总量超过上限时淘汰，返回 (删除数, 释放字节)。理由同上。"""
        active = self.active_ticket_ids()
        return await asyncio.to_thread(
            enforce_capacity,
            self.cfg.root,
            self.cfg.max_total_bytes,
            self.cfg.min_keep_days,
            now,
            active,
            self._store,
            self._forget_ticket,
        )

    async def list_artifacts(self) -> list[dict[str, Any]]:
        """合并本地已发布目录与内存任务，供 DSH 控制台展示。"""
        async with self._lock:
            local: dict[str, datetime] = {}
            for entry in self.cfg.root.iterdir():
                if not entry.is_dir() or entry.is_symlink() or TICKET_RE.fullmatch(entry.name) is None:
                    continue
                try:
                    local[entry.name] = datetime.fromtimestamp(entry.stat().st_mtime, TZ)
                except OSError:
                    logger.warning("读取本地工单目录状态失败 ticket=%s", entry.name)

            ticket_ids = set(local) | set(self.jobs) | set(self._history)
            result: list[dict[str, Any]] = []
            for ticket_id in sorted(ticket_ids, reverse=True):
                job = self.jobs.get(ticket_id) or self._history.get(ticket_id)
                retry = self._retries.get(ticket_id)
                result.append(
                    {
                        "ticketId": ticket_id,
                        "phase": job.phase.value if job else Phase.READY.value,
                        "directoryReady": ticket_id in local,
                        "directoryUpdatedAt": local[ticket_id].isoformat() if ticket_id in local else None,
                        "job": job.to_payload() if job else None,
                        # 退避状态对外可见：否则页面显示"失败"但后台不再重试，
                        # 用户无从判断到底还会不会自己好。
                        "retry": (
                            {
                                "failures": retry.failures,
                                "nextRetryAt": retry.next_retry_at.isoformat() if retry.next_retry_at else None,
                            }
                            if retry is not None
                            else None
                        ),
                    }
                )
            return result

    async def _run(self, job: Job) -> None:
        ticket_id = job.ticket_id
        work_dir = self.cfg.tmp_dir() / f"{ticket_id}-{job.job_id}"

        try:
            async with self._slots:
                job.started_at = _now()
                job.phase = Phase.FETCHING
                job.updated_at = _now()
                self._persist(job)

                await self._reject_non_log_payload(job)

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
                self._persist(job)

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

                # 把"何时同步完、多大、有没有日志"固化成显式事实：
                # 清理与容量判断不再依赖目录 mtime，也不用每天全盘扫描。
                has_logs = has_usable_logs(self.cfg.root / ticket_id)
                write_sync_meta(
                    self.cfg.root / ticket_id,
                    completedAt=_now().timestamp(),
                    bytes=stats.bytes,
                    files=stats.files,
                    hasLogs=has_logs,
                )

                job.extracted_bytes = stats.bytes
                job.file_count = stats.files
                job.current_archive = None
                job.phase = Phase.READY
                # 解压成功但有归档解不开（如 RAR5 新压缩方法）：必须让用户看见，
                # 否则"已就绪"会掩盖"少了一份转储"的事实。
                if stats.unreadable_archives:
                    job.message = (
                        f"{stats.unreadable_archives} 个归档工具无法解压，已保留原压缩文件"
                    )
                # 「流程跑通」不等于「产物可用」：实测 Q2026102101911 解出 65 GiB
                # 的 kasi/kasi.dump，状态却是 ready，用户点进去无日志可看。
                if not has_logs:
                    suffix = "产物中未发现日志清单（log_list.json / sp_*），可能仅含转储文件"
                    job.message = f"{job.message}；{suffix}" if job.message else suffix
                # 深度上限从「整单失败」改为「停止下钻」后，截断信息不能静默：
                # 用户需要知道产物里少了更深层的附件，而不是以为日志齐了。
                if stats.depth_truncated:
                    suffix = (
                        f"{stats.depth_truncated} 个归档超出嵌套层数上限，未继续解压"
                    )
                    job.message = f"{job.message}；{suffix}" if job.message else suffix
                job.updated_at = _now()
                self._persist(job)
                # 产物用量单独记账：容量判断只读这张表，不再遍历目录算体积。
                try:
                    dir_mtime = (self.cfg.root / ticket_id).stat().st_mtime
                except OSError:
                    dir_mtime = None
                self._store.upsert_artifact(
                    ticket_id,
                    stats.bytes,
                    stats.files,
                    has_logs,
                    _now().timestamp(),
                    dir_mtime,
                )
                self._retries.pop(ticket_id, None)
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
            self._persist(job)
            logger.error("ticket=%s job=%s 失败 %s detail=%s", ticket_id, job.job_id, error, error.detail)
            self._note_failure(ticket_id)
        except asyncio.CancelledError:
            job.phase = Phase.CANCELLED
            job.message = "已取消"
            job.updated_at = _now()
            self._persist(job)
            raise
        except Exception as error:  # noqa: BLE001 - 兜底保证任务不会卡在 running
            job.phase = Phase.FAILED
            job.error_code = ErrorCode.EXTRACT_FAILED
            job.message = "同步失败，请查看服务日志"
            job.updated_at = _now()
            self._persist(job)
            logger.exception("ticket=%s job=%s 未预期异常 %s", ticket_id, job.job_id, error)
            self._note_failure(ticket_id)
        finally:
            # 成功时 work_dir 已被 rename 走，这里是空操作；
            # 失败与取消时清理半成品，避免 .tmp 累积。
            cleanup_workspace(work_dir)

    async def _reject_non_log_payload(self, job: Job) -> None:
        """下载前识别非日志载荷（vmcore 等），避免为几十 GiB 的转储白耗资源。

        探测本身只是优化：ssh 不可用、unzip 缺失等失败一律放行，
        绝不能因为"探测失败"把正常工单挡在门外。
        """
        try:
            entries = await list_remote_entries(self.cfg, job.ticket_id)
        except Exception as error:  # noqa: BLE001 - 探测失败必须降级放行
            logger.warning(
                "ticket=%s job=%s 归档内容探测失败，按日志包继续 error=%s",
                job.ticket_id,
                job.job_id,
                error,
            )
            return

        if looks_like_non_log_bundle(entries):
            raise TicketError(
                ErrorCode.NON_LOG_PAYLOAD,
                "该工单包不是日志包（仅含转储文件），已跳过",
                f"entries={entries[:5]}",
            )

    def _note_failure(self, ticket_id: str) -> None:
        """登记一次失败并按阶梯推迟下一次自动重试。

        手动 POST /jobs 不受退避限制：用户显式点击代表新的判断，
        自动巡检则必须让路，否则坏包会把带宽和磁盘反复烧掉。
        """
        state = self._retries.setdefault(ticket_id, RetryState())
        delay = state.delay_for_next()
        state.failures += 1
        state.next_retry_at = _now() + timedelta(seconds=delay)
        logger.warning(
            "ticket=%s 失败退避 第%d次 下次自动重试=%s 间隔=%ds",
            ticket_id,
            state.failures,
            state.next_retry_at.isoformat(),
            delay,
        )

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
    maintenance_task = asyncio.create_task(
        _maintenance_loop(cfg, registry),
        name="ticket-sync:maintenance",
    )

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
        job.task for job in list(registry.jobs.values()) if job.task is not None and not job.task.done()
    ]
    maintenance_task.cancel()
    pending.append(maintenance_task)
    for task in pending:
        task.cancel()
    if pending:
        # 必须等取消真正落地：kill 子进程是在 CancelledError 处理里完成的，
        # 不等就直接关事件循环，会留下仍在写盘的 rsync/unzip 孤儿进程。
        await asyncio.wait(pending, timeout=10)
    # 先让被取消的任务把失败/取消状态写进库，再关连接；顺序反了会丢最后一条状态。
    registry.close()
    logger.info("服务退出 已终止任务数=%d", len(pending))


async def _maintenance_loop(cfg: Config, registry: JobRegistry) -> None:
    """每 30 秒扫描远端归档，并按本地时间每日执行保留期清理。"""
    last_cleanup_date = None
    # 首次等待一个周期，避免服务刚启动时与网络/挂载初始化同时争抢资源。
    await asyncio.sleep(max(1, cfg.auto_sync_interval_seconds))
    while True:
        try:
            now = _now()
            if last_cleanup_date != now.date():
                started = time.monotonic()
                removed = await registry.prune_expired(now)
                logger.info("本地保留期检查完成 retention_days=%d removed=%d", cfg.retention_days, removed)
                cap_removed, cap_freed = await registry.prune_capacity(now)
                elapsed = time.monotonic() - started
                logger.info(
                    "容量检查完成 上限=%.1fGiB 删除=%d 释放=%.1fGiB elapsed_ms=%.0f",
                    cfg.max_total_bytes / 1024**3,
                    cap_removed,
                    cap_freed / 1024**3,
                    elapsed * 1000,
                )
                # 维护耗时必须可观测：它曾经在冷缓存下达到 3.5 分钟并阻塞事件循环。
                _MAINTENANCE_STATS["last_run_timestamp"] = now.timestamp()
                _MAINTENANCE_STATS["last_duration_seconds"] = elapsed
                _MAINTENANCE_STATS["expired_removed"] += removed
                _MAINTENANCE_STATS["capacity_freed_bytes"] += cap_freed
                last_cleanup_date = now.date()

            if cfg.auto_sync_enabled:
                archives = await list_today_archives(cfg, now)
                for archive in archives:
                    try:
                        await registry.create_auto(archive)
                    except JobConflict:
                        # 手动按钮刚好先入队时，自动扫描只需跳过本轮。
                        continue
        except asyncio.CancelledError:
            raise
        except TicketError as error:
            logger.error("自动维护失败 code=%s message=%s detail=%s", error.code, error.message, error.detail)
        except Exception as error:  # noqa: BLE001 - 后台循环不能因一次扫描异常退出
            logger.exception("自动维护出现未预期异常 error=%s", error)

        await asyncio.sleep(max(1, cfg.auto_sync_interval_seconds))


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


@app.get("/artifacts")
async def list_artifacts(request: Request) -> dict[str, Any]:
    """返回已同步目录和仍在运行/最近结束的任务，目录路径不暴露宿主机绝对路径。"""
    registry: JobRegistry = request.app.state.registry
    return {"items": await registry.list_artifacts()}


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


# 维护任务的最近一次结果，供 /metrics 暴露（进程内，重启归零）。
_MAINTENANCE_STATS: dict[str, float] = {
    "last_run_timestamp": 0.0,
    "last_duration_seconds": 0.0,
    "expired_removed": 0.0,
    "capacity_freed_bytes": 0.0,
}


@app.get("/metrics")
async def metrics(request: Request) -> Response:
    """Prometheus 文本格式指标。

    只暴露真正会用来告警的少量指标，不做"有指标没告警"的摆设：
    磁盘余量（盘满是所有同步失败的共同前置条件）、队列深度、任务分布、
    维护耗时（2026-10-31 这里曾把事件循环钉死 3.5 分钟，必须有数可查）。
    """
    cfg: Config = request.app.state.cfg
    registry: JobRegistry = request.app.state.registry
    free = shutil.disk_usage(cfg.root).free

    phases: dict[str, int] = {}
    for job in registry.jobs.values():
        key = getattr(job.phase, "value", str(job.phase))
        phases[key] = phases.get(key, 0) + 1

    lines = [
        "# HELP ticket_sync_disk_free_bytes 产物根目录所在磁盘剩余字节",
        "# TYPE ticket_sync_disk_free_bytes gauge",
        f"ticket_sync_disk_free_bytes {free}",
        "# HELP ticket_sync_active_jobs 正在运行或排队中的任务数",
        "# TYPE ticket_sync_active_jobs gauge",
        f"ticket_sync_active_jobs {registry.active_count()}",
        "# HELP ticket_sync_jobs 内存中的任务数（按阶段）",
        "# TYPE ticket_sync_jobs gauge",
    ]
    for phase, count in sorted(phases.items()):
        lines.append(f'ticket_sync_jobs{{phase="{phase}"}} {count}')
    lines.extend(
        [
            "# HELP ticket_sync_maintenance_last_run_timestamp 最近一次维护执行的 Unix 时间",
            "# TYPE ticket_sync_maintenance_last_run_timestamp gauge",
            f"ticket_sync_maintenance_last_run_timestamp {_MAINTENANCE_STATS['last_run_timestamp']:.0f}",
            "# HELP ticket_sync_maintenance_duration_seconds 最近一次维护耗时",
            "# TYPE ticket_sync_maintenance_duration_seconds gauge",
            f"ticket_sync_maintenance_duration_seconds {_MAINTENANCE_STATS['last_duration_seconds']:.3f}",
            "# HELP ticket_sync_expired_removed_total 累计删除的过期目录数",
            "# TYPE ticket_sync_expired_removed_total counter",
            f"ticket_sync_expired_removed_total {_MAINTENANCE_STATS['expired_removed']:.0f}",
            "# HELP ticket_sync_capacity_freed_bytes_total 累计容量淘汰释放的字节数",
            "# TYPE ticket_sync_capacity_freed_bytes_total counter",
            f"ticket_sync_capacity_freed_bytes_total {_MAINTENANCE_STATS['capacity_freed_bytes']:.0f}",
        ]
    )
    return Response("\n".join(lines) + "\n", media_type="text/plain; version=0.0.4")


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
