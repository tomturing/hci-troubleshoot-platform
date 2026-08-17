"""
Structured Logging Utilities
结构化日志工具
"""

import hashlib
import json
import logging
import re
import sys
import traceback
from datetime import UTC, datetime
from typing import Any

from .otel import get_current_span_id, get_current_trace_id

_RESERVED_FIELDS = {
    "log_schema_version",
    "timestamp",
    "level",
    "service",
    "event",
    "message",
    "trace_id",
    "custom_trace_id",
    "span_id",
}
_SENSITIVE_KEYS = {
    "authorization",
    "api_key",
    "apikey",
    "access_token",
    "refresh_token",
    "internal_api_token",
    "password",
    "passwd",
    "secret",
    "secret_key",
    "credential",
    "credentials",
    "cookie",
    "set_cookie",
}
_DEFAULT_STRING_LIMIT = 4096
_TRACEBACK_STRING_LIMIT = 16000
_MAX_SANITIZE_DEPTH = 8
_INLINE_SECRET_PATTERNS = (
    re.compile(r"(?i)\bBearer\s+[^\s,;]+"),
    re.compile(
        r"(?i)([\"']?(?:authorization|api[_-]?key|access[_-]?token|refresh[_-]?token|"
        r"internal[_-]?api[_-]?token|password|passwd|secret(?:[_-]?key)?|credential|cookie)"
        r"[\"']?\s*[:=]\s*[\"']?)([^\"'\s,;}\]]+)"
    ),
    re.compile(r"(?i)(://[^:/\s]+:)([^@\s]+)(@)"),
)


def _is_sensitive_key(key: str) -> bool:
    normalized = key.casefold().replace("-", "_")
    return normalized in _SENSITIVE_KEYS or normalized.endswith(
        ("_password", "_passwd", "_secret", "_secret_key", "_api_key", "_access_token", "_refresh_token")
    )


def _sanitize_string(value: str, *, limit: int) -> str:
    sanitized = _INLINE_SECRET_PATTERNS[0].sub("Bearer [REDACTED]", value)
    sanitized = _INLINE_SECRET_PATTERNS[1].sub(r"\1[REDACTED]", sanitized)
    sanitized = _INLINE_SECRET_PATTERNS[2].sub(r"\1[REDACTED]\3", sanitized)
    if len(sanitized) <= limit:
        return sanitized
    digest = hashlib.sha256(sanitized.encode("utf-8", errors="replace")).hexdigest()
    return f"{sanitized[:limit]}...[TRUNCATED original_chars={len(sanitized)} sha256={digest}]"


def _sanitize_log_value(key: str, value: Any, *, depth: int = 0) -> Any:
    """递归净化日志字段，日志安全失败时也不得泄露原始对象。"""

    if _is_sensitive_key(key):
        return "[REDACTED]"
    if depth >= _MAX_SANITIZE_DEPTH:
        return "[MAX_DEPTH]"
    if isinstance(value, dict):
        return {
            str(item_key): _sanitize_log_value(str(item_key), item_value, depth=depth + 1)
            for item_key, item_value in value.items()
        }
    if isinstance(value, (list, tuple, set, frozenset)):
        return [_sanitize_log_value(key, item, depth=depth + 1) for item in value]
    if isinstance(value, str):
        limit = _TRACEBACK_STRING_LIMIT if key == "traceback" else _DEFAULT_STRING_LIMIT
        return _sanitize_string(value, limit=limit)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return _sanitize_string(str(value), limit=_DEFAULT_STRING_LIMIT)


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

    @staticmethod
    def _coerce_call(
        event: str,
        args: tuple[object, ...],
        message: object | None,
        trace_id: object | None,
    ) -> tuple[str, str | None, str | None]:
        """兼容标准 logging 的 ``logger.info('x=%s', value)`` 调用。

        业务日志应使用关键字字段，但历史代码中仍有标准 logging 风格调用。
        如果不在这里归一，参数会错位写入 ``message``/``trace_id``，甚至让
        日志调用本身抛出 TypeError，掩盖原始业务异常。
        """

        if not args:
            return event, str(message) if message is not None else None, str(trace_id) if trace_id else None
        if "%" in event:
            try:
                rendered = event % args
            except (TypeError, ValueError):
                rendered = " ".join([event, *(str(item) for item in args)])
            return "legacy_log", rendered, str(trace_id) if trace_id else None
        values = list(args)
        rendered_message = message if message is not None else values.pop(0)
        rendered_trace = trace_id if trace_id is not None and not values else (values.pop(0) if values else trace_id)
        return event, str(rendered_message) if rendered_message is not None else None, str(rendered_trace) if rendered_trace else None

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
            "log_schema_version": 1,
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

        # 日志信封字段只能由 logger 生成，业务字段不得覆盖事件、服务或调用链。
        conflicts = sorted(_RESERVED_FIELDS.intersection(kwargs))
        for key, value in kwargs.items():
            if key not in _RESERVED_FIELDS:
                log_data[key] = value
        if conflicts:
            log_data["reserved_field_conflicts"] = conflicts

        sanitized = {
            key: _sanitize_log_value(key, value)
            for key, value in log_data.items()
        }
        return json.dumps(sanitized, ensure_ascii=False, default=str)

    def info(self, event: str, *args, message: str | None = None, trace_id: str | None = None, **kwargs):
        """记录 INFO 级别日志"""
        event, message, trace_id = self._coerce_call(event, args, message, trace_id)
        log_str = self._format_log("INFO", event, message, trace_id, **kwargs)
        self.logger.info(log_str)

    def warning(self, event: str, *args, message: str | None = None, trace_id: str | None = None, **kwargs):
        """记录 WARNING 级别日志"""
        event, message, trace_id = self._coerce_call(event, args, message, trace_id)
        log_str = self._format_log("WARNING", event, message, trace_id, **kwargs)
        self.logger.warning(log_str)

    def error(
        self,
        event: str,
        *args,
        message: str | None = None,
        trace_id: str | None = None,
        error: Exception | None = None,
        **kwargs,
    ):
        """记录 ERROR 级别日志"""
        event, message, trace_id = self._coerce_call(event, args, message, trace_id)
        if error:
            kwargs["error_type"] = type(error).__name__
            kwargs["error_message"] = str(error)

        log_str = self._format_log("ERROR", event, message, trace_id, **kwargs)
        self.logger.error(log_str)

    def debug(self, event: str, *args, message: str | None = None, trace_id: str | None = None, **kwargs):
        """记录 DEBUG 级别日志"""
        event, message, trace_id = self._coerce_call(event, args, message, trace_id)
        log_str = self._format_log("DEBUG", event, message, trace_id, **kwargs)
        self.logger.debug(log_str)

    def exception(
        self,
        event: str,
        *args,
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

        event, message, trace_id = self._coerce_call(event, args, message, trace_id)
        if error:
            kwargs["error_type"] = type(error).__name__
            kwargs["error_message"] = str(error)

        # 获取当前活跃的异常堆栈（在 except 块内有效）
        tb = traceback.format_exc()
        if tb and tb.strip() != "NoneType: None":
            kwargs["traceback"] = tb

        log_str = self._format_log("ERROR", event, message, trace_id, **kwargs)
        self.logger.error(log_str)

    def critical(
        self,
        event: str,
        *args,
        message: str | None = None,
        trace_id: str | None = None,
        error: Exception | None = None,
        **kwargs,
    ):
        """记录 CRITICAL 级别日志，并保证日志失败不会替换原始故障。"""

        event, message, trace_id = self._coerce_call(event, args, message, trace_id)
        if error:
            kwargs["error_type"] = type(error).__name__
            kwargs["error_message"] = str(error)
            kwargs.setdefault("traceback", traceback.format_exc())
        log_str = self._format_log("CRITICAL", event, message, trace_id, **kwargs)
        self.logger.critical(log_str)


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
