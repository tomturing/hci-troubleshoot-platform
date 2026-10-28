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
from app.schemas.log_collect import UploadCompleteRequest
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
from pydantic import ValidationError

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
    # complete 在合并前与落库后会各显式 commit 一次（否则连接以
    # idle-in-transaction 超过 30 秒被 PostgreSQL 杀掉、或与代传 trigger
    # 死锁）。这里显式给出可 await 的 commit，避免依赖 MagicMock 的隐式异步协议。
    repo.commit = AsyncMock()
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
    # 声明了整包摘要且核对通过 → 这才是 verified 的唯一来源
    assert result["integrity_state"] == "verified"
    assert task.integrity_state == "verified"
    # 本服务只在两个点提交：合并前清事务、落库后放行。本层不再 flush。
    assert repo.flush.await_count == 0
    assert repo.commit.await_count == 2
    # 临时分片已清理
    assert storage.existing_parts(str(task.task_id)) == []


async def test_complete_without_digest_stays_unverified(service, repo, storage):
    """没声明摘要时必须落 unverified，不能因为"平台算出了 sha256"就宣称校验通过。"""

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

    result = await service.complete(task, parts=None, declared_sha256=None, declared_size=None)

    assert result["integrity_state"] == "unverified"
    assert task.integrity_state == "unverified"
    assert task.object_sha256 == result["sha256"]


async def test_complete_accepts_uppercase_declared_digest(service, repo, storage):
    """客户端发大写十六进制不能被误判成不一致——否则会删掉一个本来完整的制品。"""

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
        task, parts=None, declared_sha256=hashlib.sha256(payload).hexdigest().upper(), declared_size=None
    )

    assert result["integrity_state"] == "verified"


async def test_complete_commits_before_merging(service, repo, storage, monkeypatch):
    """合并（分钟级纯文件 I/O）之前必须已经提交只读事务。

    现场 Q2026102800720：complete 在同一个事务里合并 3.6 GiB（~50s），
    连接级 idle_in_transaction_session_timeout=30s 把连接 terminate 了，
    随后 flush 报 "connection is closed" → 500，包已合并却在库里查不到。
    这条断言锁的是顺序：merge 被调用时，commit 必须已经发生过。
    """
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

    observed: list[int] = []
    original_merge = storage.merge

    async def spy_merge(**kwargs):
        observed.append(repo.commit.await_count)
        return await original_merge(**kwargs)

    monkeypatch.setattr(storage, "merge", spy_merge)

    await service.complete(task, parts=None, declared_sha256=None, declared_size=None)

    assert observed == [1], "合并发生时事务必须已经提交（否则会被 30s 空闲超时杀掉）"
    # 两次提交：合并前清事务 + 落库后放行。第二次必须发生在 trigger 代传之前，
    # 否则 trigger（另一个 session 的 UPDATE）会与本事务的行锁互等成死锁
    # （Q2026101500828：commit 报 InternalClientError 与"平台尚无制品"相隔 16ms；
    # staging 同日实测复现：若拖到请求收尾才提交，trigger 等行锁、主协程等 trigger、
    # 提交等路由返回，构成自死锁，只能靠 30s idle 超时"解死锁"，代价是写入整体
    # 回滚而客户端拿到 200。）
    assert repo.commit.await_count == 2


async def test_complete_retry_reuses_merged_object(service, repo, storage):
    """落库失败后重试不得重写整包。

    现场同一条任务重试了 4 次，每次都把 3.6 GiB 重新合并一遍——峰值磁盘翻倍、
    每次又都超过 30s，重试注定原地打转。合并成功即落 marker，重试直接复用。
    """
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

    # 第一次：合并成功，随后落库 commit 失败（模拟连接已被 PG 杀掉）。
    # commit 有两个调用点（合并前/落库后），只让**第二次**抛错——
    # 如果第一次就抛，merge 根本不会执行，测不到"成品已落盘但元数据丢失"。
    commit_calls = {"n": 0}

    async def failing_commit():
        commit_calls["n"] += 1
        if commit_calls["n"] == 2:
            raise RuntimeError("connection is closed")

    repo.commit.side_effect = failing_commit
    with pytest.raises(RuntimeError):
        await service.complete(task, parts=None, declared_sha256=None, declared_size=None)
    assert commit_calls["n"] == 2, "失败必须发生在落库提交，而不是合并前的清事务"
    repo.commit.side_effect = None

    target = storage.object_path(task.case_id, str(task.task_id))
    assert target.is_file(), "合并成品应已落盘（这正是现场的真实状态）"
    first_mtime = target.stat().st_mtime_ns

    result = await service.complete(task, parts=None, declared_sha256=None, declared_size=None)

    assert result["sha256"] == hashlib.sha256(payload).hexdigest()
    assert target.stat().st_mtime_ns == first_mtime, "重试应复用成品，而不是重写一遍"
    assert task.archive_sha256 == result["sha256"]


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


async def test_status_returns_usable_token_for_resume(service):
    """断点续传重提必须能拿到可用令牌。

    历史缺陷：status() 不签发 token，前端"证书失败→确认信任→刷新会话→重试"这条路
    于是必然带空令牌提交，bridge 回 `task_id 与 upload_token 必填`。前端表现成
    "参数没传"，真实原因却是会话接口少一个字段——错误信息与根因完全脱节。
    """
    task, total, _sha = make_task()
    await service.init(task, total_size=total, expected_sha256=None)

    snapshot = await service.status(task.task_id)

    assert snapshot["upload_token"], "续传刷新必须返回令牌"
    # 令牌必须真能通过数据面校验，而不只是"非空字符串"
    assert service.verify_token(str(task.task_id), snapshot["upload_token"]) is True
    # 跨任务不可用：令牌绑定 task_id
    assert service.verify_token(str(uuid4()), snapshot["upload_token"]) is False


# ── 收尾请求体契约 ───────────────────────────────────────────────────────────


def test_complete_request_treats_blank_sha256_as_undeclared():
    """空串 sha256 必须等同于"未声明"。

    现场回归（Q2026102701193，2026-10-27）：bridge 收尾固定发
    ``{"parts": ..., "size": ..., "sha256": ""}``，而字段 pattern 只接受 64 位 hex，
    空串在请求体校验层直接 422 —— 业务代码没被调用，15 个分片（1.4 GB）全部白传。
    放宽必须落在服务端：客户机上的 bridge 不会随平台发布更新。
    """
    request = UploadCompleteRequest(parts=[1, 2], size=20, sha256="")
    assert request.sha256 is None
    assert request.size == 20
    assert request.parts == [1, 2]


def test_complete_request_keeps_valid_sha256():
    digest = "a" * 64
    assert UploadCompleteRequest(sha256=digest).sha256 == digest


def test_complete_request_rejects_malformed_sha256():
    # 放宽只针对"空串"，真正的非法值仍必须拦住
    with pytest.raises(ValidationError):
        UploadCompleteRequest(sha256="not-a-hash")
