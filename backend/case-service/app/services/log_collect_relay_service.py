"""日志收集制品代传 5888（P3 段③）。

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
import math
import time
from typing import Any
from uuid import UUID

import httpx
from sqlalchemy import func, select, update

from ..models.log_collect import LogCollectTask
from .log_collect_storage import LocalLogCollectStorage

RELAY_STATUS_VALUES = ("none", "pending", "running", "success", "failed")


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
    ):
        self.session_factory = session_factory
        self.storage = storage
        self.client = client
        self.poll_interval = poll_interval
        self.archive_timeout = archive_timeout

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
        try:
            await self._run(task_id)
        except Exception as exc:  # noqa: BLE001 - 任何异常都必须落到 failed，不能静默
            await self._finish(task_id, "failed", error=str(exc))
            raise

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
                upload_id, chunk_size = await self.client.init_upload(client, path.name, size)
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
                    filename=path.name,
                    payload=payload,
                )
                # 每块成功后立即持久化：进程崩溃时从这里继续，不重传已完成分片。
                await self._save(task_id, upload_id=upload_id, chunk_index=index)
                if status == "complete":
                    break

            await self._await_archive(client, upload_id)
            await self._finish(task_id, "success")

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

    async def _finish(self, task_id: UUID, status: str, *, error: str | None = None) -> None:
        payload: dict[str, Any] = {"relay_status": status, "relay_updated_at": func.now()}
        if error:
            payload["error_code"] = "LC-TRANSFER-003"
            payload["error_message"] = error[:2000]
        async with self.session_factory() as session:
            await session.execute(
                update(LogCollectTask).where(LogCollectTask.task_id == task_id).values(**payload)
            )
            await session.commit()
