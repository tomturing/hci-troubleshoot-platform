"""
Structured Logging Utilities
结构化日志工具
"""

import json
import logging
import sys
from datetime import UTC, datetime

from .otel import get_current_span_id, get_current_trace_id


class StructuredLogger:
    """结构化日志记录器"""

    def __init__(self, service_name: str, log_level: str = "INFO"):
        self.service_name = service_name
        self.logger = logging.getLogger(service_name)
        self.logger.setLevel(getattr(logging, log_level.upper()))

        # 防止重复添加 handler（多次调用 get_logger 时）
        if not self.logger.handlers:
            # 配置输出到 stdout
            handler = logging.StreamHandler(sys.stdout)
            handler.setFormatter(logging.Formatter("%(message)s"))
            self.logger.addHandler(handler)

        # 阻止向 root logger 传播，避免日志被 LoggingInstrumentor 的 handler 重复输出
        self.logger.propagate = False

    def _format_log(
        self, level: str, event: str, message: str | None = None, trace_id: str | None = None, **kwargs
    ) -> str:
        """
        格式化日志为 JSON

        Args:
            level: 日志级别
            event: 事件名称
            message: 日志消息
            trace_id: TraceID
            **kwargs: 额外的日志字段

        Returns:
            str: JSON 格式的日志
        """
        log_data = {
            "timestamp": datetime.now(UTC).isoformat().replace("+00:00", "Z"),
            "level": level,
            "service": self.service_name,
            "event": event,
        }

        if message:
            log_data["message"] = message

        # 添加 trace_id 和 span_id（优先使用 OTel 上下文）
        otel_trace_id = get_current_trace_id()
        otel_span_id = get_current_span_id()
        if otel_trace_id:
            log_data["trace_id"] = otel_trace_id
            # 若同时传入了自定义 trace_id（如 X-Trace-ID），保留为额外字段
            if trace_id and trace_id != otel_trace_id:
                log_data["custom_trace_id"] = trace_id
        elif trace_id:
            log_data["trace_id"] = trace_id
        if otel_span_id:
            log_data["span_id"] = otel_span_id

        # 添加额外字段
        log_data.update(kwargs)

        return json.dumps(log_data, ensure_ascii=False, default=str)

    def info(self, event: str, message: str | None = None, trace_id: str | None = None, **kwargs):
        """记录 INFO 级别日志"""
        log_str = self._format_log("INFO", event, message, trace_id, **kwargs)
        self.logger.info(log_str)

    def warning(self, event: str, message: str | None = None, trace_id: str | None = None, **kwargs):
        """记录 WARNING 级别日志"""
        log_str = self._format_log("WARNING", event, message, trace_id, **kwargs)
        self.logger.warning(log_str)

    def error(
        self,
        event: str,
        message: str | None = None,
        trace_id: str | None = None,
        error: Exception | None = None,
        **kwargs,
    ):
        """记录 ERROR 级别日志"""
        if error:
            kwargs["error_type"] = type(error).__name__
            kwargs["error_message"] = str(error)

        log_str = self._format_log("ERROR", event, message, trace_id, **kwargs)
        self.logger.error(log_str)

    def debug(self, event: str, message: str | None = None, trace_id: str | None = None, **kwargs):
        """记录 DEBUG 级别日志"""
        log_str = self._format_log("DEBUG", event, message, trace_id, **kwargs)
        self.logger.debug(log_str)

    def exception(
        self,
        event: str,
        message: str | None = None,
        trace_id: str | None = None,
        error: Exception | None = None,
        **kwargs,
    ):
        """记录 CRITICAL/ERROR 级别日志，并附带完整 Python traceback

        应在 except 块内调用，自动捕获当前的异常堆栈信息。
        比 error() 更适合记录需要完整调用栈的异常。
        """
        import traceback

        if error:
            kwargs["error_type"] = type(error).__name__
            kwargs["error_message"] = str(error)

        # 获取当前活跃的异常堆栈（在 except 块内有效）
        tb = traceback.format_exc()
        if tb and tb.strip() != "NoneType: None":
            kwargs["traceback"] = tb

        log_str = self._format_log("ERROR", event, message, trace_id, **kwargs)
        self.logger.error(log_str)


# 日志实例缓存，避免重复创建
_logger_cache: dict[str, "StructuredLogger"] = {}


def get_logger(service_name: str, log_level: str = "INFO") -> "StructuredLogger":
    """
    获取结构化日志记录器（带缓存）

    Args:
        service_name: 服务名称
        log_level: 日志级别

    Returns:
        StructuredLogger: 日志记录器实例
    """
    cache_key = f"{service_name}:{log_level}"
    if cache_key not in _logger_cache:
        _logger_cache[cache_key] = StructuredLogger(service_name, log_level)
    return _logger_cache[cache_key]


# 日志装饰器
def log_function_call(logger: "StructuredLogger"):
    """
    函数调用日志装饰器

    Usage:
        @log_function_call(logger)
        async def my_function(arg1, arg2):
            pass
    """
    from functools import wraps

    def decorator(func):
        @wraps(func)
        async def async_wrapper(*args, **kwargs):
            func_name = func.__name__
            start_time = datetime.now(UTC)

            logger.debug(
                event="function_start",
                message=f"Starting {func_name}",
                function=func_name,
                args_count=len(args),
                kwargs_count=len(kwargs),
            )

            try:
                result = await func(*args, **kwargs)
                duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

                logger.debug(
                    event="function_complete",
                    message=f"Completed {func_name}",
                    function=func_name,
                    duration_ms=duration_ms,
                    status="success",
                )

                return result
            except Exception as e:
                duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

                logger.error(
                    event="function_error",
                    message=f"Error in {func_name}",
                    function=func_name,
                    duration_ms=duration_ms,
                    error=e,
                    status="error",
                )
                raise

        @wraps(func)
        def sync_wrapper(*args, **kwargs):
            func_name = func.__name__
            start_time = datetime.now(UTC)

            logger.debug(event="function_start", message=f"Starting {func_name}", function=func_name)

            try:
                result = func(*args, **kwargs)
                duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

                logger.debug(
                    event="function_complete",
                    message=f"Completed {func_name}",
                    function=func_name,
                    duration_ms=duration_ms,
                    status="success",
                )

                return result
            except Exception as e:
                duration_ms = int((datetime.now(UTC) - start_time).total_seconds() * 1000)

                logger.error(
                    event="function_error",
                    message=f"Error in {func_name}",
                    function=func_name,
                    duration_ms=duration_ms,
                    error=e,
                    status="error",
                )
                raise

        # 检查是否是异步函数
        import inspect

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator
