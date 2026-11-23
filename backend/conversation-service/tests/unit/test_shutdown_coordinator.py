"""ShutdownCoordinator 退出语义单测。

覆盖三个真实故障点：
1. ``begin_shutdown`` 必须幂等——信号处理和 lifespan shutdown 都会调它，
   若第二次调用重置开始时间，收尾耗时会算错（看起来很快，实际被砍）。
2. ``drain`` 必须等到登记的后台任务真正结束，否则「用户看到回复、库里没有」。
3. ``drain`` 超时必须放弃并取消，且把放弃数返回出来——这是判断 grace 够不够的唯一指标。
"""

import asyncio

import pytest
from app.services.shutdown_coordinator import ShutdownCoordinator


@pytest.mark.asyncio
async def test_begin_shutdown_is_idempotent():
    coordinator = ShutdownCoordinator()
    assert coordinator.is_shutting_down is False

    coordinator.begin_shutdown()
    began_at = coordinator._began_at

    await asyncio.sleep(0.01)
    coordinator.begin_shutdown()

    assert coordinator.is_shutting_down is True
    assert coordinator._began_at == began_at, "重复调用不得重置开始时间，否则收尾耗时失真"


@pytest.mark.asyncio
async def test_drain_waits_for_tracked_task_to_finish():
    coordinator = ShutdownCoordinator()
    finished = False

    async def _persist() -> None:
        nonlocal finished
        await asyncio.sleep(0.05)
        finished = True

    coordinator.track(_persist(), name="test_persist")
    abandoned, elapsed = await coordinator.drain(timeout=5.0)

    assert finished is True
    assert abandoned == 0
    assert elapsed >= 0


@pytest.mark.asyncio
async def test_drain_abandons_task_that_has_no_safe_point():
    coordinator = ShutdownCoordinator()

    async def _stuck() -> None:
        await asyncio.sleep(60)

    task = coordinator.track(_stuck(), name="test_stuck")
    abandoned, _ = await coordinator.drain(timeout=0.1)

    assert abandoned == 1
    assert task is not None and task.cancelled() is True


@pytest.mark.asyncio
async def test_drain_without_tasks_still_logs_completion(monkeypatch):
    """空收尾必须落 shutdown_drained，否则发布演练无法确认收尾真的执行过。"""
    from app.services import shutdown_coordinator as module

    events: list[str] = []

    class _Logger:
        def info(self, *, event: str, **_kwargs) -> None:
            events.append(event)

    monkeypatch.setattr(module, "logger", _Logger())
    coordinator = ShutdownCoordinator()

    abandoned, elapsed = await coordinator.drain(timeout=1.0)

    assert (abandoned, elapsed) == (0, 0.0)
    assert events == ["shutdown_drained"]
