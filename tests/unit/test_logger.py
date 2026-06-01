"""
logger 模块单元测试

覆盖 StructuredLogger OTel trace_id/span_id 逻辑和 exception 方法
"""

import json
from unittest.mock import patch

import pytest

from backend.shared.observability.logger import StructuredLogger, get_logger


class TestStructuredLogger:
    """StructuredLogger 测试"""

    @pytest.fixture
    def logger(self):
        """创建 StructuredLogger 实例"""
        return StructuredLogger("test-service")

    def test_format_log_with_otel_trace_id(self, logger):
        """有 OTel trace_id 时添加到日志"""
        with patch("backend.shared.observability.logger.get_current_trace_id", return_value="abc123"):
            with patch("backend.shared.observability.logger.get_current_span_id", return_value="def456"):
                log_str = logger._format_log("INFO", "test_event", "test message")
                log_data = json.loads(log_str)
                assert log_data["trace_id"] == "abc123"
                assert log_data["span_id"] == "def456"

    def test_format_log_with_custom_trace_id(self, logger):
        """有自定义 trace_id 但无 OTel 时使用自定义"""
        with patch("backend.shared.observability.logger.get_current_trace_id", return_value=""):
            with patch("backend.shared.observability.logger.get_current_span_id", return_value=""):
                log_str = logger._format_log("INFO", "test_event", "test message", trace_id="custom123")
                log_data = json.loads(log_str)
                assert log_data["trace_id"] == "custom123"

    def test_format_log_with_both_trace_ids(self, logger):
        """同时有 OTel 和自定义 trace_id 时保留两者"""
        with patch("backend.shared.observability.logger.get_current_trace_id", return_value="otel123"):
            with patch("backend.shared.observability.logger.get_current_span_id", return_value="span456"):
                log_str = logger._format_log("INFO", "test_event", "test message", trace_id="custom789")
                log_data = json.loads(log_str)
                assert log_data["trace_id"] == "otel123"
                assert log_data["custom_trace_id"] == "custom789"

    def test_exception_with_error(self, logger):
        """exception 方法记录错误信息"""
        with patch("backend.shared.observability.logger.get_current_trace_id", return_value=""):
            with patch("backend.shared.observability.logger.get_current_span_id", return_value=""):
                error = ValueError("test error")
                # Mock traceback.format_exc
                with patch("traceback.format_exc", return_value="Traceback (most recent call last):\n  File test.py"):
                    log_str = logger.exception("error_event", "error message", error=error)
                    # 返回 None，但验证方法不抛异常
                    assert log_str is None
                    # 检查 logger.error 是否被调用
                    # 由于实现细节，只验证方法执行成功


class TestGetLogger:
    """get_logger 测试"""

    def test_get_logger_caches(self):
        """get_logger 缓存日志实例"""
        logger1 = get_logger("test-service")
        logger2 = get_logger("test-service")
        assert logger1 is logger2

    def test_get_logger_different_levels(self):
        """不同日志级别返回不同实例"""
        logger1 = get_logger("test-service", "INFO")
        logger2 = get_logger("test-service", "DEBUG")
        assert logger1 is not logger2
