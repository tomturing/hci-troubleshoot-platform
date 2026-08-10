"""
data-pipeline KBD 可观测性：trace_id 贯穿 + 日志注入

对接目标
--------
kb-service 已启用 OpenTelemetry 的 FastAPIInstrumentor，会自动从入站请求的
W3C `traceparent` 头提取并沿用其 trace_id。因此本模块在调用 kb-service API
时注入 `traceparent` 头，即可让「离线脚本（data-pipeline） ↔ 在线服务
（kb-service）」的日志共享同一 trace_id，在 Grafana / Tempo 中串联排查
（例如本次 Vision 阶段失败，可凭 trace_id 同时捞到脚本侧轮询日志与服务端
LLM 调用失败日志）。

同时提供 TraceIdFilter，把当前 contextvar 中的 trace_id 注入每条日志记录，
使现有所有 `logger.info(...)` 文本日志也自动带上 trace_id（灰度可观测）。
"""
from __future__ import annotations

import contextvars
import logging
import uuid

# 一次 pipeline run 一个根 trace_id，贯穿全程。
# asyncio 单事件循环下 await 不切换 context，所有协程共享此 contextvar，
# 因此一次 CLI 调用内 set 一次即可贯穿 fetch/import/vision/classify 全阶段。
_TRACE_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kbd_trace_id", default=None
)
_RUN_ID: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "kbd_run_id", default=None
)


def new_trace_id() -> str:
    """生成符合 W3C 标准的 16 字节 hex trace_id（32 字符）。"""
    return uuid.uuid4().hex


def get_trace_id() -> str | None:
    return _TRACE_ID.get()


def set_trace_id(trace_id: str | None) -> None:
    """设置当前上下文的 trace_id（一次 run 调用一次即可贯穿全程）。"""
    _TRACE_ID.set(trace_id)


def get_run_id() -> str | None:
    """返回当前 CLI 运行编号。日志过滤器会把它注入每条记录。"""

    return _RUN_ID.get()


def set_run_id(run_id: str | None) -> None:
    """设置当前 CLI 运行编号（一次 run 调用一次即可贯穿全程）。"""

    _RUN_ID.set(run_id)


def traceparent(trace_id: str | None = None) -> dict[str, str]:
    """构造注入到 kb-service 请求头的 W3C traceparent 字典。

    kb-service 的 FastAPIInstrumentor 会自动提取该头并沿用同一 trace_id，
    使服务端日志与本端日志共享 trace_id，实现跨进程串联。
    """
    tid = trace_id or get_trace_id() or new_trace_id()
    span_id = uuid.uuid4().hex[:16]  # 本次出站调用的本地 span_id
    return {"traceparent": f"00-{tid}-{span_id}-01"}


class TraceIdFilter(logging.Filter):
    """将当前 contextvar 的 trace_id 注入每条日志记录（record.trace_id）。

    在 formatter 中可用 `%(trace_id)s` 引用；若未设置则为 '-'。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        tid = get_trace_id()
        record.trace_id = tid or "-"
        record.run_id = get_run_id() or "-"
        return True


def install_trace_logging() -> None:
    """为 root logger 及其所有 handler 追加 TraceIdFilter（幂等，重复调用安全）。"""
    root = logging.getLogger()

    if not any(isinstance(f, TraceIdFilter) for f in root.filters):
        root.addFilter(TraceIdFilter())

    for handler in root.handlers:
        if not any(isinstance(f, TraceIdFilter) for f in handler.filters):
            handler.addFilter(TraceIdFilter())
