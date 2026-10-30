"""日志收集制品代传 5888（P2 段③）。

语义来自 5888 服务端源码实测（`/acli/app.py`、`process_upload.py`）：

1. `init_upload` **每次** 生成新的 `upload_id`，不会复用旧会话 → 平台必须持久化；
2. `upload_chunk` 只校验会话存在且未过期（24h），**支持续传**；
3. 同 `chunk_index` 重传幂等（分片文件覆盖写，`uploaded_size` 不重复累加）；
4. 收齐后返回 `{"status":"complete"}`，但归档由 `process_upload.py` 异步完成；
5. 归档成功后服务端删除会话 JSON → `upload_status` 返回 **404 才算真正落地**；
6. `complete` 之后不得再传任何分片：异常分支只清理分片不删会话，再传会重复派发。

职责边界：平台只做"搬运与确认"，不解析日志内容，也不自动清理任何文件。
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import math
import re
import time
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import and_, func, or_, select, update

from ..config import settings
from ..models.log_collect import LogCollectTask
from .log_collect_storage import LocalLogCollectStorage

logger = logging.getLogger(__name__)

RELAY_STATUS_VALUES = ("none", "pending", "running", "success", "failed")

# 5888 归档文件名契约：只接受「工单号 + .zip」。这是服务端硬约束（实测 400
# Invalid filename），不是平台的命名偏好；缺了它代传在 init_upload 就失败。
# 注意与归档目录一致：`/sf/log/<工单号>/<工单号>.zip`。
SANGFOR_ARCHIVE_NAME_RE = re.compile(r"Q\d{13}")


class SangforRelayError(Exception):
    """代传类异常（错误码与传输层一致）。"""

    def __init__(self, message: str, code: str = "LC-TRANSFER-003"):
        super().__init__(message)
        self.code = code


class SangforRelayClient:
    """5888 日志服务器协议客户端。

    只实现插件实际使用的三个端点；不引入 5888 的任何鉴权假设
    （生产实测未启用 `ACLI_UPLOAD_TOKEN`，插件自身也不带认证头）。
    """

    def __init__(self, base_url: str, *, chunk_timeout: float = 300.0):
        self.base_url = base_url.rstrip("/")
        self.chunk_timeout = chunk_timeout

    async def init_upload(self, client: httpx.AsyncClient, filename: str, file_size: int) -> tuple[str, int]:
        response = await client.post(
            f"{self.base_url}/init_upload",
            files={"filename": (None, filename), "file_size": (None, str(file_size))},
        )
        response.raise_for_status()
        data = response.json()
        upload_id = str(data.get("upload_id") or "")
        chunk_size = int(data.get("chunk_size") or 0)
        if not upload_id or chunk_size <= 0:
            raise SangforRelayError("日志服务器返回无效的 upload_id / chunk_size")
        return upload_id, chunk_size

    async def upload_chunk(
        self,
        client: httpx.AsyncClient,
        *,
        upload_id: str,
        chunk_index: int,
        chunk_total: int,
        filename: str,
        payload: bytes,
    ) -> str:
        """返回服务端 status；`complete` 表示收齐（不代表归档完成）。"""

        response = await client.post(
            f"{self.base_url}/upload_chunk",
            files={
                "upload_id": (None, upload_id),
                "chunk_index": (None, str(chunk_index)),
                "chunk_total": (None, str(chunk_total)),
                "chunk": (filename, payload, ""),
            },
            timeout=self.chunk_timeout,
        )
        response.raise_for_status()
        return str(response.json().get("status") or "")

    async def upload_status(self, client: httpx.AsyncClient, upload_id: str) -> dict[str, Any] | None:
        """返回 None 表示会话已不存在：已归档完成或超过 24h 过期。"""

        response = await client.get(f"{self.base_url}/upload_status/{upload_id}")
        if response.status_code == 404:
            return None
        response.raise_for_status()
        return response.json()


class LogCollectRelayService:
    def __init__(
        self,
        session_factory,
        storage: LocalLogCollectStorage,
        client: SangforRelayClient,
        *,
        poll_interval: float = 5.0,
        archive_timeout: float = 1800.0,
        max_attempts: int = 3,
        retry_base_delay: float = 2.0,
        retry_max_delay: float = 30.0,
        stale_after: float | None = None,
        heartbeat_interval: float | None = None,
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.client = client
        self.poll_interval = poll_interval
        self.archive_timeout = archive_timeout
        # 瞬时错误重试：采集已在节点侧成功（包已生成），中继/5888 仅因网络抖动或
        # 服务端暂不可达失败时，应退避重试而非直接写 failed，让网络恢复后自动补传。
        self.max_attempts = max(1, int(max_attempts))
        self.retry_base_delay = float(retry_base_delay)
        self.retry_max_delay = float(retry_max_delay)
        # 租约：``relay_status=running`` 只是「有人正在跑」的声明，靠 relay_updated_at
        # 续租才成立。发布/重启/OOM 杀掉的进程不可能自己写终态，租约过期即视为执行者
        # 已死，任何实例都可抢占重跑——这是「代传卡死 running」的唯一通用兜底。
        self.stale_after = float(stale_after or settings.LOG_COLLECT_RELAY_STALE_SECONDS)
        derived = max(5.0, self.stale_after / 4)
        # 心跳必须明显密于租约，且不允许被外部调大到超过租约一半，否则会自我过期。
        self.heartbeat_interval = float(min(heartbeat_interval or derived, self.stale_after / 2))

    # ── 抢占 ────────────────────────────────────────────────────────────────

    async def trigger(self, task_id: UUID, *, force: bool = False) -> bool:
        """抢占式启动：只有把 relay_status 从非 running 改成 running 的调用者才执行。

        ``force`` 用于人工重试。**不带 force 也允许抢占租约已过期的 running**：
        发布/重启杀掉的进程不可能自己写终态，如果只认 force，任务会永远停在
        running，前端只能等到 2 小时轮询超时才敢说「结果未知」，而用户点
        「上传到平台」又会重传整包、且代传依旧不会启动。
        """

        async with self.session_factory() as session:
            result = await session.execute(
                update(LogCollectTask)
                .where(LogCollectTask.task_id == task_id, self._claimable(force))
                .values(relay_status="running", relay_updated_at=func.now())
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    def _stale_cutoff(self) -> datetime:
        return datetime.now(UTC) - timedelta(seconds=self.stale_after)

    def _expired(self, updated_at: datetime | None) -> bool:
        """租约是否已过期。``NULL`` 视为过期：老记录没有心跳，不能因此永久占位。"""

        if updated_at is None:
            return True
        moment = updated_at if updated_at.tzinfo else updated_at.replace(tzinfo=UTC)
        return moment < self._stale_cutoff()

    def lease_expired(self, status: str | None, updated_at: datetime | None) -> bool:
        """对外暴露的租约判定（快照与测试共用同一口径）。"""

        return status == "running" and self._expired(updated_at)

    def _claimable(self, force: bool):
        """可被本次调用抢占的条件：初次执行 / 上次失败 / 租约过期（或 force 的 running）。"""

        claimable = [LogCollectTask.relay_status.in_(["none", "pending", "failed"])]
        if force:
            claimable.append(LogCollectTask.relay_status == "running")
        else:
            claimable.append(
                and_(
                    LogCollectTask.relay_status == "running",
                    or_(
                        LogCollectTask.relay_updated_at.is_(None),
                        LogCollectTask.relay_updated_at < self._stale_cutoff(),
                    ),
                )
            )
        return or_(*claimable)

    async def heartbeat(self, task_id: UUID) -> bool:
        """续租：只在仍为 running 时刷新 relay_updated_at，不碰任何终态。"""

        async with self.session_factory() as session:
            result = await session.execute(
                update(LogCollectTask)
                .where(
                    LogCollectTask.task_id == task_id,
                    LogCollectTask.relay_status == "running",
                )
                .values(relay_updated_at=func.now())
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def _heartbeat_loop(self, task_id: UUID) -> None:
        """独立协程续租：单分片 300s 超时、合并等长阻塞期间也不能让租约过期。"""

        while True:
            await asyncio.sleep(self.heartbeat_interval)
            try:
                if not await self.heartbeat(task_id):
                    # 状态已被终态或别的实例接管：停止续租，不能替别人维持租约。
                    return
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001 - 心跳失败不能把业务打成失败
                logger.warning("日志包代传心跳失败 task=%s", task_id, exc_info=True)

    async def claim_stale(self, *, limit: int = 20) -> list[UUID]:
        """接管「待跑」与「租约失效」的代传任务，返回本次真正归本方执行的 task_id。

        只看有制品的任务：没有 archive_path 的中间态抢过来只会以 LC-TRANSFER-001
        立即落 failed，那不是自愈而是自毁。
        """

        async with self.session_factory() as session:
            result = await session.execute(
                select(LogCollectTask.task_id)
                .where(
                    LogCollectTask.archive_path.isnot(None),
                    or_(
                        LogCollectTask.relay_status == "pending",
                        and_(
                            LogCollectTask.relay_status == "running",
                            or_(
                                LogCollectTask.relay_updated_at.is_(None),
                                LogCollectTask.relay_updated_at < self._stale_cutoff(),
                            ),
                        ),
                    ),
                )
                .order_by(LogCollectTask.relay_updated_at.asc().nullsfirst())
                .limit(limit)
            )
            candidates = [row[0] for row in result.all()]

        claimed: list[UUID] = []
        for task_id in candidates:
            if await self.trigger(task_id):
                claimed.append(task_id)
        return claimed

    async def release(self, task_ids: list[UUID]) -> int:
        """优雅退出时交还租约：把仍在 running 的置回 pending，交给下一个实例。

        写 pending 而不是 failed：任务并没有得出「归档失败」这个结论（5888 侧会话
        还有 24h 有效期），判死会让用户看到假失败并触发整包重传。
        """

        if not task_ids:
            return 0
        async with self.session_factory() as session:
            result = await session.execute(
                update(LogCollectTask)
                .where(
                    LogCollectTask.task_id.in_(task_ids),
                    LogCollectTask.relay_status == "running",
                )
                .values(relay_status="pending", relay_updated_at=func.now())
            )
            await session.commit()
            return result.rowcount or 0

    async def snapshot(self, task_id: UUID) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            result = await session.execute(select(LogCollectTask).where(LogCollectTask.task_id == task_id))
            task = result.scalar_one_or_none()
            if task is None:
                return None
            return {
                "task_id": str(task.task_id),
                "relay_status": task.relay_status,
                "relay_upload_id": task.relay_upload_id,
                "relay_chunk_size": task.relay_chunk_size,
                "relay_chunk_index": task.relay_chunk_index,
                "relay_chunk_total": task.relay_chunk_total,
                "relay_restarts": task.relay_restarts,
                # 让"卡住"与"在传"可区分：单片超时期间 index 不动，只有 attempt /
                # last_error / updated_at 能说明进程还活着、上一轮为什么失败。
                "relay_attempt": task.relay_attempt or 0,
                "relay_last_error": task.relay_last_error,
                "relay_last_error_at": task.relay_last_error_at,
                "relay_updated_at": task.relay_updated_at,
                # 前端据此判断「归档卡住」并自动强制续跑，而不是干等 2 小时。
                "stale": self.lease_expired(task.relay_status, task.relay_updated_at),
                "archive_path": task.archive_path,
            }

    # ── 主流程 ──────────────────────────────────────────────────────────────

    async def run(self, task_id: UUID) -> None:
        """代传入口：租约续期与重试编排在这一层，``_run_with_retry`` 只管业务。"""

        heartbeat = asyncio.create_task(self._heartbeat_loop(task_id), name=f"relay-heartbeat-{task_id}")
        try:
            await self._run_with_retry(task_id)
        finally:
            heartbeat.cancel()
            with suppress(asyncio.CancelledError):
                await heartbeat

    async def _run_with_retry(self, task_id: UUID) -> None:
        """代传主入口：瞬时错误自动退避重试，永久错误立即落终态。

        第一性原理：采集已在节点侧成功（包已生成），中继/5888 仅因瞬时网络抖动或
        服务端暂不可达而失败。若不重试直接写 failed，用户会看到"已采到却交付失败"且
        无法自愈——网络恢复后这包本应自动补传。但"无制品 / 空文件"属永久错误，
        重试必然同样失败，直接落 failed，避免无谓等待与重复全量重传。
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            # 每轮开始先落 attempt。单片上传超时是 300s，这期间 relay_chunk_index 不会变，
            # 界面只能看到"running 且进度不动"——把 attempt 落库后，用户至少能看出
            # "已经第几轮了、进程还活着"，而不是干等。
            await self._save(task_id, attempt=attempt)
            try:
                await self._run(task_id)
                return
            except Exception as exc:  # noqa: BLE001 - 异常要么落终态要么退避重试
                last_exc = exc
                # 原因先落库再决定要不要重试：瞬时失败随后成功后，用户仍需要知道
                # "刚才那一轮为什么慢"；只写终态的话，成功任务的这段历史会彻底消失。
                await self._save(task_id, last_error=str(exc)[:2000])
                # 永久错误：不重试，立即落终态（错误码保留异常自身分类）。
                if not self._is_transient(exc):
                    code = getattr(exc, "code", None) or "LC-TRANSFER-003"
                    await self._finish(task_id, "failed", error=str(exc), error_code=code)
                    raise
                # 瞬时错误且仍有重试次数：退避后重试，relay_status 保持 running，
                # 交付状态不落 failed；_run 会复用持久化的 upload_id 续传已完成分片。
                if attempt >= self.max_attempts:
                    code = getattr(exc, "code", None) or "LC-TRANSFER-003"
                    await self._finish(task_id, "failed", error=str(exc), error_code=code)
                    raise
                await asyncio.sleep(min(self.retry_base_delay * (2 ** (attempt - 1)), self.retry_max_delay))
        if last_exc is not None:  # 兜底（理论上不可达）
            code = getattr(last_exc, "code", None) or "LC-TRANSFER-003"
            await self._finish(task_id, "failed", error=str(last_exc), error_code=code)
            raise last_exc

    @staticmethod
    def _is_transient(exc: Exception) -> bool:
        """判断代传异常是否值得重试。

        永久错误（无制品 LC-TRANSFER-001 / 空文件 LC-TRANSFER-002）重试必然失败，
        直接落终态；其余（网络超时 / 连接失败 / 5888 5xx / 归档确认超时）视为瞬时，
        退避重试，网络恢复后自动续传已上传分片。
        """
        if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.TransportError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            # 5888 返回 5xx 视为服务端临时不可用；4xx（含 400 Invalid filename）视为永久。
            return 500 <= int(getattr(exc.response, "status_code", 0) or 0) < 600
        if isinstance(exc, SangforRelayError):
            return exc.code not in ("LC-TRANSFER-001", "LC-TRANSFER-002")
        return False

    async def _run(self, task_id: UUID) -> None:
        task = await self._load(task_id)
        if task is None:
            raise SangforRelayError("任务不存在", "LC-TRANSFER-001")

        archive_key = (task.archive_path or "").strip()
        if not archive_key:
            raise SangforRelayError("平台尚无制品，无法代传", "LC-TRANSFER-001")
        path = self.storage.object_path(task.case_id, str(task_id))
        if not path.is_file():
            raise SangforRelayError(f"平台副本不存在：{archive_key}", "LC-TRANSFER-001")
        size = path.stat().st_size
        if size <= 0:
            raise SangforRelayError("平台副本为空文件", "LC-TRANSFER-002")

        # 代传前核对"要发的这份字节"与落库的制品一致。
        # 第一性原理：5888 不回摘要，平台能做的最后一次自证就在出站前——
        # 若副本在合并后被改写/截断，只有这里能拦住，否则坏包会一路进归档。
        recorded_size = getattr(task, "object_size", None)
        if recorded_size and int(recorded_size) != size:
            raise SangforRelayError(
                f"平台副本大小与落库不一致：落库 {recorded_size}，实际 {size}",
                "LC-TRANSFER-002",
            )
        expected_digest = (getattr(task, "object_sha256", None) or "").strip().lower()
        if expected_digest:
            actual_digest = await asyncio.to_thread(self._file_sha256, path)
            if actual_digest != expected_digest:
                raise SangforRelayError(
                    f"平台副本摘要与落库不一致：落库 {expected_digest}，实际 {actual_digest}",
                    "LC-TRANSFER-002",
                )

        # 归档名必须由**工单号**构造，不能用平台内部的 task_id（UUID）：
        # 5888 会直接 400 Invalid filename，而那个错误对排障毫无指向性。
        archive_name = self.archive_filename(task.case_id)

        async with httpx.AsyncClient(timeout=httpx.Timeout(120.0, connect=10.0)) as client:
            upload_id = (task.relay_upload_id or "").strip()
            chunk_size = int(task.relay_chunk_size or 0)
            chunk_total = int(task.relay_chunk_total or 0)
            start_index = 0
            restarts = int(task.relay_restarts or 0)

            # 尝试复用旧会话：只有服务端仍持有时才能续传。
            if upload_id and chunk_size > 0 and chunk_total > 0:
                status = await self.client.upload_status(client, upload_id)
                if status is None:
                    upload_id = ""
                else:
                    start_index = int(task.relay_chunk_index or -1) + 1

            if not upload_id:
                upload_id, chunk_size = await self.client.init_upload(client, archive_name, size)
                chunk_total = max(1, math.ceil(size / chunk_size))
                start_index = 0
                restarts += 1
                await self._save(
                    task_id,
                    upload_id=upload_id,
                    chunk_size=chunk_size,
                    chunk_total=chunk_total,
                    chunk_index=-1,
                    restarts=restarts,
                )

            for index in range(start_index, chunk_total):
                payload = await asyncio.to_thread(self._read_chunk, path, index, chunk_size, size)
                if not payload:
                    raise SangforRelayError(f"分片 {index} 读取为空")
                status = await self.client.upload_chunk(
                    client,
                    upload_id=upload_id,
                    chunk_index=index,
                    chunk_total=chunk_total,
                    filename=archive_name,
                    payload=payload,
                )
                # 每块成功后立即持久化：进程崩溃时从这里继续，不重传已完成分片。
                await self._save(task_id, upload_id=upload_id, chunk_index=index)
                if status == "complete":
                    break

            await self._await_archive(client, upload_id)
            await self._finish(task_id, "success")

    @staticmethod
    def archive_filename(case_id: str | None) -> str:
        """由工单号构造 5888 归档文件名，并在本地先拦一次契约（避免 400 被吞成笼统失败）。"""

        value = (case_id or "").strip()
        if not SANGFOR_ARCHIVE_NAME_RE.fullmatch(value):
            raise SangforRelayError(f"工单号不合法，无法确定归档文件名：{value or '<空>'}", "LC-TRANSFER-001")
        return f"{value}.zip"

    @staticmethod
    def _file_sha256(path) -> str:
        """流式算整包 SHA-256；20 GiB 用 4 MiB 块读，内存占用与包大小无关。"""

        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()

    @staticmethod
    def _read_chunk(path, index: int, chunk_size: int, total: int) -> bytes:
        offset = index * chunk_size
        length = min(chunk_size, total - offset)
        if length <= 0:
            return b""
        with path.open("rb") as handle:
            handle.seek(offset)
            return handle.read(length)

    async def _await_archive(self, client: httpx.AsyncClient, upload_id: str) -> None:
        """确认归档落地：会话 JSON 被删除（404）才代表 process_upload.py 成功。"""

        deadline = time.monotonic() + self.archive_timeout
        while time.monotonic() < deadline:
            if await self.client.upload_status(client, upload_id) is None:
                return
            await asyncio.sleep(self.poll_interval)
        raise SangforRelayError("归档确认超时：服务端会话仍存在，归档可能失败")

    # ── 持久化 ──────────────────────────────────────────────────────────────

    async def _load(self, task_id: UUID) -> LogCollectTask | None:
        async with self.session_factory() as session:
            result = await session.execute(select(LogCollectTask).where(LogCollectTask.task_id == task_id))
            return result.scalar_one_or_none()

    async def _save(self, task_id: UUID, **values) -> None:
        payload: dict[str, Any] = {"relay_status": "running", "relay_updated_at": func.now()}
        if "upload_id" in values:
            payload["relay_upload_id"] = values["upload_id"]
        if "chunk_size" in values:
            payload["relay_chunk_size"] = values["chunk_size"]
        if "chunk_total" in values:
            payload["relay_chunk_total"] = values["chunk_total"]
        if "chunk_index" in values:
            payload["relay_chunk_index"] = values["chunk_index"]
        if "restarts" in values:
            payload["relay_restarts"] = values["restarts"]
        if "attempt" in values:
            payload["relay_attempt"] = values["attempt"]
        if "last_error" in values:
            # 传 null 表示"清除"（成功时清），传文本表示记录本次失败。
            payload["relay_last_error"] = values["last_error"]
            payload["relay_last_error_at"] = func.now() if values["last_error"] else None

        async with self.session_factory() as session:
            await session.execute(update(LogCollectTask).where(LogCollectTask.task_id == task_id).values(**payload))
            await session.commit()

    async def _finish(
        self,
        task_id: UUID,
        status: str,
        *,
        error: str | None = None,
        error_code: str = "LC-TRANSFER-003",
    ) -> None:
        payload: dict[str, Any] = {"relay_status": status, "relay_updated_at": func.now()}
        # 交付状态必须随代传终态一起收敛：它是"产物是否真到了日志服务器"的唯一权威字段。
        # 只写 relay_status 的话，delivery_status 会永远停在 begin_delivery 写的 queued，
        # 前端据此判断"是否已交付"时永远拿不到 delivered——补交付入口会一直亮着。
        payload["delivery_finished_at"] = func.now()
        if status == "success":
            payload["delivery_status"] = "delivered"
            payload["delivery_error"] = None
            # 成功即清掉"最近一次失败"：留着只会让已交付的任务在界面上挂着一条历史错误，
            # 现场第一反应是"是不是又坏了"。失败任务则保留，供重试前判断。
            payload["relay_last_error"] = None
            payload["relay_last_error_at"] = None
        else:
            payload["delivery_status"] = "failed"
            payload["delivery_error"] = (error or "归档到日志服务器失败")[:2000]
        if error:
            payload["error_code"] = error_code
            payload["error_message"] = error[:2000]
        async with self.session_factory() as session:
            await session.execute(update(LogCollectTask).where(LogCollectTask.task_id == task_id).values(**payload))
            await session.commit()
