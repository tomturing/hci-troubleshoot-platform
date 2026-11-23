"""异步 Job 任务登记：让 kb-service 在被替换前把手上的活干完。

第一性原理
----------
vision/signal job 单次 30~60 秒，而 k8s 替换 Pod 时只给 grace 窗口。之前这里的
job 任务由 ``asyncio.create_task`` 裸启动、没有句柄：
  - 收到 SIGTERM 后 uvicorn 只等 HTTP 请求结束，不等这些后台任务；
  - 进程一退出，job 直接消失，轮询方拿到 404（前端只能报错）。

这里不引入 Redis 持久化：这些任务本身是幂等可重跑的短任务，为它加一套跨进程
状态存储属于成本大于收益。真正要做的是"别在它们跑到一半时把进程杀掉"——
登记任务句柄，退出时 join（有界），跑不完才谈放弃。
"""

from __future__ import annotations

import asyncio
from typing import Any, Coroutine

from shared.observability.logger import get_logger

logger = get_logger("kb-service-job-registry")


def track_job_task(holder: Any, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any] | None:
    """登记一个 job 后台任务；holder 需持有 ``_job_tasks`` 集合。"""
    try:
        task = asyncio.create_task(coro, name=name)
    except RuntimeError as exc:  # 事件循环已关闭
        coro.close()
        logger.error(event="job_task_rejected", task_name=name, error=str(exc))
        return None
    holder._job_tasks.add(task)  # noqa: SLF001 —— 约定由 holder 自己持有集合
    task.add_done_callback(holder._job_tasks.discard)  # noqa: SLF001
    return task


async def drain_job_tasks(holder: Any, timeout: float, *, label: str) -> tuple[int, float]:
    """等待在跑 job 完成；超时取消并留痕。

    返回值里 abandoned 非零说明 grace 不够或任务没有安全点——这是发布真的丢活的信号。
    """
    pending = set(holder._job_tasks)  # noqa: SLF001
    if not pending:
        return 0, 0.0
    started = asyncio.get_running_loop().time()
    _, still_pending = await asyncio.wait(pending, timeout=timeout)
    for task in still_pending:
        task.cancel()
    if still_pending:
        await asyncio.gather(*still_pending, return_exceptions=True)
    elapsed = asyncio.get_running_loop().time() - started
    logger.info(
        event="job_tasks_drained",
        message=f"{label} 在跑 job 收尾完成",
        inflight=len(pending),
        abandoned=len(still_pending),
        elapsed_seconds=round(elapsed, 3),
        timeout_seconds=timeout,
    )
    return len(still_pending), elapsed