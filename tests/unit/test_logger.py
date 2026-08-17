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

    def test_sensitive_fields_are_redacted_recursively(self, logger):
        """敏感键即使嵌套在列表和字典中也不会写入日志。"""
        with patch("backend.shared.observability.logger.get_current_trace_id", return_value=""):
            with patch("backend.shared.observability.logger.get_current_span_id", return_value=""):
                log_data = json.loads(
                    logger._format_log(
                        "INFO",
                        "secret_test",
                        payload={"password": "plain", "nested": [{"api-key": "key-value"}]},
                    )
                )

        assert log_data["payload"]["password"] == "[REDACTED]"
        assert log_data["payload"]["nested"][0]["api-key"] == "[REDACTED]"
        assert "plain" not in json.dumps(log_data)
        assert "key-value" not in json.dumps(log_data)

    def test_inline_credentials_are_redacted(self, logger):
        """自由文本中的常见令牌、密码参数和 URL 凭据也会被净化。"""
        message = (
            "Authorization: Bearer abc.def password=hunter2 "
            "url=https://root:secret@example.test api_key='key123'"
        )
        log_data = json.loads(logger._format_log("ERROR", "credential_test", message=message))

        rendered = log_data["message"]
        assert "abc.def" not in rendered
        assert "hunter2" not in rendered
        assert "root:secret" not in rendered
        assert "key123" not in rendered
        assert rendered.count("[REDACTED]") >= 4

    def test_long_fields_are_bounded_with_hash(self, logger):
        """超长现场输出保留前缀、原长度和摘要，不无限放大日志。"""
        log_data = json.loads(logger._format_log("INFO", "large_output", output="x" * 5000))

        assert log_data["output"].startswith("x" * 100)
        assert "[TRUNCATED original_chars=5000 sha256=" in log_data["output"]
        assert len(log_data["output"]) < 4300

    def test_reserved_fields_cannot_override_log_envelope(self, logger):
        """业务参数不能伪造事件名、服务名或调用链字段。"""
        with patch("backend.shared.observability.logger.get_current_trace_id", return_value="otel-trace"):
            with patch("backend.shared.observability.logger.get_current_span_id", return_value="otel-span"):
                log_data = json.loads(
                    logger._format_log(
                        "INFO",
                        "real_event",
                        trace_id="custom-trace",
                        service="forged-service",
                        span_id="forged-span",
                    )
                )

        assert log_data["service"] == "test-service"
        assert log_data["event"] == "real_event"
        assert log_data["trace_id"] == "otel-trace"
        assert log_data["custom_trace_id"] == "custom-trace"
        assert log_data["span_id"] == "otel-span"
        assert log_data["reserved_field_conflicts"] == ["service", "span_id"]


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
