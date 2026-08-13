"""诊断证据对象存储边界及本地开发实现。"""

import hashlib
import os
import re
from collections.abc import AsyncIterator, Iterable
from pathlib import Path
from typing import Protocol

import anyio

from app.errors import DiagnosisError

_OBJECT_KEY_PATTERN = re.compile(r"^[A-Za-z0-9._/-]{1,1024}$")


class ObjectStorage(Protocol):
    """对象存储最小协议；生产环境可替换为 S3/KMS 适配器。"""

    def multipart_path(self, upload_id: str, part_number: int) -> Path:
        """返回隔离分片落盘路径。"""

    def object_path(self, object_key: str) -> Path:
        """解析受控对象键。"""

    async def write_part(
        self,
        *,
        upload_id: str,
        part_number: int,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
    ) -> tuple[int, str]:
        """流式写入单个分片。"""

    async def complete_multipart(
        self,
        *,
        upload_id: str,
        part_numbers: list[int],
        object_key: str,
        max_bytes: int,
    ) -> tuple[int, str]:
        """流式合并分片并返回大小和哈希。"""

    async def delete_object(self, object_key: str) -> bool:
        """删除对象，已不存在时保持幂等。"""


class LocalObjectStorage:
    """本地共享卷对象存储，限定用于开发、测试和单机部署。"""

    def __init__(self, root: str):
        self.root = Path(root).resolve()
        self.multipart_root = self.root / "multipart"
        self.objects_root = self.root / "objects"
        self.work_root = self.root / "work"
        for path in (self.multipart_root, self.objects_root, self.work_root):
            path.mkdir(parents=True, exist_ok=True, mode=0o700)

    def multipart_path(self, upload_id: str, part_number: int) -> Path:
        """生成不可逃逸的分片路径。"""

        if not re.fullmatch(r"[0-9a-fA-F-]{36}", upload_id) or not 1 <= part_number <= 10000:
            raise DiagnosisError(code="INVALID_UPLOAD_PART", message="上传分片标识不合法", http_status=422)
        directory = self.multipart_root / upload_id
        directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        return directory / f"{part_number:05d}.part"

    def object_path(self, object_key: str) -> Path:
        """把对象键限制在 objects 根目录。"""

        if not _OBJECT_KEY_PATTERN.fullmatch(object_key) or ".." in Path(object_key).parts:
            raise DiagnosisError(code="INVALID_OBJECT_KEY", message="对象存储键不合法", http_status=500)
        candidate = (self.objects_root / object_key).resolve()
        if self.objects_root != candidate and self.objects_root not in candidate.parents:
            raise DiagnosisError(code="INVALID_OBJECT_KEY", message="对象存储键越界", http_status=500)
        return candidate

    async def write_part(
        self,
        *,
        upload_id: str,
        part_number: int,
        chunks: AsyncIterator[bytes],
        max_bytes: int,
    ) -> tuple[int, str]:
        """流式写分片到临时文件，完成后原子替换。"""

        target = self.multipart_path(upload_id, part_number)
        temporary = target.with_suffix(".tmp")
        digest = hashlib.sha256()
        size = 0
        try:
            async with await anyio.open_file(temporary, "wb") as handle:
                async for chunk in chunks:
                    if not chunk:
                        continue
                    size += len(chunk)
                    if size > max_bytes:
                        raise DiagnosisError(
                            code="UPLOAD_PART_TOO_LARGE",
                            message="上传分片超过会话声明的大小上限",
                            http_status=413,
                        )
                    digest.update(chunk)
                    await handle.write(chunk)
            await anyio.to_thread.run_sync(os.replace, temporary, target)
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
        return size, digest.hexdigest()

    async def complete_multipart(
        self,
        *,
        upload_id: str,
        part_numbers: list[int],
        object_key: str,
        max_bytes: int,
    ) -> tuple[int, str]:
        """顺序流式合并全部分片，避免在 Web 进程构建整包内存副本。"""

        target = self.object_path(object_key)
        target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        temporary = target.with_suffix(target.suffix + ".tmp")

        def _merge() -> tuple[int, str]:
            digest = hashlib.sha256()
            total = 0
            try:
                with temporary.open("wb") as output:
                    for part_number in part_numbers:
                        part_path = self.multipart_path(upload_id, part_number)
                        if not part_path.is_file():
                            raise DiagnosisError(
                                code="UPLOAD_PART_MISSING",
                                message="上传分片不完整",
                                http_status=409,
                                details={"part_number": part_number},
                            )
                        with part_path.open("rb") as source:
                            while chunk := source.read(1024 * 1024):
                                total += len(chunk)
                                if total > max_bytes:
                                    raise DiagnosisError(
                                        code="BUNDLE_TOO_LARGE",
                                        message="证据包超过允许大小",
                                        http_status=413,
                                    )
                                digest.update(chunk)
                                output.write(chunk)
                os.replace(temporary, target)
            except BaseException:
                temporary.unlink(missing_ok=True)
                raise
            return total, digest.hexdigest()

        return await anyio.to_thread.run_sync(_merge)

    async def delete_multipart(self, upload_id: str) -> None:
        """清理上传会话分片目录。"""

        directory = self.multipart_root / upload_id

        def _delete() -> None:
            if not directory.is_dir():
                return
            for child in directory.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            directory.rmdir()

        await anyio.to_thread.run_sync(_delete)

    async def delete_object(self, object_key: str) -> bool:
        """幂等删除单个对象。"""

        path = self.object_path(object_key)
        existed = path.exists()
        if existed:
            await anyio.to_thread.run_sync(path.unlink)
        return existed

    async def iter_bytes(self, object_key: str, chunk_size: int = 1024 * 1024) -> AsyncIterator[bytes]:
        """流式读取对象。"""

        path = self.object_path(object_key)
        async with await anyio.open_file(path, "rb") as handle:
            while chunk := await handle.read(chunk_size):
                yield chunk

    def existing_parts(self, upload_id: str) -> Iterable[int]:
        """列出本地已存在的合法分片编号。"""

        directory = self.multipart_root / upload_id
        if not directory.is_dir():
            return ()
        return tuple(int(path.stem) for path in sorted(directory.glob("*.part")) if path.stem.isdigit())
