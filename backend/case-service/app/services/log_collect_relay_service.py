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
import math
import re
import time
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select, update

from ..models.log_collect import LogCollectTask
from .log_collect_storage import LocalLogCollectStorage

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

    async def init_upload(
        self, client: httpx.AsyncClient, filename: str, file_size: int
    ) -> tuple[str, int]:
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

    # ── 抢占 ────────────────────────────────────────────────────────────────

    async def trigger(self, task_id: UUID, *, force: bool = False) -> bool:
        """抢占式启动：只有把 relay_status 从非 running 改成 running 的调用者才执行。

        `force` 用于手动重试（上一进程崩溃导致状态卡在 running 时）。
        """

        allowed = ["none", "pending", "failed"]
        if force:
            allowed.append("running")
        async with self.session_factory() as session:
            result = await session.execute(
                update(LogCollectTask)
                .where(
                    LogCollectTask.task_id == task_id,
                    LogCollectTask.relay_status.in_(allowed),
                )
                .values(relay_status="running", relay_updated_at=func.now())
            )
            await session.commit()
            return (result.rowcount or 0) > 0

    async def snapshot(self, task_id: UUID) -> dict[str, Any] | None:
        async with self.session_factory() as session:
            result = await session.execute(
                select(LogCollectTask).where(LogCollectTask.task_id == task_id)
            )
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
                "relay_updated_at": task.relay_updated_at,
                "archive_path": task.archive_path,
            }

    # ── 主流程 ──────────────────────────────────────────────────────────────

    async def run(self, task_id: UUID) -> None:
        """代传主入口：瞬时错误自动退避重试，永久错误立即落终态。

        第一性原理：采集已在节点侧成功（包已生成），中继/5888 仅因瞬时网络抖动或
        服务端暂不可达而失败。若不重试直接写 failed，用户会看到"已采到却交付失败"且
        无法自愈——网络恢复后这包本应自动补传。但"无制品 / 空文件"属永久错误，
        重试必然同样失败，直接落 failed，避免无谓等待与重复全量重传。
        """
        last_exc: Exception | None = None
        for attempt in range(1, self.max_attempts + 1):
            try:
                await self._run(task_id)
                return
            except Exception as exc:  # noqa: BLE001 - 异常要么落终态要么退避重试
                last_exc = exc
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
                await asyncio.sleep(
                    min(self.retry_base_delay * (2 ** (attempt - 1)), self.retry_max_delay)
                )
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
            raise SangforRelayError(
                f"工单号不合法，无法确定归档文件名：{value or '<空>'}", "LC-TRANSFER-001"
            )
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
            result = await session.execute(
                select(LogCollectTask).where(LogCollectTask.task_id == task_id)
            )
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

        async with self.session_factory() as session:
            await session.execute(
                update(LogCollectTask).where(LogCollectTask.task_id == task_id).values(**payload)
            )
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
        else:
            payload["delivery_status"] = "failed"
            payload["delivery_error"] = (error or "归档到日志服务器失败")[:2000]
        if error:
            payload["error_code"] = error_code
            payload["error_message"] = error[:2000]
        async with self.session_factory() as session:
            await session.execute(
                update(LogCollectTask).where(LogCollectTask.task_id == task_id).values(**payload)
            )
            await session.commit()
