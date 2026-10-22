"""日志收集制品存储：分片写入、流式合并、原子落盘。

设计取舍（与 diagnosis-service 的差异）：

- 日志收集是 **1 任务 : 1 包 : 1 会话**，因此分片目录直接以 ``task_id`` 命名，
  不引入独立的 upload 会话表，也不生成额外的 ``upload_id``；
- 分片与合并全部流式处理，20 GiB 包不在进程内构建内存副本；
- 合并先写 ``.tmp`` 再 ``os.replace``，中途失败不会产生半成品对象；
- 已上传分片以「目录里存在哪些 ``.part``」为唯一事实，不落位图。

平台不提供删除 API：本模块的 ``delete_multipart`` 只在合并成功或显式 abort 时调用，
且只清理临时分片，不触碰 ``objects/`` 下的成品。
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

import anyio

_READ_BLOCK = 1024 * 1024


class PartSizeMismatchError(Exception):
    """分片实际大小与预期不符（非末片必须等于 chunk_size）。"""


class PartHashMismatchError(Exception):
    """分片 SHA-256 与声明值不符。"""


class PartMissingError(Exception):
    """合并时缺少分片。"""

    def __init__(self, part_number: int):
        super().__init__(f"分片 {part_number} 缺失")
        self.part_number = part_number


class ArchiveTooLargeError(Exception):
    """合并后大小超过上限。"""


class LocalLogCollectStorage:
    """本地共享卷实现，限定单机/K3s 单副本部署。"""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.multipart_root = self.root / "multipart"
        self.objects_root = self.root / "objects"
        for path in (self.multipart_root, self.objects_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    # ── 路径 ────────────────────────────────────────────────────────────────

    def _part_dir(self, upload_id: str) -> Path:
        return self.multipart_root / upload_id

    def part_path(self, upload_id: str, part_number: int) -> Path:
        return self._part_dir(upload_id) / f"{part_number}.part"

    def meta_path(self, upload_id: str) -> Path:
        return self._part_dir(upload_id) / "meta.json"

    def object_path(self, case_id: str, task_id: str) -> Path:
        return self.objects_root / case_id / f"{task_id}.zip"

    # ── 会话元数据 ──────────────────────────────────────────────────────────

    def read_meta(self, upload_id: str) -> dict[str, Any] | None:
        path = self.meta_path(upload_id)
        if not path.is_file():
            return None
        try:
            return json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None

    def write_meta(self, upload_id: str, meta: dict[str, Any]) -> None:
        directory = self._part_dir(upload_id)
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        target = self.meta_path(upload_id)
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
        os.replace(temporary, target)

    def existing_parts(self, upload_id: str) -> list[int]:
        directory = self._part_dir(upload_id)
        if not directory.is_dir():
            return []
        return sorted(
            int(child.stem)
            for child in directory.iterdir()
            if child.suffix == ".part" and child.stem.isdigit()
        )

    # ── 分片写入 ────────────────────────────────────────────────────────────

    async def write_part(
        self,
        *,
        upload_id: str,
        part_number: int,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
        claimed_sha256: str | None = None,
    ) -> tuple[int, str]:
        """流式写入单个分片，返回 (size, sha256)。同名分片覆盖写（幂等重传）。"""

        directory = self._part_dir(upload_id)
        if not directory.is_dir():
            raise FileNotFoundError(f"上传会话不存在：{upload_id}")

        target = self.part_path(upload_id, part_number)
        temporary = target.with_name(f"{target.name}.tmp")
        digest = hashlib.sha256()
        total = 0

        try:
            async with await anyio.open_file(temporary, "wb") as output:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    total += len(chunk)
                    if total > max_bytes:
                        raise PartSizeMismatchError(
                            f"分片 {part_number} 超过允许大小 {max_bytes} 字节"
                        )
                    digest.update(chunk)
                    await output.write(chunk)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        actual = digest.hexdigest()
        if claimed_sha256 and claimed_sha256 != actual:
            temporary.unlink(missing_ok=True)
            raise PartHashMismatchError(
                f"分片 {part_number} SHA-256 不匹配：声明 {claimed_sha256}，实际 {actual}"
            )
        if total == 0:
            temporary.unlink(missing_ok=True)
            raise PartSizeMismatchError(f"分片 {part_number} 为空")

        await anyio.to_thread.run_sync(os.replace, temporary, target)
        return total, actual

    # ── 合并 ────────────────────────────────────────────────────────────────

    async def merge(
        self,
        *,
        upload_id: str,
        part_numbers: list[int],
        case_id: str,
        task_id: str,
        max_bytes: int,
    ) -> tuple[int, str, str]:
        """按序流式合并分片，返回 (size, sha256, object_key)。"""

        target = self.object_path(case_id, task_id)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_name(f"{target.name}.tmp")
        digest = hashlib.sha256()
        total = 0

        try:
            async with await anyio.open_file(temporary, "wb") as output:
                for part_number in part_numbers:
                    part_path = self.part_path(upload_id, part_number)
                    if not part_path.is_file():
                        raise PartMissingError(part_number)
                    async with await anyio.open_file(part_path, "rb") as source:
                        while True:
                            block = await source.read(_READ_BLOCK)
                            if not block:
                                break
                            total += len(block)
                            if total > max_bytes:
                                raise ArchiveTooLargeError(
                                    f"合并后超过上限 {max_bytes} 字节"
                                )
                            digest.update(block)
                            await output.write(block)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise

        await anyio.to_thread.run_sync(os.replace, temporary, target)
        object_key = f"{case_id}/{task_id}.zip"
        return total, digest.hexdigest(), object_key

    # ── 清理临时分片 ────────────────────────────────────────────────────────

    async def delete_multipart(self, upload_id: str) -> None:
        directory = self._part_dir(upload_id)

        def _delete() -> None:
            if not directory.is_dir():
                return
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            directory.rmdir()

        await anyio.to_thread.run_sync(_delete)

    def storage_usage_bytes(self) -> int:
        """平台存储占用（仅统计对象目录），供容量告警使用；不触发任何删除。"""

        total = 0
        for root, _dirs, files in os.walk(self.objects_root):
            for name in files:
                try:
                    total += os.path.getsize(os.path.join(root, name))
                except OSError:
                    continue
        return total
