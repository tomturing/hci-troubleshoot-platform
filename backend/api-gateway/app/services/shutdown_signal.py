"""网关退出信号：让在途 SSE 主动收盘，而不是被 SIGKILL 直接切断。

第一性原理
----------
网关的 grace 是 60s（workloadDefaults.gracePeriods）。它只是原样透传上游 SSE 字节、
不解释事件，所以 conversation-service 主动发的 ``server_restart`` 能完整穿过；但**网关
自己被替换**时，在途流没有任何收盘机会：uvicorn 会一直等到连接自然结束，grace 到期后
SIGKILL 直接切断 TCP。前端此时拿不到任何事件，只能表现为"网络错误"，也不会重连。

所以网关是唯一"必须理解事件"的透传层——只在退出时向在途流补一条
``event: server_restart``，让前端去新副本重连。

为什么信号要自己拦
------------------
uvicorn 收到 SIGTERM 的顺序是：停止接新连接 → 等待现有连接自然结束 → 才跑 lifespan
shutdown。流必须自己先结束才轮得到 lifespan，而"要不要结束"又依赖这个标志——只在
lifespan shutdown 里置位就是死代码。实测（uvicorn 0.27 + Linux）确认了该顺序。
"""

from __future__ import annotations

import signal
from typing import Any

from shared.observability.logger import get_logger

logger = get_logger("api-gateway")

# 与前端 chat.ts 的常量保持一致语义：一条 SSE 帧，data 非 [DONE] 即可。
SERVER_RESTART_FRAME = b"event: server_restart\ndata: {}\n\n"

# 检查退出标志的轮询间隔。上游在思考时可能长时间无数据，不能等下一帧才响应退出；
# 1s 足够短（收尾不需要更快），也不会造成可观唤醒开销。
STOP_POLL_INTERVAL_SECONDS = 1.0


class ShutdownSignal:
    """进程级退出标志 + 收盘计数（仅用于观测）。"""

    def __init__(self) -> None:
        self._stopping = False
        self._closed_streams = 0

    @property
    def is_stopping(self) -> bool:
        """SSE 生成器用它决定"该给客户端发 server_restart 了"。"""
        return self._stopping

    def mark_stopping(self) -> None:
        self._stopping = True

    def note_stream_closed(self) -> int:
        self._closed_streams += 1
        return self._closed_streams


# 进程级单例：路由层直接 import 使用，避免在 app.state 上绕一圈。
shutdown_signal = ShutdownSignal()


def install_shutdown_signal() -> bool:
    """把退出信号处理挂到 uvicorn 之上。

    uvicorn 用 ``loop.add_signal_handler`` 注册，``signal.getsignal`` 拿到的是 asyncio 的
    noop 占位函数；覆盖后仍要调用它（走 ``signal.signal`` 分支时那才是 uvicorn 的
    handle_exit，必须显式转发）。

    返回是否安装成功。失败只记日志、不抛异常——少一层收盘能力，但绝不能因此起不来。
    """
    installed = False
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            prev = signal.getsignal(sig)

            def _handler(signum: int, frame: Any, _prev: Any = prev) -> None:
                shutdown_signal.mark_stopping()
                logger.info(
                    event="gateway_shutdown_signal",
                    message="收到退出信号，在途 SSE 将在下一帧收盘",
                    signal_name=signal.Signals(signum).name,
                )
                if callable(_prev):
                    _prev(signum, frame)

            signal.signal(sig, _handler)
            installed = True
        except (ValueError, OSError) as exc:  # 非主线程或平台不支持
            logger.warning(
                event="gateway_shutdown_signal_install_failed",
                message="无法拦截退出信号，发布时在途 SSE 会被直接切断",
                signal_name=getattr(sig, "name", str(sig)),
                error=str(exc),
            )
    return installed
