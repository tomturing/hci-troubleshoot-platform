"""并发写入下的临时文件隔离。

背景：分片、会话元数据、合并成品的临时文件曾经只由目标名推导
（``3.part.tmp`` / ``meta.json.tmp`` / ``<task>.zip.tmp``）。滚动发布时新旧两个
Pod 共享同一个 RWO 卷，同名临时文件会让两个进程写同一个 inode，互相截断/覆盖，
最后被 ``os.replace`` 当作完整分片落盘——这是静默的数据损坏，不是可重试的报错。

这里锁两件事：
1. 每次写入拿到的临时路径都不同（机制）；
2. 并发写同一目标后，落盘内容必须是完整副本之一（行为）。
"""

import asyncio
import os

import pytest
from app.services.log_collect_storage import LocalLogCollectStorage

UPLOAD_ID = "Q2026103000001"


@pytest.fixture
def storage(tmp_path):
    return LocalLogCollectStorage(str(tmp_path / "log-collect"))


def test_temp_path_is_allocated_per_write(storage):
    """同一目标连续两次写入必须用不同临时文件（修复前两者完全相同）。"""

    target = storage.part_path(UPLOAD_ID, 1)
    first = storage._temp_for(target)
    second = storage._temp_for(target)

    assert first != second
    # 必须与目标同目录：跨卷时 os.replace 会抛 EXDEV，原子落盘就失效了。
    assert first.parent == target.parent
    assert first.name.startswith("1.part.")
    assert first.name.endswith(".tmp")
    assert str(os.getpid()) in first.name


async def test_concurrent_part_writes_keep_one_complete_copy(storage):
    """两个写者并发写同一分片：落盘只能是其中一份完整内容，不能是混合体。"""

    storage.write_meta(UPLOAD_ID, {"part_size_bytes": 4096})
    payload_a = b"A" * 4096
    payload_b = b"B" * 4096

    async def write(payload: bytes) -> tuple[int, str]:
        async def chunks():
            for start in range(0, len(payload), 256):
                yield payload[start : start + 256]
                # 主动让出，制造真实交错：共享临时文件时这里必然把对方写花。
                await asyncio.sleep(0.005)

        return await storage.write_part(
            upload_id=UPLOAD_ID,
            part_number=1,
            chunks=chunks(),
            max_bytes=8192,
        )

    results = await asyncio.gather(write(payload_a), write(payload_b))

    landed = storage.part_path(UPLOAD_ID, 1).read_bytes()
    assert landed in (payload_a, payload_b)
    assert [size for size, _ in results] == [4096, 4096]
    # 临时文件必须全部被 os.replace 收走，不留残骸。
    assert not list(storage._part_dir(UPLOAD_ID).glob("*.tmp"))


async def test_concurrent_merges_keep_one_complete_archive(storage):
    """两个进程并发合并同一任务：成品必须是完整压缩包。"""

    storage.write_meta(UPLOAD_ID, {"part_size_bytes": 2048})
    payload = bytes(range(256)) * 8

    async def chunks():
        for start in range(0, len(payload), 256):
            yield payload[start : start + 256]

    await storage.write_part(
        upload_id=UPLOAD_ID,
        part_number=1,
        chunks=chunks(),
        max_bytes=8192,
    )

    async def merge() -> tuple[int, str, str]:
        return await storage.merge(
            upload_id=UPLOAD_ID,
            part_numbers=[1],
            case_id="C1",
            task_id="t1",
            max_bytes=8192,
        )

    await asyncio.gather(merge(), merge())

    archive = storage.object_path("C1", "t1").read_bytes()
    assert archive == payload
    assert not list(storage.objects_root.rglob("*.tmp"))
