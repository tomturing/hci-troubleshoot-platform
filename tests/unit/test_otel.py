"""
otel 模块单元测试

覆盖 _parse_grpc_endpoint, get_current_trace_id, get_current_span_id, get_current_traceparent
"""

from unittest.mock import MagicMock, patch

from backend.shared.observability.otel import (
    _parse_grpc_endpoint,
    get_current_span_id,
    get_current_trace_id,
    get_current_traceparent,
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


class TestGetCurrentTraceparent:
    """get_current_traceparent 测试"""

    def test_traceparent_and_trace_id_share_same_context(self):
        """traceparent 必须携带当前 Span 的同一 trace_id/span_id 和完整 flags"""
        with patch("backend.shared.observability.otel.trace") as mock_trace:
            mock_ctx = MagicMock()
            mock_ctx.trace_id = int("caa7e3e825ba4a606df189740be1118c", 16)
            mock_ctx.span_id = int("cbef2f8fb7e2d3a8", 16)
            mock_ctx.trace_flags = 0x03
            mock_span = MagicMock()
            mock_span.get_span_context.return_value = mock_ctx
            mock_trace.get_current_span.return_value = mock_span

            assert get_current_trace_id() == "caa7e3e825ba4a606df189740be1118c"
            assert get_current_traceparent() == (
                "00-caa7e3e825ba4a606df189740be1118c-cbef2f8fb7e2d3a8-03"
            )

    def test_no_trace_context_returns_empty_string(self):
        """无有效父上下文时不生成伪造 traceparent"""
        with patch("backend.shared.observability.otel.trace") as mock_trace:
            mock_span = MagicMock()
            mock_span.get_span_context.return_value = None
            mock_trace.get_current_span.return_value = mock_span

            assert get_current_traceparent() == ""
