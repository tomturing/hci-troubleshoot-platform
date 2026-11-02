"""FastAPI 服务：受理同步任务、查询进度、取消。

状态全部放在进程内存：单实例部署，重启后任务丢失，用户重新点一次即可。
这是有意的取舍——为内部工具引入持久化队列，复杂度远高于它解决的问题。

单实例 + 内存状态带来两个必须显式处理的点：
1. 重启后 .tmp 里的半成品**不能**无脑清空：保住已下载的归档才有 rsync 续传，
   只回收超龄残留（STALE_WORKSPACE_SECONDS），否则一个坏包失败一次就要重拉几十 GiB；
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
from pathlib import Path
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
    list_recent_archives,
    list_remote_entries,
    looks_like_non_log_bundle,
)
from .maintenance import (
    cleanup_expired,
    enforce_capacity,
    has_usable_logs,
    read_sync_meta,
    reconcile_artifacts,
    write_sync_meta,
)
from .models import (
    RUNNING_PHASES,
    TICKET_RE,
    ErrorCode,
    ExtractStats,
    Phase,
    TicketError,
    validate_ticket_id,
)
from .publisher import cleanup_stale_workspaces, prepare_workspace, prune_trash, publish
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

# 自动重试的硬上限。退避只降低频率，不改变"永远不成功"这件事：阶梯到 6h 封顶后
# 实测仍有 6 个工单（12.2 GiB）在两小时一次的循环里空转，永久占用并发槽，
# 而且只要 retries 非空，扫描就一直走 7 天宽窗口。达到上限转终态，
# 由人在界面上决定要不要手动重试（手动 POST 永远不受限）。
MAX_RETRY_ATTEMPTS = 6

# 确定性失败：重试不会改变结果，直接转终态，不占用任何重试额度。
# - non-log-payload：远端就是转储包，重下多少次都不是日志；
# - bad-password：密码错是配置问题，重试等于用同一个错密码再试一次；
# - too-large / quota-exceeded：归档本身超出服务能力，重试结果相同；
# - invalid-ticket-id / not-found：远端状态确定。
TERMINAL_ERROR_CODES: frozenset[ErrorCode] = frozenset(
    {
        ErrorCode.NON_LOG_PAYLOAD,
        ErrorCode.BAD_PASSWORD,
        ErrorCode.TOO_LARGE,
        ErrorCode.QUOTA_EXCEEDED,
        ErrorCode.INVALID_TICKET_ID,
        ErrorCode.NOT_FOUND,
        # 归档损坏是数据事实，重拉多少次都是同一份坏数据（实测 Q2026102802029 的
        # 内层 gzip 流在 1 GiB 边界截断，`unzip -t` 通过、md5 两端一致）。
        # 能降级的截断在 extract 层已消化，走到这里的损坏无法通过重试改善。
        ErrorCode.ARCHIVE_CORRUPT,
    }
)

# 7 天宽窗口扫描的最小间隔。只要 retries 非空就会走宽窗口，而巡检本身是 30 秒
# 一轮：实测每 31 秒 ssh 遍历一次 98 个归档（远端 /sf/log 共 1060 个目录），
# 限流只挡住入队、挡不住这趟遍历。补扫的语义是"给失败任务第二次机会"，
# 5 分钟粒度足够，且能把 ssh 压力降到原来的 1/10。
DEEP_SCAN_MIN_INTERVAL_SECONDS = 300

# `.tmp` 半成品的保留时长。work_dir 按工单号固定后，失败残留是"续传的本钱"，
# 重启不该让 65 GiB 的下载从头再来；但也不能无限累积，超龄的由启动自检
# 与每日维护回收。选 2 小时：覆盖一次正常的慢速下载，又不至于把垃圾留过夜。
STALE_WORKSPACE_SECONDS = 2 * 3600

# 墓碑的保留时长。墓碑不是永久资产：远端归档本身有保留窗口，过了窗口它既不会
# 被扫到，留着只会让表无限增长。取 30 天，远大于 7 天保留期，留足"删掉之后
# 不该立刻被拉回来"的余量。
TOMBSTONE_RETENTION_DAYS = 30

# 单轮自动入队的硬上限。2026-11-01 的事故里，扫描窗口一放宽就把 57 个从未同步
# 过的历史归档一次性入队、吃掉 766 GiB。这个上限保证同类判据错误最多只放大成
# 5 个任务，而正常一天的新工单量远小于它；未入队的顺延到 30 秒后的下一轮。
MAX_AUTO_JOBS_PER_SCAN = 5

# 任务来源。两者的差别不在执行逻辑，而在"要不要排队"：
# - manual：用户当下的一次显式判断，必须立即开始，不占自动巡检的队列位置；
# - auto：巡检成批产生，必须受 TICKET_CONCURRENCY 约束，否则 5 个 65 GiB 的
#   rsync 会同时抢带宽与磁盘，把"2 并发 × 65 GiB"的容量假设直接打穿。
ORIGIN_AUTO = "auto"
ORIGIN_MANUAL = "manual"

# 判定"远端归档换过一版"的容差（秒）。比较对象是同一台源机上同一个文件的
# mtime（发布时写进 meta，扫描时原样读回），同源比较，理论上 0 容差即可；
# 留 1 秒覆盖 %T@ 的浮点往返与文件系统精度差异。
REMOTE_UPDATE_TOLERANCE_SECONDS = 1


@asynccontextmanager
async def _unlimited():
    """立即通过的闸门：手动任务用它代替并发信号量。

    不用 `if ...: await sem.acquire()` 手写配对——异常路径漏一次 release
    就会永久吃掉一个槽位；`async with` 由解释器保证释放。
    """
    yield


class JobConflict(Exception):
    """同一工单已有任务在执行。"""


@dataclass
class RetryState:
    """按工单累计的失败退避状态。

    内存 + SQLite 双写：退避跨重启存活，否则一次部署重启就让坏包再拉一遍。
    `failures >= MAX_RETRY_ATTEMPTS` 表示**终态**——已达到重试上限或命中确定性
    失败（登记时直接把次数推到上限），自动扫描不再考虑它，只有手动 POST 能重新触发。
    """

    failures: int = 0
    next_retry_at: datetime | None = None

    @property
    def exhausted(self) -> bool:
        # 终态统一用"次数用尽"表达，不新增数据库字段：确定性失败在登记时
        # 直接把次数推到上限，两种终态因此共用同一套判据与恢复路径。
        return self.failures >= MAX_RETRY_ATTEMPTS

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
    # 谁产生了这个任务。决定执行时是否进入并发闸门，也是排障时的第一分岔点
    # （"没人点过却自己跑起来了"→ 只能是 auto）。
    origin: str = ORIGIN_AUTO
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
            "origin": self.origin,
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
        # 已同步过、产物已按策略删除的工单。自动扫描靠它区分"没同步过"与
        # "同步过但已清理"，否则扫描窗口一放宽就会删了又拉。
        self._tombstones: set[str] = set()
        # 上次 7 天宽窗口扫描的单调时钟时间戳。宽窗口只在有失败工单时启用，
        # 但启用后若不节流，就退化成每轮巡检都全量遍历远端归档。
        self.last_deep_scan_at: float = 0.0
        # 持久化：任务历史与产物用量。内存表仍是运行态权威，DB 只是镜像。
        self._store = Store(cfg.state_db())
        interrupted = self._store.mark_interrupted()
        if interrupted:
            logger.warning("上次进程遗留 %d 个未完成任务，已标记为 %s", interrupted, Phase.INTERRUPTED.value)
        self._store.prune_jobs()
        self._history: dict[str, Job] = {}
        self._load_history()
        self._load_retries()
        self._tombstones = set(self._store.tombstone_map())

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
                    # 老库没有 origin 列（迁移前写入的历史行），来源不可考时
                    # 按 auto 展示：历史上绝大多数任务是巡检自动入队的。
                    origin=row.get("origin") or ORIGIN_AUTO,
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

    def _load_retries(self) -> None:
        """从 SQLite 恢复失败退避状态。

        只存内存时"重启即重新给一次机会"，而每次重试都是完整重拉——一次部署
        重启就能让几十 GiB 的坏包再拉一遍。退避必须跨重启存活才有意义。
        """
        for ticket_id, row in self._store.load_retries().items():
            next_at = row.get("next_retry_at")
            self._retries[ticket_id] = RetryState(
                failures=int(row.get("failures") or 0),
                next_retry_at=(
                    datetime.fromtimestamp(float(next_at), TZ) if next_at is not None else None
                ),
            )
        if self._retries:
            logger.info("已恢复失败退避状态 工单数=%d", len(self._retries))

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
            origin=job.origin,
        )

    async def create(self, ticket_id: str, request_id: str = "-") -> Job:
        """手动触发：代表用户当下的显式判断，直接入队并立即开始。"""
        async with self._lock:
            job = self._create_locked(ticket_id, ORIGIN_MANUAL)
        self._start(job, request_id)
        return job

    async def create_auto(self, archive: RemoteArchive) -> Job | None:
        """为尚未同步、或远端已换过一版的归档创建自动任务。

        检查本地产物和注册任务与创建动作共用同一把锁，扫描轮询与手动按钮不会
        在检查与入队之间产生竞态。返回 None 表示当前无需创建任务。
        """
        async with self._lock:
            existing = self.jobs.get(archive.ticket_id)
            if existing is not None and existing.phase in RUNNING_PHASES:
                return None

            final_dir = self.cfg.root / archive.ticket_id
            if final_dir.is_dir():
                # 终态优先于一切自动判据。`retries` 里 failures 到上限有两种来源：
                # 用户点了「放弃自动重试」（#1416），或重试次数真的用尽。两者共用
                # 同一个标记，因此都必须在这里让路——尤其不能因为"远端换包"就把它
                # 清掉：清掉之后 `_note_failure` 的终态护栏也跟着失效，结果是既抹掉
                # 了人工选择，又让坏包重新进入退避循环。要恢复必须显式 resume 或手动
                # `POST /jobs`（两条路都代表人的新判断）。
                state = self._retries.get(archive.ticket_id)
                if state is not None and state.exhausted:
                    return None
                if not _remote_archive_changed(final_dir, archive):
                    # 已发布且远端还是同一份：历史退避记录可以清掉。
                    self._clear_retry(archive.ticket_id)
                    return None
                # 远端换了包（客户重传修复版）：本地产物已经不是当前版本，
                # 必须重同步。旧实现的 docstring 声称支持这一点，代码里却只
                # 判断 final_dir.is_dir() 就 return None，重传永远到不了本地。
                # 远端换包同时是"上次失败原因可能已消失"的证据，退避一并清零
                # （终态已在上面让路，这里清掉的只会是普通退避）。
                self._clear_retry(archive.ticket_id)
                logger.info(
                    "ticket=%s 远端归档已更新，重新同步 远端mtime=%.3f 远端size=%d",
                    archive.ticket_id,
                    archive.mtime,
                    archive.size,
                )
            elif archive.ticket_id in self._tombstones:
                # 同步过、产物已按保留期或容量淘汰删掉的工单：扫描窗口现在覆盖 7 天，
                # 远端归档很可能还在（保留期也是 7 天），没有墓碑就会删了又拉。
                # 只挡自动扫描——手动 POST 代表用户新的判断，不该被墓碑拦下。
                return None

            state = self._retries.get(archive.ticket_id)
            if state is not None:
                if state.exhausted:
                    # 终态：自动扫描一律让路。这里必须显式挡，否则宽窗口每轮都会
                    # 重新发现它——"已放弃"就等于没有放弃。
                    return None
                if state.next_retry_at is not None and _now() < state.next_retry_at:
                    # 仍在退避窗口内：本轮巡检跳过，避免把同一个坏包反复重拉。
                    return None

            job = self._create_locked(archive.ticket_id, ORIGIN_AUTO)
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

    def _create_locked(self, ticket_id: str, origin: str) -> Job:
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
            origin=origin,
        )
        self.jobs[ticket_id] = job
        self._persist(job)
        return job

    def _start(self, job: Job, request_id: str) -> None:
        # request_id 一路带进任务日志，一次点击就能串起受理与执行两段。
        logger.info(
            "request_id=%s ticket=%s job=%s origin=%s 受理 排队中 当前任务数=%d",
            request_id,
            job.ticket_id,
            job.job_id,
            job.origin,
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

    def retry_ticket_ids(self) -> set[str]:
        """有失败记录、处于退避中的工单号。

        自动补扫只认这些——它们是"同步意图"的证据。窗口内的历史存量没有这条
        记录，就不该被当成漏同步重新拉一遍。

        已转终态的工单不在其中：它们"有同步意图"但"已被判定重试无意义"，
        再拉进补扫只会让 7 天宽窗口永久开着。
        """
        return {ticket_id for ticket_id, state in self._retries.items() if not state.exhausted}

    def _forget_ticket(self, ticket_id: str) -> None:
        """产物被删除后清理记账与历史，并留下墓碑。

        墓碑是"这个工单同步成功过"的唯一证据。不留的话，删除与自动扫描之间
        没有任何东西能区分"从没同步过"和"同步过但已清理"，扫描窗口一放宽
        就是删了又拉的无限循环。
        """
        row = self._store.get_artifact(ticket_id)
        synced_at = row.get("completed_at") if row else None
        self._store.add_tombstone(
            ticket_id,
            float(synced_at) if synced_at else _now().timestamp(),
            "retention",
        )
        self._tombstones.add(ticket_id)
        self._store.delete_artifact(ticket_id)
        self._store.clear_retry(ticket_id)
        self._retries.pop(ticket_id, None)
        self._history.pop(ticket_id, None)

    def _prune_expired_sync(self, now: datetime) -> int:
        active = self.active_ticket_ids()
        # 判据必须先于使用：过期判断读的 completedAt 只有新工单才写，
        # 历史目录缺这个字段就退化成目录 mtime，而 mtime 会被外部写入刷新
        # （DSH 往产物里写报告），结果是早已过期的产物永远显示"刚同步完"。
        # 所以在同一次维护里先对账固化完成时间，再判过期。
        reconcile_artifacts(self.cfg.root, self._store, active)
        return cleanup_expired(
            self.cfg.root,
            now,
            self.cfg.retention_days,
            active,
            self._forget_ticket,
        )

    async def prune_expired(self, now: datetime) -> int:
        """清理过期产物。

        两个必须遵守的约束：
        1. 磁盘遍历与 rmtree 都是阻塞 IO（冷缓存下全盘统计可达数分钟），
           必须放进线程池。直接在协程里跑会把事件循环钉死，实测 2026-10-31
           00:00:07 到 00:03:35 之间服务完全静默，连 /health 都没有响应。
        2. 不在注册表锁内执行。清理期间不应阻塞入队与前端查询；删除目标已经
           排除活跃工单，而发布是原子 rename，最坏情况只是多删一个旧目录。
        """
        return await asyncio.to_thread(self._prune_expired_sync, now)

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

    def prune_tombstones(self, now: datetime) -> int:
        """回收过期墓碑，返回删除条数。

        同步实现（单条 DELETE，微秒级），但由调用方放进线程池，
        与容量检查保持同一种执行模型，避免以后有人在这里加 IO 阻塞事件循环。
        """
        cutoff = now.timestamp() - TOMBSTONE_RETENTION_DAYS * 86400
        removed = self._store.prune_tombstones(cutoff)
        if removed:
            # 内存集合与库对齐：否则被回收的工单号仍会挡住自动扫描。
            self._tombstones = set(self._store.tombstone_map())
        return removed

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
                                # exhausted=True 表示已放弃自动重试，页面据此把
                                # "等待重试"改为"需人工处理"，否则用户会一直等它自愈。
                                "exhausted": retry.exhausted,
                            }
                            if retry is not None
                            else None
                        ),
                    }
                )
            return result

    async def _run(self, job: Job) -> None:
        ticket_id = job.ticket_id
        # 目录名只用工单号、不带 job_id：带 job_id 意味着每次重试都是全新目录，
        # rsync --partial 永远找不到上次的文件，65 GiB 级的包一旦失败就要从 0 重来。
        # 同工单串行由 RUNNING_PHASES 保证，这里不会有两个任务共用同一目录。
        work_dir = self.cfg.tmp_dir() / ticket_id
        archive_name = f"{ticket_id}.zip"

        # 手动任务不进并发闸门：用户点击代表"现在就要看到结果"，不该排在自动巡检
        # 成批入队的 5 个 65 GiB 任务后面。自动任务仍必须受限——它们的数量由扫描
        # 判据决定，判据一错就可能同时开满带宽，而手动是逐次点击、天然收敛。
        gate = _unlimited() if job.origin == ORIGIN_MANUAL else self._slots

        try:
            async with gate:
                job.started_at = _now()
                job.phase = Phase.FETCHING
                job.updated_at = _now()
                self._persist(job)

                # 上次失败留下的解压产物必须先清掉，只留归档供 rsync 续传，
                # 否则新旧产物混在一起，发布会把脏目录推给用户。
                prepare_workspace(work_dir, archive_name)

                await self._reject_non_log_payload(job)

                archive = await fetch(
                    ticket_id,
                    self.cfg,
                    work_dir,
                    job.job_id,
                    on_progress=self._progress_writer(job),
                )
                # 远端版本身份必须在解压/发布前取：解压可能删掉归档，发布还会把
                # 整个 work_dir rename 走，之后就拿不到这个文件的 stat 了。
                fetched_mtime, fetched_size = _file_identity(archive)

                if not self.cfg.archive_password:
                    raise TicketError(
                        ErrorCode.BAD_PASSWORD,
                        "未配置归档密码",
                        "TICKET_ARCHIVE_PASSWORD 为空",
                    )

                job.phase = Phase.EXTRACTING
                job.updated_at = _now()
                self._persist(job)

                self._release_archive_for_space(work_dir, archive)

                stats = await extract(
                    ticket_id,
                    archive,
                    work_dir,
                    self.cfg,
                    job.job_id,
                    on_progress=self._extract_writer(job),
                    on_archive=self._archive_writer(job),
                )

                # 解压已全部成功，归档的续传价值消失；必须在 publish 前删掉，
                # 否则它会跟着 work_dir 一起 rename 进产物目录。
                self._release_archive(work_dir, archive.name)

                replaced = publish(work_dir, self.cfg.root / ticket_id, self.cfg.trash_dir())
                prune_trash(self.cfg.trash_dir())

                # 产物已经原子落盘——从这一刻起「本次同步成功」是既成事实。
                # 后面的元数据、记账都只是辅助事实，任何一步失败都不能把它改判成
                # failed：用户看到 failed 会点重试，几十 GiB 的包就要再完整拉一遍。
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
                # 截断与"工具不支持"是两种事实：前者解出了一部分、可用；后者一点都打不开。
                # 混成一句话会让用户以为整份日志都不可信。
                if stats.truncated_archives:
                    suffix = f"{stats.truncated_archives} 个归档被截断，已保留可读部分"
                    job.message = f"{job.message}；{suffix}" if job.message else suffix
                # 深度上限从「整单失败」改为「停止下钻」后，截断信息不能静默：
                # 用户需要知道产物里少了更深层的附件，而不是以为日志齐了。
                if stats.depth_truncated:
                    suffix = f"{stats.depth_truncated} 个归档超出嵌套层数上限，未继续解压"
                    job.message = f"{job.message}；{suffix}" if job.message else suffix

                # 只有两项都取到才写版本判据：只剩一半的判据会让"远端更新"永远
                # 判不出来（甚至反过来每轮重拉），不如显式退化成"不判版本"。
                identity_fields = (
                    {"remoteMtime": fetched_mtime, "remoteSize": fetched_size}
                    if fetched_mtime is not None and fetched_size is not None
                    else {}
                )

                try:
                    # 把"何时同步完、多大、有没有日志"固化成显式事实：
                    # 清理与容量判断不再依赖目录 mtime，也不用每天全盘扫描。
                    has_logs = has_usable_logs(self.cfg.root / ticket_id)
                    # 「流程跑通」不等于「产物可用」：实测 Q2026102101911 解出 65 GiB
                    # 的 kasi/kasi.dump，状态却是 ready，用户点进去无日志可看。
                    if not has_logs:
                        suffix = "产物中未发现日志清单（log_list.json / sp_*），可能仅含转储文件"
                        job.message = f"{job.message}；{suffix}" if job.message else suffix
                    write_sync_meta(
                        self.cfg.root / ticket_id,
                        completedAt=_now().timestamp(),
                        bytes=stats.bytes,
                        files=stats.files,
                        hasLogs=has_logs,
                        # 记下"这份产物对应远端的哪个版本"。自动扫描据此判断
                        # 远端有没有换包（客户重传修复版），而不是靠完成时间
                        # 猜——完成时间会被收尾失败、时钟回拨污染，且不可重放。
                        **(identity_fields or {}),
                    )
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
                except Exception as error:  # noqa: BLE001 - 收尾失败不能否定已落盘的产物
                    logger.exception(
                        "ticket=%s job=%s 产物已就绪但收尾失败 %s", ticket_id, job.job_id, error
                    )
                    suffix = "产物已就绪，但同步元数据写入失败，请检查磁盘与目录权限"
                    job.message = f"{job.message}；{suffix}" if job.message else suffix

                job.updated_at = _now()
                self._persist(job)
                self._clear_retry(ticket_id)
                # 重新同步成功，墓碑必须抹掉：否则它下次被清理后会被当成"仍已同步过"
                # 而不再进入自动扫描，等于把用户显式要求的重拉结果又丢掉。
                self._store.clear_tombstone(ticket_id)
                self._tombstones.discard(ticket_id)
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
            self._note_failure(ticket_id, error.code)
            self._append_terminal_hint(job)
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
            self._append_terminal_hint(job)
        finally:
            # 成功时 work_dir 已被 rename 走，这里是空操作。
            # 失败与取消时只清解压产物、保留已下载的归档，下次重试才有得续传；
            # 陈旧残留交给每日维护按年龄回收，不再"每次失败都删干净"。
            prepare_workspace(work_dir, archive_name)

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

    def _note_failure(self, ticket_id: str, code: ErrorCode | None = None) -> None:
        """登记一次失败并按阶梯推迟下一次自动重试。

        手动 POST /jobs 不受退避限制：用户显式点击代表新的判断，
        自动巡检则必须让路，否则坏包会把带宽和磁盘反复烧掉。

        两条终态路径：确定性失败（重试结果不会变）与重试次数用尽。终态只是
        "不再自动试"，不是"禁止同步"——用户仍可手动触发。

        已处于终态的工单不再被失败改写：用户手动"放弃自动重试"后，正在跑或
        排队中的那次任务若失败，不能把它拉回退避循环。
        """
        existing = self._retries.get(ticket_id)
        if existing is not None and existing.exhausted:
            logger.info(
                "ticket=%s 已处于终态（failures=%d），本次失败不改写退避状态",
                ticket_id,
                existing.failures,
            )
            return

        state = self._retries.setdefault(ticket_id, RetryState())
        # delay 必须先于 failures 自增取，否则第一次失败就会跳到第二档。
        delay = state.delay_for_next()
        state.failures += 1
        if code in TERMINAL_ERROR_CODES:
            # 重试次数直接推到上限，与"次数用尽"共用同一个终态判据。
            state.failures = MAX_RETRY_ATTEMPTS
            state.next_retry_at = None
            reason = f"确定性失败 code={code.value}，重试不会改变结果"
        elif state.failures >= MAX_RETRY_ATTEMPTS:
            state.next_retry_at = None
            reason = f"已达自动重试上限 {MAX_RETRY_ATTEMPTS} 次"
        else:
            state.next_retry_at = _now() + timedelta(seconds=delay)
            reason = f"间隔={delay}s 下次自动重试={state.next_retry_at.isoformat()}"
        # 落库：只存内存的话，重启就等于"重新给坏包一次全量拉取的机会"。
        self._store.save_retry(
            ticket_id,
            state.failures,
            state.next_retry_at.timestamp() if state.next_retry_at else None,
        )
        logger.warning(
            "ticket=%s 失败登记 第%d次 %s%s",
            ticket_id,
            state.failures,
            reason,
            "（自动重试已停止，可手动重试）" if state.exhausted else "",
        )

    def _clear_retry(self, ticket_id: str) -> None:
        """成功或无需再试时，同时清掉内存与库里的退避状态。"""
        self._retries.pop(ticket_id, None)
        self._store.clear_retry(ticket_id)

    def abandon_retry(self, ticket_id: str) -> RetryState:
        """把工单置为终态：放弃自动重试，保留产物与历史。

        必须**同时**改内存与 SQLite。只改库是无效的——内存才是运行态权威：
        2026-11-02 实测直接 `sqlite3 update retries set failures=6`，
        被运行中进程的下一次写入覆盖回 4，只能停服改库再启动。
        手动 POST /jobs 不受影响，成功同步后终态会自动清除。
        """
        state = self._retries.setdefault(ticket_id, RetryState())
        state.failures = max(state.failures, MAX_RETRY_ATTEMPTS)
        state.next_retry_at = None
        self._store.save_retry(ticket_id, state.failures, None)
        logger.warning("ticket=%s 手动放弃自动重试 failures=%d", ticket_id, state.failures)
        return state

    def resume_retry(self, ticket_id: str) -> None:
        """恢复自动重试：清掉终态与退避记录，下一轮巡检重新考虑它。

        只清退避，不碰墓碑：墓碑管的是"同步过又按保留期删掉"的工单，
        语义不同，混在一起会让已清理的工单被重新拉回来。
        """
        self._clear_retry(ticket_id)
        logger.info("ticket=%s 手动恢复自动重试", ticket_id)

    def _append_terminal_hint(self, job: Job) -> None:
        """把"已停止自动重试"写进 job.message。

        页面只渲染 message，退避详情在 /artifacts 的 retry 字段里；用户看到
        failed 但不知道后台已经放弃，就会一直等它自愈。手动重试永远可用，
        所以文案要给出这条路。
        """
        state = self._retries.get(job.ticket_id)
        if state is None or not state.exhausted:
            return
        if "已停止自动重试" in (job.message or ""):
            return
        job.message = f"{job.message or '同步失败'}（已停止自动重试，可手动重试）"
        self._persist(job)

    def _release_archive(self, work_dir: Path, archive_name: str) -> None:
        """释放 work_dir 里的顶层归档。

        时机是"解压全部成功之后、发布之前"：
        - 早于解压完成就会毁掉续传能力（旧实现在开始解压时就 unlink，导致
          extract 阶段失败后 work_dir 全空，下轮重试只能全量重下）；
        - 晚于发布会跟着 rename 进产物目录，污染用户可见的目录结构。
        """
        target = work_dir / archive_name
        try:
            size = target.stat().st_size
            target.unlink()
        except FileNotFoundError:
            return
        except OSError as exc:
            # 删不掉只影响磁盘占用，不影响正确性，不能因此让同步失败。
            logger.warning("释放归档失败 path=%s error=%s", target, exc)
            return
        logger.info("已释放归档 path=%s bytes=%d", target, size)

    def _release_archive_for_space(self, work_dir: Path, archive: Path) -> None:
        """余量不足以同时容纳归档与解压产物时，提前释放归档。

        默认优先保续传；只有磁盘放不下两份时才牺牲它——解压中途把磁盘写满，
        代价比重下一次更大（还会触发 min_free_bytes 熔断）。
        """
        try:
            size = archive.stat().st_size
            free = shutil.disk_usage(work_dir).free
        except OSError:
            return
        if free >= size:
            return
        logger.warning(
            "磁盘余量不足 free=%d archive=%d，提前释放归档（本次失败将无法续传）",
            free,
            size,
        )
        self._release_archive(work_dir, archive.name)

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


async def _auto_discover(cfg: Config, registry: JobRegistry, now: datetime) -> int:
    """自动发现候选：当天新归档 + 有失败记录的旧归档，并做单轮限流。

    为什么不能按「最近 N 天」一刀切：2026-11-01 把窗口放到 7 天后，第一轮就把
    95 个归档里 57 个**从未同步过**的历史工单全部入队，一次吃掉 766 GiB。
    历史存量不是"漏同步"——客户从来没要求同步它们。

    真正要解决的是"昨天失败的任务今天还能重试"，所以第二路只认 `retries`
    里有失败记录的工单：那才是"同步意图"的证据。
    """
    candidates: dict[str, RemoteArchive] = {
        archive.ticket_id: archive for archive in await list_recent_archives(cfg, 1, now)
    }

    retry_ids = registry.retry_ticket_ids()
    if retry_ids and cfg.auto_scan_days > 1:
        # 宽窗口补扫按分钟级节流：巡检本身 30 秒一轮，若每轮都走 7 天窗口，
        # 就变成每 31 秒 ssh 遍历一遍近百个归档（远端 /sf/log 有 1060 个目录），
        # 而限流只挡住入队、挡不住这趟遍历。补扫的语义是"给失败任务第二次机会"，
        # 5 分钟粒度完全够用。
        since_last = time.monotonic() - registry.last_deep_scan_at
        if since_last < DEEP_SCAN_MIN_INTERVAL_SECONDS:
            logger.info(
                "宽窗口补扫未到间隔，本轮跳过 待重试=%d 距上次=%.0fs",
                len(retry_ids),
                since_last,
            )
        else:
            registry.last_deep_scan_at = time.monotonic()
            looked_back = await list_recent_archives(cfg, cfg.auto_scan_days, now)
            for archive in looked_back:
                if archive.ticket_id in retry_ids:
                    candidates.setdefault(archive.ticket_id, archive)
            logger.info(
                "失败任务补扫 窗口=%d天 待重试=%d 候选合计=%d",
                cfg.auto_scan_days,
                len(retry_ids),
                len(candidates),
            )

    created = 0
    for archive in candidates.values():
        if created >= MAX_AUTO_JOBS_PER_SCAN:
            # 判据再出错，最多也只放大成这么多任务。
            logger.warning(
                "单轮自动入队已达上限 %d，本轮剩余 %d 个顺延到下一轮",
                MAX_AUTO_JOBS_PER_SCAN,
                len(candidates) - created,
            )
            break
        try:
            if await registry.create_auto(archive) is not None:
                created += 1
        except JobConflict:
            # 手动按钮刚好先入队时，自动扫描只需跳过本轮。
            continue
    return created


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
                # .tmp 残留按年龄回收：失败残留是续传的本钱，不能一失败就删，
                # 但也不能无限累积。与容量检查同批执行，避免多开一趟遍历。
                stale = await asyncio.to_thread(
                    cleanup_stale_workspaces, cfg.tmp_dir(), STALE_WORKSPACE_SECONDS
                )
                if stale:
                    logger.info("清理陈旧临时目录 count=%d", stale)
                tombstones = await asyncio.to_thread(registry.prune_tombstones, now)
                if tombstones:
                    logger.info("回收过期墓碑 count=%d", tombstones)
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
                await _auto_discover(cfg, registry, now)
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


@app.post("/jobs/{ticket_id}/abandon", status_code=202)
async def abandon_ticket(ticket_id: str, request: Request):
    """放弃该工单的自动重试（置终态）。

    只影响自动巡检：产物与历史都保留，手动 `POST /jobs` 仍可随时重新同步，
    成功后终态记录会被清掉。用于"确定不会成功、不想再等 6 次退避"的场景
    （如源端归档本身损坏）。
    """
    registry: JobRegistry = request.app.state.registry
    ticket_id = validate_ticket_id(ticket_id)
    state = registry.abandon_retry(ticket_id)
    return {
        "ticketId": ticket_id,
        "retry": {
            "failures": state.failures,
            "nextRetryAt": state.next_retry_at.isoformat() if state.next_retry_at else None,
            "exhausted": state.exhausted,
        },
    }


@app.delete("/jobs/{ticket_id}/abandon", status_code=204)
async def resume_ticket(ticket_id: str, request: Request):
    """恢复该工单的自动重试：清掉终态与退避记录。"""
    registry: JobRegistry = request.app.state.registry
    registry.resume_retry(validate_ticket_id(ticket_id))
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

    # 手动任务绕过并发闸门，因此"同时跑了几个手动任务"必须单独可见：
    # 总量指标无法区分"2 个自动任务"和"2 个自动 + 3 个人工点击"。
    running_by_origin = {ORIGIN_AUTO: 0, ORIGIN_MANUAL: 0}
    for job in registry.jobs.values():
        if job.phase in RUNNING_PHASES:
            running_by_origin[job.origin] = running_by_origin.get(job.origin, 0) + 1

    lines = [
        "# HELP ticket_sync_disk_free_bytes 产物根目录所在磁盘剩余字节",
        "# TYPE ticket_sync_disk_free_bytes gauge",
        f"ticket_sync_disk_free_bytes {free}",
        "# HELP ticket_sync_active_jobs 正在运行或排队中的任务数",
        "# TYPE ticket_sync_active_jobs gauge",
        f"ticket_sync_active_jobs {registry.active_count()}",
        "# HELP ticket_sync_active_jobs_origin 正在运行或排队中的任务数（按来源）",
        "# TYPE ticket_sync_active_jobs_origin gauge",
        f'ticket_sync_active_jobs_origin{{origin="{ORIGIN_AUTO}"}} {running_by_origin[ORIGIN_AUTO]}',
        f'ticket_sync_active_jobs_origin{{origin="{ORIGIN_MANUAL}"}} {running_by_origin[ORIGIN_MANUAL]}',
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


def _file_identity(archive: Path) -> tuple[float | None, int | None]:
    """取刚拉下来的归档 (mtime, size)，作为"远端版本身份"。

    `fetch` 用的是 `rsync -rlt`，`-t` 保留源端修改时间，因此本地文件的 mtime/size
    与远端一致（`find %T@` 拿到的也是源端 mtime）。取不到时不臆造值：调用方
    会退化成"不写版本判据"，而不是把当前时间写成远端版本、导致后续每轮重判。
    """
    try:
        info = archive.stat()
    except OSError as exc:
        logger.warning("读取归档身份失败 path=%s error=%s", archive, exc)
        return None, None
    return info.st_mtime, info.st_size


def _remote_archive_changed(ticket_dir: Path, archive: RemoteArchive) -> bool:
    """本地这份产物对应的远端版本，是否已不是扫描到的这一份。

    判据是「与发布时记下的远端身份比对」，而不是「与完成时间比大小」：
    完成时间会被收尾失败、时钟回拨污染，同一个输入可能得出不同结论；而
    `remoteMtime`/`remoteSize` 是发布当时随产物一起写死的显式事实，判据幂等——
    重复判断永远给出同一答案，不会因为一次元数据写失败就无限重拉。

    历史目录（本次改造之前发布的）没有这两项，一律按「没变」处理：
    升级首轮绝不能因此把存量产物重拉一遍（2026-11-01 的 766 GiB 教训）。
    """
    meta = read_sync_meta(ticket_dir)
    recorded_mtime = meta.get("remoteMtime")
    recorded_size = meta.get("remoteSize")

    # bool 是 int 的子类，`true` 会被当成 1 秒/1 字节，必须显式排除。
    size_known = isinstance(recorded_size, int) and not isinstance(recorded_size, bool)
    mtime_known = (
        isinstance(recorded_mtime, (int, float)) and not isinstance(recorded_mtime, bool)
    )
    if not size_known and not mtime_known:
        return False

    # 体积先比：客户重传通常会改包内容，而 rsync -a 可能把源文件的旧 mtime
    # 原样带过来，只比 mtime 会漏判。
    if size_known and recorded_size != archive.size:
        return True
    if mtime_known:
        # 只认"远端变新"这一个方向。若哪天 rsync 不再保留 mtime（记录值被刷成
        # 拉取时刻），这个方向的比较会稳定地判"没变"，不会退化成每轮重拉 65 GiB。
        return archive.mtime > float(recorded_mtime) + REMOTE_UPDATE_TOLERANCE_SECONDS
    return False


def _prepare_dirs(cfg: Config) -> None:
    """启动自检：目录可写、回收陈旧半成品。任何一个不满足都直接启动失败。

    这里**不再无差别清空** `.tmp`：work_dir 按工单号固定后，保住已下载的归档
    才能让 `rsync --partial` 跨重启续传。只回收超龄残留，避免边下边清。
    """
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

    removed = cleanup_stale_workspaces(cfg.tmp_dir(), STALE_WORKSPACE_SECONDS)
    if removed:
        logger.info("启动自检：已回收陈旧临时目录 count=%d", removed)


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
