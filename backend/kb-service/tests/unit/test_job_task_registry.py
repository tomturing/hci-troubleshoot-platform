"""kb-service job 任务登记与收尾单测。

背景：vision/signal job 由裸 ``asyncio.create_task`` 启动，没有句柄。uvicorn 退出只等
HTTP 请求，不等这些后台任务，于是 Pod 一换 job 就消失、轮询方 404。这里锁定：
登记后的任务会被 drain 等到；跑不完的会被取消并计入 abandoned（发布真的丢活的信号）。
"""

import asyncio

import pytest
from app.services.job_task_registry import drain_job_tasks, track_job_task


class _Holder:
    """与 VisionJobManager / SignalJobManager 相同的最小约定。"""

    def __init__(self) -> None:
        self._job_tasks: set[asyncio.Task] = set()


@pytest.mark.asyncio
async def test_tracked_job_is_waited_by_drain():
    holder = _Holder()
    done = False

    async def _job() -> None:
        nonlocal done
        await asyncio.sleep(0.05)
        done = True

    task = track_job_task(holder, _job(), name="vision_job:1")
    assert task is not None

    abandoned, elapsed = await drain_job_tasks(holder, timeout=5.0, label="vision")

    assert done is True
    assert abandoned == 0
    assert elapsed >= 0


@pytest.mark.asyncio
async def test_unfinished_job_is_cancelled_and_reported():
    holder = _Holder()

    async def _job() -> None:
        await asyncio.sleep(60)

    task = track_job_task(holder, _job(), name="signal_job:2")
    abandoned, _ = await drain_job_tasks(holder, timeout=0.1, label="signal")

    assert abandoned == 1
    assert task is not None and task.cancelled() is True


@pytest.mark.asyncio
async def test_drain_without_tracked_jobs_still_logs_completion(monkeypatch):
    """空收尾必须落 job_tasks_drained，否则发布演练无法确认收尾真的执行过。"""
    from app.services import job_task_registry as module

    events: list[str] = []

    class _Logger:
        def info(self, *, event: str, **_kwargs) -> None:
            events.append(event)

    monkeypatch.setattr(module, "logger", _Logger())
    holder = _Holder()

    assert await drain_job_tasks(holder, timeout=1.0, label="vision") == (0, 0.0)
    assert events == ["job_tasks_drained"]


@pytest.mark.asyncio
async def test_finished_task_leaves_the_set():
    """done_callback 必须把自己从集合里摘掉，否则 drain 会把已结束任务当在途统计。"""
    holder = _Holder()

    async def _job() -> None:
        return None

    track_job_task(holder, _job(), name="vision_job:3")
    await asyncio.sleep(0.05)

    assert holder._job_tasks == set()
