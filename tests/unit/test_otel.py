"""
otel 模块单元测试

覆盖 _parse_grpc_endpoint, get_current_trace_id, get_current_span_id
"""

from unittest.mock import MagicMock, patch

from backend.shared.observability.otel import (
    _parse_grpc_endpoint,
    get_current_span_id,
    get_current_trace_id,
)


class TestParseGrpcEndpoint:
    """_parse_grpc_endpoint 测试"""

    def test_http_scheme(self):
        """http scheme 返回 insecure=True"""
        endpoint, insecure = _parse_grpc_endpoint("http://tempo:4317")
        assert endpoint == "tempo:4317"
        assert insecure is True

    def test_https_scheme(self):
        """https scheme 返回 insecure=False"""
        endpoint, insecure = _parse_grpc_endpoint("https://tempo:4317")
        assert endpoint == "tempo:4317"
        assert insecure is False

    def test_no_scheme(self):
        """无 scheme 默认 insecure=True"""
        endpoint, insecure = _parse_grpc_endpoint("tempo:4317")
        assert endpoint == "tempo:4317"
        assert insecure is True


class TestGetCurrentTraceId:
    """get_current_trace_id 测试"""

    def test_no_trace_context(self):
        """无 trace context 返回空串"""
        with patch("backend.shared.observability.otel.trace") as mock_trace:
            mock_span = MagicMock()
            mock_span.get_span_context.return_value = None
            mock_trace.get_current_span.return_value = mock_span

            result = get_current_trace_id()
            assert result == ""

    def test_valid_trace_id(self):
        """有 trace context 返回格式化的 trace_id"""
        with patch("backend.shared.observability.otel.trace") as mock_trace:
            mock_ctx = MagicMock()
            mock_ctx.trace_id = 123456789
            mock_span = MagicMock()
            mock_span.get_span_context.return_value = mock_ctx
            mock_trace.get_current_span.return_value = mock_span

            result = get_current_trace_id()
            assert result == format(123456789, "032x")


class TestGetCurrentSpanId:
    """get_current_span_id 测试"""

    def test_no_span_context(self):
        """无 span context 返回空串"""
        with patch("backend.shared.observability.otel.trace") as mock_trace:
            mock_span = MagicMock()
            mock_span.get_span_context.return_value = None
            mock_trace.get_current_span.return_value = mock_span

            result = get_current_span_id()
            assert result == ""

    def test_valid_span_id(self):
        """有 span context 返回格式化的 span_id"""
        with patch("backend.shared.observability.otel.trace") as mock_trace:
            mock_ctx = MagicMock()
            mock_ctx.span_id = 123456789
            mock_span = MagicMock()
            mock_span.get_span_context.return_value = mock_ctx
            mock_trace.get_current_span.return_value = mock_span

            result = get_current_span_id()
            assert result == format(123456789, "016x")
