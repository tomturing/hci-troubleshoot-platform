"""
TraceID 工具函数
统一使用 OpenTelemetry W3C 标准 Trace ID (128-bit hex)

本模块为 otel.py 中 get_current_trace_id() 的薄封装，
供业务代码在需要显式获取当前 trace_id 时使用。
"""

from .otel import get_current_span_id, get_current_trace_id


def get_trace_id() -> str:
    """
    获取当前请求的 OTel Trace ID (32 字符小写 hex)

    在 FastAPI 请求处理链中调用时，会自动返回由 FastAPIInstrumentor
    创建的 Span 上下文中的 trace_id。

    Returns:
        str: 32 字符 hex trace_id，若无活跃 Span 则返回空串
    """
    return get_current_trace_id()


def get_span_id() -> str:
    """
    获取当前请求的 OTel Span ID (16 字符小写 hex)

    Returns:
        str: 16 字符 hex span_id，若无活跃 Span 则返回空串
    """
    return get_current_span_id()
