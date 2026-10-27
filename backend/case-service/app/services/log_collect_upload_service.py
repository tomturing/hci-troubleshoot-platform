"""日志收集制品分块上传（P2 段②：客户 PC → 平台）。

关键设计：

- **会话标识即 task_id**：1 任务 : 1 包 : 1 会话，不引入 upload_id 与独立表；
- **init 幂等**：同一任务重复 init 返回既有会话与已传分片，重连不会产生孤儿分片；
- **分片幂等**：同序号重传覆盖写，与 5888 的行为保持一致，便于客户端统一重试策略；
- **数据面 token**：直连路径不经过 API Gateway（网关限制 1 MiB / 豁免 10 MiB），
  因此用 HMAC 签发的短期 token 鉴权，token 绑定 task_id 且不可跨任务复用；
- **平台不做自动清理**：合并成功或显式 abort 只清理临时分片，``objects/`` 下的成品
  只能由运维删除。
"""

from __future__ import annotations

import hashlib
import hmac
import math
import time
from collections.abc import AsyncIterator
from typing import Any
from uuid import UUID

from ..config import settings
from ..models.log_collect import LogCollectTask
from ..repositories.log_collect_repo import LogCollectTaskRepository
from .log_collect_storage import (
    ArchiveTooLargeError,
    LocalLogCollectStorage,
    LogCollectStorageInsufficientError,
    LogCollectStorageUnavailableError,
    PartHashMismatchError,
    PartMissingError,
    PartSizeMismatchError,
)

# 单分片允许的上下限：上限防止单请求过大，下限防止分片过碎导致请求风暴。
MIN_CHUNK_SIZE = 8 * 1024 * 1024
MAX_CHUNK_SIZE = 64 * 1024 * 1024


class LogCollectUploadError(Exception):
    """上传类业务异常（错误码见 log_collect_error_catalog.json）。"""

    code = "LC-TRANSFER-003"
    status = 502

    def __init__(self, message: str, code: str | None = None, status: int | None = None):
        super().__init__(message)
        if code:
            self.code = code
        if status:
            self.status = status


class UploadSessionNotFoundError(LogCollectUploadError):
    """会话不存在或已过期（分片目录已被清理）。"""

    code = "LC-TRANSFER-004"
    status = 500


class PartValidationError(LogCollectUploadError):
    """分片大小或校验和不符。"""

    code = "LC-TRANSFER-002"
    status = 500


class ArchiveSizeExceededError(LogCollectUploadError):
    """总大小超过平台上限。"""

    code = "LC-TRANSFER-005"
    status = 413


class LogCollectUploadService:
    def __init__(
        self,
        repo: LogCollectTaskRepository,
        storage: LocalLogCollectStorage,
        *,
        chunk_size: int | None = None,
        max_archive_bytes: int | None = None,
        token_ttl_hours: int | None = None,
        secret: bytes | None = None,
    ):
        self.repository = repo
        self.storage = storage
        self.chunk_size = chunk_size or settings.LOG_COLLECT_CHUNK_SIZE_BYTES
        self.max_archive_bytes = max_archive_bytes or settings.LOG_COLLECT_MAX_ARCHIVE_BYTES
        self.token_ttl_seconds = (token_ttl_hours or settings.LOG_COLLECT_UPLOAD_TOKEN_TTL_HOURS) * 3600
        self.secret = secret or self._derive_secret()

    @staticmethod
    def _derive_secret() -> bytes:
        raw = settings.LOG_COLLECT_UPLOAD_SECRET or settings.INTERNAL_API_TOKEN
        return hashlib.sha256(f"log-collect-upload:{raw}".encode()).digest()

    # ── token ───────────────────────────────────────────────────────────────

    def issue_token(self, task_id: str) -> str:
        expires_at = int(time.time()) + self.token_ttl_seconds
        signature = hmac.new(
            self.secret, f"{task_id}:{expires_at}".encode(), hashlib.sha256
        ).hexdigest()
        return f"{expires_at}.{signature}"

    def verify_token(self, task_id: str, token: str) -> bool:
        expires_text, _, signature = token.partition(".")
        if not expires_text.isdigit() or not signature:
            return False
        if int(expires_text) < int(time.time()):
            return False
        expected = hmac.new(
            self.secret, f"{task_id}:{expires_text}".encode(), hashlib.sha256
        ).hexdigest()
        return hmac.compare_digest(signature, expected)

    # ── 会话 ────────────────────────────────────────────────────────────────

    def part_count_for(self, total_size: int) -> int:
        return max(1, math.ceil(total_size / self.chunk_size))

    def expected_part_size(self, part_number: int, total_size: int) -> int:
        """非末片必须等于 chunk_size；末片为剩余字节。"""

        consumed = (part_number - 1) * self.chunk_size
        return min(self.chunk_size, total_size - consumed)

    async def init(
        self, task: LogCollectTask, *, total_size: int, expected_sha256: str | None
    ) -> dict[str, Any]:
        if total_size <= 0:
            raise PartValidationError("total_size 必须为正数", code="LC-TRANSFER-002", status=400)
        if total_size > self.max_archive_bytes:
            raise ArchiveSizeExceededError(
                f"日志包大小 {total_size} 超过平台上限 {self.max_archive_bytes}"
            )

        # 合并阶段要在分片之外再写一份等大的 .tmp，峰值约 2 倍包体。
        # 在这里先算清楚，容量不足时直接给出"需要/可用"的具体数字，
        # 而不是等写完一半再抛 ENOSPC 变成未知 500。
        try:
            self.storage.check_capacity(total_size * 2)
        except LogCollectStorageInsufficientError as exc:
            raise LogCollectUploadError(str(exc), code="LC-PLATFORM-008", status=503) from exc
        except LogCollectStorageUnavailableError as exc:
            raise LogCollectUploadError(str(exc), code="LC-PLATFORM-007", status=503) from exc

        upload_id = str(task.task_id)
        meta = self.storage.read_meta(upload_id)

        if meta is not None:
            if int(meta.get("total_size", 0)) != total_size:
                raise PartValidationError(
                    "同一任务已存在大小不同的上传会话，请先 abort 再重新初始化",
                    code="LC-TRANSFER-004",
                    status=409,
                )
            chunk_size = int(meta.get("chunk_size", self.chunk_size))
        else:
            # 目录可能存在历史残留（上一次异常退出），直接清空后重建。
            await self.storage.delete_multipart(upload_id)
            chunk_size = self.chunk_size
            self.storage.write_meta(
                upload_id,
                {
                    "task_id": upload_id,
                    "case_id": task.case_id,
                    "total_size": total_size,
                    "chunk_size": chunk_size,
                    "expected_sha256": expected_sha256,
                    "created_at": int(time.time()),
                },
            )

        parts = self.storage.existing_parts(upload_id)
        return {
            "upload_id": upload_id,
            "upload_token": self.issue_token(upload_id),
            "chunk_size": chunk_size,
            "part_count": self.part_count_for(total_size),
            "total_size": total_size,
            "uploaded_parts": parts,
            "missing_parts": self._missing(parts, self.part_count_for(total_size)),
        }

    async def status(self, task_id: UUID) -> dict[str, Any]:
        upload_id = str(task_id)
        meta = self.storage.read_meta(upload_id)
        if meta is None:
            raise UploadSessionNotFoundError(f"上传会话不存在或已过期：{upload_id}")
        total_size = int(meta["total_size"])
        part_count = self.part_count_for(total_size)
        parts = self.storage.existing_parts(upload_id)
        return {
            "upload_id": upload_id,
            "chunk_size": int(meta["chunk_size"]),
            "part_count": part_count,
            "total_size": total_size,
            "uploaded_parts": parts,
            "missing_parts": self._missing(parts, part_count),
        }

    async def abort(self, task_id: UUID) -> None:
        await self.storage.delete_multipart(str(task_id))

    @staticmethod
    def _missing(uploaded: list[int], part_count: int) -> list[int]:
        done = set(uploaded)
        return [n for n in range(1, part_count + 1) if n not in done]

    # ── 分片 ────────────────────────────────────────────────────────────────

    async def record_part(
        self,
        task_id: UUID,
        *,
        part_number: int,
        chunks: AsyncIterator[bytes],
        claimed_sha256: str | None,
    ) -> dict[str, Any]:
        upload_id = str(task_id)
        meta = self.storage.read_meta(upload_id)
        if meta is None:
            raise UploadSessionNotFoundError(f"上传会话不存在或已过期：{upload_id}")

        total_size = int(meta["total_size"])
        part_count = self.part_count_for(total_size)
        if part_number < 1 or part_number > part_count:
            raise PartValidationError(
                f"分片序号越界：{part_number} ∉ [1, {part_count}]", status=400
            )

        expected = self.expected_part_size(part_number, total_size)
        try:
            size, digest = await self.storage.write_part(
                upload_id=upload_id,
                part_number=part_number,
                chunks=chunks,
                max_bytes=expected,
                claimed_sha256=claimed_sha256,
            )
        except PartHashMismatchError as exc:
            raise PartValidationError(str(exc)) from exc
        except PartSizeMismatchError as exc:
            raise PartValidationError(str(exc)) from exc

        if size != expected:
            # max_bytes 已经拦截超长，这里只剩「偏短」一种可能（末片除外）。
            raise PartValidationError(
                f"分片 {part_number} 大小不符：期望 {expected}，实际 {size}"
            )

        return {
            "upload_id": upload_id,
            "part_number": part_number,
            "size": size,
            "sha256": digest,
            "uploaded_parts": len(self.storage.existing_parts(upload_id)),
            "part_count": part_count,
        }

    # ── 合并 ────────────────────────────────────────────────────────────────

    async def complete(
        self,
        task: LogCollectTask,
        *,
        parts: list[int] | None,
        declared_sha256: str | None,
        declared_size: int | None,
    ) -> dict[str, Any]:
        upload_id = str(task.task_id)
        meta = self.storage.read_meta(upload_id)
        if meta is None:
            raise UploadSessionNotFoundError(f"上传会话不存在或已过期：{upload_id}")

        total_size = int(meta["total_size"])
        part_count = self.part_count_for(total_size)
        uploaded = self.storage.existing_parts(upload_id)
        missing = self._missing(uploaded, part_count)
        if missing:
            raise LogCollectUploadError(
                f"分片不完整，缺少 {len(missing)} 个：{[n for n in missing[:10]]}",
                code="LC-TRANSFER-003",
                status=409,
            )

        part_numbers = sorted(parts) if parts else list(range(1, part_count + 1))
        if part_numbers != list(range(1, part_count + 1)):
            raise PartValidationError("parts 必须是从 1 到 part_count 的完整序列", status=400)

        try:
            size, digest, object_key = await self.storage.merge(
                upload_id=upload_id,
                part_numbers=part_numbers,
                case_id=task.case_id,
                task_id=upload_id,
                max_bytes=self.max_archive_bytes,
            )
        except PartMissingError as exc:
            raise LogCollectUploadError(str(exc), code="LC-TRANSFER-003", status=409) from exc
        except ArchiveTooLargeError as exc:
            raise ArchiveSizeExceededError(str(exc)) from exc
        except LogCollectStorageInsufficientError as exc:
            # 合并过程中被写满（init 时的预算被其它并发上传吃掉）：同样是容量问题。
            raise LogCollectUploadError(str(exc), code="LC-PLATFORM-008", status=503) from exc
        except LogCollectStorageUnavailableError as exc:
            raise LogCollectUploadError(str(exc), code="LC-PLATFORM-007", status=503) from exc

        if declared_size is not None and declared_size != size:
            await self.storage.delete_multipart(upload_id)
            raise PartValidationError(
                f"合并后大小不一致：声明 {declared_size}，实际 {size}"
            )
        expected_sha256 = declared_sha256 or meta.get("expected_sha256")
        if expected_sha256 and expected_sha256 != digest:
            await self.storage.delete_multipart(upload_id)
            raise PartValidationError(
                f"合并后 SHA-256 不一致：期望 {expected_sha256}，实际 {digest}"
            )

        # 制品元数据落库；任务状态由前端在 P2 各阶段跃迁时 PATCH，此处不改 status。
        task.archive_path = object_key
        task.archive_size = size
        task.archive_sha256 = digest
        await self.repository.flush()
        await self.repository.refresh(task)

        await self.storage.delete_multipart(upload_id)
        return {
            "upload_id": upload_id,
            "object_key": object_key,
            "size": size,
            "sha256": digest,
            "part_count": part_count,
        }
