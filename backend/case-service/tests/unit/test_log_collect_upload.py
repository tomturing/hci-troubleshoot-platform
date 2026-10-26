"""分块上传：存储层与服务层的行为测试（P2 段②）。

覆盖点：
- 分片写入的幂等覆盖、大小与 SHA-256 校验；
- 合并的流式顺序、缺片与超限；
- init 幂等与「大小变化必须 abort」的语义；
- 数据面 token 的绑定与过期。
"""

import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.services.log_collect_storage import (
    ArchiveTooLargeError,
    LocalLogCollectStorage,
    PartHashMismatchError,
    PartMissingError,
    PartSizeMismatchError,
)
from app.services.log_collect_upload_service import (
    ArchiveSizeExceededError,
    LogCollectUploadError,
    LogCollectUploadService,
    PartValidationError,
    UploadSessionNotFoundError,
)

CHUNK = 8


def chunks_of(payload: bytes, block: int = 3):
    async def iterator():
        for start in range(0, len(payload), block):
            yield payload[start : start + block]

    return iterator()


@pytest.fixture
def storage(tmp_path):
    return LocalLogCollectStorage(str(tmp_path / "log-collect"))


@pytest.fixture
def repo():
    repo = MagicMock()
    repo.flush = AsyncMock()
    repo.refresh = AsyncMock()
    return repo


@pytest.fixture
def service(storage, repo):
    return LogCollectUploadService(
        repo,
        storage,
        chunk_size=CHUNK,
        max_archive_bytes=1024,
        token_ttl_hours=1,
        secret=b"test-secret-32-bytes-padding-0000",
    )


def make_task(total_size: int = 20, sha256: str | None = None):
    task = SimpleNamespace(
        task_id=uuid4(),
        case_id="Q2026102200001",
        archive_path=None,
        archive_size=None,
        archive_sha256=None,
    )
    return task, total_size, sha256


# ── 存储层 ───────────────────────────────────────────────────────────────────


async def test_write_part_is_idempotent_and_reports_size(storage):
    storage.write_meta("u1", {"total_size": 20, "chunk_size": CHUNK})
    first = await storage.write_part(
        upload_id="u1", part_number=1, chunks=chunks_of(b"a" * CHUNK), max_bytes=CHUNK
    )
    second = await storage.write_part(
        upload_id="u1", part_number=1, chunks=chunks_of(b"b" * CHUNK), max_bytes=CHUNK
    )

    assert first[0] == CHUNK and second[0] == CHUNK
    assert storage.existing_parts("u1") == [1]
    # 覆盖写：内容以最后一次为准
    assert storage.part_path("u1", 1).read_bytes() == b"b" * CHUNK


async def test_write_part_rejects_hash_mismatch(storage):
    storage.write_meta("u2", {"total_size": 20, "chunk_size": CHUNK})
    with pytest.raises(PartHashMismatchError):
        await storage.write_part(
            upload_id="u2",
            part_number=1,
            chunks=chunks_of(b"x" * CHUNK),
            max_bytes=CHUNK,
            claimed_sha256=hashlib.sha256(b"y" * CHUNK).hexdigest(),
        )
    assert storage.existing_parts("u2") == []


async def test_write_part_rejects_oversize(storage):
    storage.write_meta("u3", {"total_size": 20, "chunk_size": CHUNK})
    with pytest.raises(PartSizeMismatchError):
        await storage.write_part(
            upload_id="u3", part_number=1, chunks=chunks_of(b"z" * (CHUNK + 1)), max_bytes=CHUNK
        )


async def test_merge_is_sequential_and_atomic(storage):
    storage.write_meta("u4", {"total_size": 20, "chunk_size": CHUNK})
    parts = [b"12345678", b"9abcdefg", b"hij"]
    for index, payload in enumerate(parts, start=1):
        await storage.write_part(
            upload_id="u4", part_number=index, chunks=chunks_of(payload), max_bytes=CHUNK
        )

    size, digest, key = await storage.merge(
        upload_id="u4",
        part_numbers=[1, 2, 3],
        case_id="Q2026102200001",
        task_id="t1",
        max_bytes=1024,
    )

    merged = b"".join(parts)
    assert size == len(merged)
    assert digest == hashlib.sha256(merged).hexdigest()
    assert key == "Q2026102200001/t1.zip"
    assert storage.object_path("Q2026102200001", "t1").read_bytes() == merged
    # 不存在半成品
    assert not list(storage.objects_root.rglob("*.tmp"))


async def test_merge_reports_missing_part(storage):
    storage.write_meta("u5", {"total_size": 20, "chunk_size": CHUNK})
    await storage.write_part(
        upload_id="u5", part_number=1, chunks=chunks_of(b"1" * CHUNK), max_bytes=CHUNK
    )
    with pytest.raises(PartMissingError) as exc:
        await storage.merge(
            upload_id="u5", part_numbers=[1, 2], case_id="c", task_id="t", max_bytes=1024
        )
    assert exc.value.part_number == 2


async def test_merge_rejects_over_limit(storage):
    storage.write_meta("u6", {"total_size": 20, "chunk_size": CHUNK})
    for index in (1, 2):
        await storage.write_part(
            upload_id="u6", part_number=index, chunks=chunks_of(b"1" * CHUNK), max_bytes=CHUNK
        )
    with pytest.raises(ArchiveTooLargeError):
        await storage.merge(
            upload_id="u6", part_numbers=[1, 2], case_id="c", task_id="t", max_bytes=10
        )


# ── 服务层：init ────────────────────────────────────────────────────────────


async def test_init_is_idempotent(service):
    task, total, sha = make_task()
    first = await service.init(task, total_size=total, expected_sha256=sha)
    second = await service.init(task, total_size=total, expected_sha256=sha)

    assert first["upload_id"] == second["upload_id"] == str(task.task_id)
    assert first["part_count"] == 3  # ceil(20 / 8)
    assert second["uploaded_parts"] == []


async def test_init_rejects_size_change(service):
    task, total, sha = make_task()
    await service.init(task, total_size=total, expected_sha256=sha)
    with pytest.raises(PartValidationError) as exc:
        await service.init(task, total_size=total + 1, expected_sha256=sha)
    assert exc.value.status == 409


async def test_init_rejects_oversize_archive(service):
    task, _total, sha = make_task()
    with pytest.raises(ArchiveSizeExceededError):
        await service.init(task, total_size=2048, expected_sha256=sha)


async def test_init_clears_stale_parts(service, storage):
    task, total, sha = make_task()
    # 模拟上一次异常退出留下的残缺分片（无 meta.json）
    directory = storage.multipart_root / str(task.task_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "1.part").write_bytes(b"stale")

    result = await service.init(task, total_size=total, expected_sha256=sha)
    assert result["uploaded_parts"] == []
    assert storage.existing_parts(str(task.task_id)) == []


# ── 服务层：token ───────────────────────────────────────────────────────────


def test_token_is_bound_to_task(service):
    task, *_ = make_task()
    token = service.issue_token(str(task.task_id))
    assert service.verify_token(str(task.task_id), token) is True
    assert service.verify_token(str(uuid4()), token) is False
    assert service.verify_token(str(task.task_id), "bogus") is False


def test_expired_token_rejected(storage, repo):
    service = LogCollectUploadService(
        repo, storage, chunk_size=CHUNK, token_ttl_hours=-1, secret=b"s" * 32
    )
    task, *_ = make_task()
    assert service.verify_token(str(task.task_id), service.issue_token(str(task.task_id))) is False


# ── 服务层：分片与合并 ──────────────────────────────────────────────────────


async def test_record_part_validates_range(service):
    task, total, sha = make_task()
    await service.init(task, total_size=total, expected_sha256=sha)

    with pytest.raises(PartValidationError) as exc:
        await service.record_part(
            task.task_id, part_number=0, chunks=chunks_of(b"x"), claimed_sha256=None
        )
    assert exc.value.status == 400

    with pytest.raises(PartValidationError):
        await service.record_part(
            task.task_id, part_number=4, chunks=chunks_of(b"x"), claimed_sha256=None
        )


async def test_record_part_requires_exact_size(service):
    task, total, sha = make_task()
    await service.init(task, total_size=total, expected_sha256=sha)
    # 非末片必须 8 字节
    with pytest.raises(PartValidationError):
        await service.record_part(
            task.task_id, part_number=1, chunks=chunks_of(b"short"), claimed_sha256=None
        )


async def test_complete_updates_task_and_cleans_parts(service, repo, storage):
    task, total, _sha = make_task()
    await service.init(task, total_size=total, expected_sha256=None)

    payload = b"12345678" + b"9abcdefg" + b"hijk"
    for index, start in enumerate((0, CHUNK, 2 * CHUNK), start=1):
        await service.record_part(
            task.task_id,
            part_number=index,
            chunks=chunks_of(payload[start : start + CHUNK]),
            claimed_sha256=None,
        )

    result = await service.complete(
        task,
        parts=None,
        declared_sha256=hashlib.sha256(payload).hexdigest(),
        declared_size=len(payload),
    )

    assert result["size"] == len(payload)
    assert result["object_key"] == f"{task.case_id}/{task.task_id}.zip"
    assert task.archive_sha256 == result["sha256"]
    assert task.archive_size == len(payload)
    assert repo.flush.await_count == 1
    # 临时分片已清理
    assert storage.existing_parts(str(task.task_id)) == []


async def test_complete_rejects_missing_parts(service):
    task, total, _sha = make_task()
    await service.init(task, total_size=total, expected_sha256=None)
    await service.record_part(
        task.task_id, part_number=1, chunks=chunks_of(b"1" * CHUNK), claimed_sha256=None
    )
    with pytest.raises(LogCollectUploadError) as exc:
        await service.complete(task, parts=None, declared_sha256=None, declared_size=None)
    assert exc.value.code == "LC-TRANSFER-003"


async def test_complete_rejects_sha_mismatch_and_drops_parts(service, storage):
    task, total, _sha = make_task()
    await service.init(task, total_size=total, expected_sha256=None)
    payload = b"12345678" + b"9abcdefg" + b"hijk"
    for index, start in enumerate((0, CHUNK, 2 * CHUNK), start=1):
        await service.record_part(
            task.task_id,
            part_number=index,
            chunks=chunks_of(payload[start : start + CHUNK]),
            claimed_sha256=None,
        )

    with pytest.raises(PartValidationError):
        await service.complete(
            task, parts=None, declared_sha256="0" * 64, declared_size=len(payload)
        )
    # 校验失败必须丢弃分片，避免半成品被继续使用
    assert storage.existing_parts(str(task.task_id)) == []


async def test_status_raises_when_session_missing(service):
    with pytest.raises(UploadSessionNotFoundError):
        await service.status(uuid4())
