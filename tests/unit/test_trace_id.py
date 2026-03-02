"""
TraceID 工具单元测试

测试当前 OTel 封装的 get_trace_id() / get_span_id()
"""

import pytest
from shared.utils.trace_id import get_trace_id, get_span_id


class TestGetTraceId:
    """get_trace_id() 测试"""

    def test_returns_string(self):
        """测试返回值为字符串类型"""
        result = get_trace_id()
        assert isinstance(result, str)

    def test_returns_hex_string(self):
        """测试返回值为有效的十六进制字符串"""
        result = get_trace_id()
        # OTel trace_id 为 32 位 hex；无活跃 Span 时可能返回 "0" * 32 或空字符串
        if result and result != "0" * 32:
            assert all(c in "0123456789abcdef" for c in result)

    def test_returns_consistent_in_same_context(self):
        """测试同一上下文多次调用返回一致结果"""
        id1 = get_trace_id()
        id2 = get_trace_id()
        assert id1 == id2


class TestGetSpanId:
    """get_span_id() 测试"""

    def test_returns_string(self):
        """测试返回值为字符串类型"""
        result = get_span_id()
        assert isinstance(result, str)

    def test_returns_hex_or_empty(self):
        """测试返回值为十六进制或空（无活跃 Span 时）"""
        result = get_span_id()
        if result and result != "0" * 16:
            assert all(c in "0123456789abcdef" for c in result)
