"""进程退出协调器：滚动发布时把在途工作交给新副本，而不是被 SIGKILL 砍断。

第一性原理
----------
Pod 一定会被替换（发布、驱逐、节点漂移都会发生），SIGKILL 之后进程内的一切都会消失。
所以「发布不影响正在跑的任务」不可能靠"调大 grace"这一个动作达成，它只能靠三件事：

1. **内容在内存里时就落库**——AI 回复的落库原本挂在 Starlette BackgroundTasks 上，
   响应结束后才执行；若那一刻正好 SIGKILL，用户看到回复、库里却没有（现场出现过）。
   本模块把落库任务登记成可等待的任务，退出时先等它们完成。
2. **主动收盘而不是等被砍**——收到 SIGTERM 后 SSE 生成器在下一帧发出
   ``event: server_restart`` 并结束；前端据此立刻向新副本 resume-stream，
   用户感知是一次极短的停顿，而不是"卡住直到超时再报错"。
3. **可观测**——开始退出的在途数量、收尾耗时、是否被迫放弃，都必须落事件。
   否则"发布到底影响了哪些会话"永远只能靠猜。

注意这里刻意不追求"零中断"：长连接在进程替换时必然断开，追求零中断只会写出
一个看起来优雅、实际把内容丢在内存里的实现。
"""

from __future__ import annotations

import asyncio
import signal
import time
from collections.abc import Coroutine
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger("conversation-service")

# 收尾上限：必须明显小于 k8s 的 terminationGracePeriodSeconds(300)，
# 给 uvicorn 自己关闭连接、以及 preStop 已经消耗的时间留出余量。
DEFAULT_DRAIN_TIMEOUT_SECONDS = 30.0


class ShutdownCoordinator:
    """登记在途后台任务，并在退出时等待它们到安全点。"""

    def __init__(self) -> None:
        self._shutting_down = asyncio.Event()
        self._tasks: set[asyncio.Task[Any]] = set()
        self._began_at: float = 0.0

    @property
    def is_shutting_down(self) -> bool:
        """SSE 生成器用它决定"该收盘了"，比等待客户端超时早得多。"""
        return self._shutting_down.is_set()

    def begin_shutdown(self) -> int:
        """标记进入退出流程，返回当前在途任务数（用于事件）。幂等。

        信号处理和 lifespan shutdown 都会调用它，重复调用不能重置开始时间——
        否则算出来的收尾耗时会失真。
        """
        if not self._shutting_down.is_set():
            self._began_at = time.monotonic()
            self._shutting_down.set()
        return len(self._tasks)

    def track(self, coro: Coroutine[Any, Any, Any], *, name: str) -> asyncio.Task[Any] | None:
        """登记一个必须在进程退出前完成的后台任务。

        与 BackgroundTasks 的差别：BackgroundTasks 由 Starlette 在响应结束后执行，
        本进程没有句柄、退出时也无法等待；这里拿到 Task 句柄，退出时能 join。

        事件循环已关闭时返回 None（此时任何后台落库都救不回来了，只留痕）：
        宁可少一个后台任务，也不要在退出路径上抛异常盖掉真正的原因。
        """
        try:
            task = asyncio.create_task(coro, name=name)
        except RuntimeError as exc:  # 事件循环已关闭
            coro.close()
            logger.error(
                event="shutdown_track_rejected",
                message="事件循环已关闭，后台任务无法登记（内容可能未落库）",
                task_name=name,
                error=str(exc),
            )
            return None
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    async def drain(self, timeout: float = DEFAULT_DRAIN_TIMEOUT_SECONDS) -> tuple[int, float]:
        """等待在途任务完成；超时的强制取消。

        返回 (被迫放弃的任务数, 实际耗时秒)。放弃数是关键指标：它非零就说明
        grace 不够或某个任务本身没有安全点，发布正在真的丢东西。
        """
        pending = set(self._tasks)
        if not pending:
            # 没有在途任务也必须落事件：它是"进程确实走到收尾"的唯一证据。
            # 否则发布演练只看到 shutdown_signal_received 却看不到 shutdown_drained，
            # 无法区分"无需收尾"与"被 SIGKILL 前根本没轮到 lifespan"。
            logger.info(
                event="shutdown_drained",
                message="在途后台任务收尾完成",
                inflight_at_begin=0,
                abandoned=0,
                elapsed_seconds=0.0,
                timeout_seconds=timeout,
            )
            return 0, 0.0
        started = time.monotonic()
        _, still_pending = await asyncio.wait(pending, timeout=timeout)
        for task in still_pending:
            task.cancel()
        # 给被取消的任务一个交出控制权的机会，避免 "Task was destroyed but it is pending"。
        if still_pending:
            await asyncio.gather(*still_pending, return_exceptions=True)
        elapsed = time.monotonic() - started
        logger.info(
            event="shutdown_drained",
            message="在途后台任务收尾完成",
            inflight_at_begin=len(pending),
            abandoned=len(still_pending),
            elapsed_seconds=round(elapsed, 3),
            timeout_seconds=timeout,
        )
        return len(still_pending), elapsed


def install_signal_handlers(coordinator: ShutdownCoordinator) -> bool:
    """把退出信号处理挂到 uvicorn 之上，让在途 SSE 立刻开始收盘。

    为什么不能只在 lifespan shutdown 里置位
    ----------------------------------------
    uvicorn 收到 SIGTERM 的顺序是：停止接新连接 → **等待现有连接自然结束**
    → 才跑 lifespan shutdown。长连接必须自己先结束才轮得到 lifespan，而
    "要不要结束"又依赖退出标志——等 lifespan 里再置位，SSE 生成器永远看不到，
    整套收盘逻辑就是死代码。

    实测（uvicorn 0.27 + Linux）：不装本函数时，一个 8s 的 SSE 流会一路跑完才轮到
    lifespan shutdown；装了之后同一场景在 0.6s 内就发出 server_restart 并收盘。

    实现说明
    --------
    uvicorn 用 ``loop.add_signal_handler`` 注册，``signal.getsignal`` 拿到的是 asyncio
    的 noop 占位函数。覆盖后**仍要调用它**：若运行环境走 ``signal.signal`` 分支，prev
    就是 uvicorn 的 handle_exit，必须显式转发；走 add_signal_handler 分支时 asyncio 靠
    wakeup fd 自行回调，调用 noop 也无害（两条路径都覆盖）。

    返回是否安装成功；失败不抛异常——收盘能力少一层，但绝不能因此让进程起不来。
    """
    installed = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)

            def _handler(signum: int, frame: Any, _prev: Any = prev) -> None:
                inflight = coordinator.begin_shutdown()
                logger.info(
                    event="shutdown_signal_received",
                    message="收到退出信号，在途 SSE 将在下一帧收盘",
                    signal_name=signal.Signals(signum).name,
                    inflight_tasks=inflight,
                )
                # 把控制权交回 uvicorn，进程才会真正退出（否则只置了个标志）。
                if callable(_prev):
                    _prev(signum, frame)

            signal.signal(sig, _handler)
            installed = True
        except (ValueError, OSError) as exc:  # 非主线程或平台不支持
            logger.warning(
                event="shutdown_signal_install_failed",
                message="无法拦截退出信号，只能依赖 grace 等在途流自然结束",
                signal_name=getattr(sig, "name", str(sig)),
                error=str(exc),
            )
    return installed
