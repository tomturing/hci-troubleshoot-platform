"""
AIStreamError 异常类单元测试

测试覆盖：
- AIStreamError 创建和 str() 输出
- to_sse_data() 返回正确 JSON
- sanitize_error_message() URL 清理
- Exception.args 被正确设置
"""

import json

import pytest
from shared.utils.exceptions import AIStreamError, ErrorCode, sanitize_error_message


class TestAIStreamError:
    """AIStreamError 异常类测试"""

    def test_create_with_all_fields(self):
        """测试创建包含所有字段的异常"""
        error = AIStreamError(
            code=ErrorCode.AI_UPSTREAM_ERROR,
            message="AI 服务暂时不可用",
            detail="status 502",
        )

        assert error.code == ErrorCode.AI_UPSTREAM_ERROR
        assert error.message == "AI 服务暂时不可用"
        assert error.detail == "status 502"

    def test_create_with_minimal_fields(self):
        """测试创建仅包含必需字段的异常"""
        error = AIStreamError(
            code=ErrorCode.INTERNAL_ERROR,
            message="内部错误",
        )

        assert error.code == ErrorCode.INTERNAL_ERROR
        assert error.message == "内部错误"
        assert error.detail == ""

    def test_str_returns_sse_data(self):
        """测试 __str__ 返回 to_sse_data() 的结果"""
        error = AIStreamError(
            code=ErrorCode.AI_TIMEOUT,
            message="AI 服务超时",
            detail="timeout after 30s",
        )

        assert str(error) == error.to_sse_data()

    def test_to_sse_data_returns_valid_json(self):
        """测试 to_sse_data() 返回有效 JSON"""
        error = AIStreamError(
            code=ErrorCode.AI_RATE_LIMITED,
            message="请求过于频繁",
            detail="rate limited",
        )

        json_str = error.to_sse_data()
        data = json.loads(json_str)

        assert data["code"] == "AI_RATE_LIMITED"
        assert data["message"] == "请求过于频繁"
        assert data["detail"] == "rate limited"

    def test_to_sse_data_handles_special_characters(self):
        """测试 to_sse_data() 正确处理特殊字符"""
        error = AIStreamError(
            code=ErrorCode.INTERNAL_ERROR,
            message='错误: "测试"',
            detail="包含特殊字符 <>&",
        )

        json_str = error.to_sse_data()
        # 确保是有效的 JSON
        data = json.loads(json_str)

        assert data["message"] == '错误: "测试"'
        assert data["detail"] == "包含特殊字符 <>&"

    def test_newlines_removed_from_message(self):
        """测试换行符从消息中被移除"""
        error = AIStreamError(
            code=ErrorCode.INTERNAL_ERROR,
            message="第一行\n第二行\r第三行",
            detail="详情\n换行",
        )

        assert "\n" not in error.message
        assert "\r" not in error.message
        assert "\n" not in error.detail
        assert "\r" not in error.detail

    def test_exception_args_is_set(self):
        """测试 Exception.args 被正确设置"""
        error = AIStreamError(
            code=ErrorCode.INTERNAL_ERROR,
            message="测试错误消息",
        )

        # 确保父类 Exception 的 args 被正确设置
        assert len(error.args) == 1
        assert error.args[0] == "测试错误消息"

    def test_raise_and_catch(self):
        """测试异常可以被正常抛出和捕获"""
        with pytest.raises(AIStreamError) as exc_info:
            raise AIStreamError(
                code=ErrorCode.AI_UNAVAILABLE,
                message="AI 服务不可用",
                detail="connection refused",
            )

        error = exc_info.value
        assert error.code == ErrorCode.AI_UNAVAILABLE
        assert "AI 服务不可用" in str(error)


class TestSanitizeErrorMessage:
    """sanitize_error_message 函数测试"""

    def test_removes_http_url(self):
        """测试移除 http URL"""

        class MockError(Exception):
            pass

        error = MockError("连接 http://internal-server:8080/api 失败")
        message, detail = sanitize_error_message(error)

        assert "http://internal-server:8080/api" not in message
        assert "[URL]" in message
        assert "MockError" in detail

    def test_removes_https_url(self):
        """测试移除 https URL"""

        class MockError(Exception):
            pass

        error = MockError("请求 https://api.example.com/v1/chat 失败")
        message, detail = sanitize_error_message(error)

        assert "https://api.example.com/v1/chat" not in message
        assert "[URL]" in message

    def test_preserves_non_url_content(self):
        """测试保留非 URL 内容"""

        class MockError(Exception):
            pass

        error = MockError("超时错误: 等待响应超过 30 秒")
        message, detail = sanitize_error_message(error)

        assert "超时错误" in message
        assert "30 秒" in message

    def test_returns_error_type_in_detail(self):
        """测试返回错误类型在详情中"""

        class CustomTimeoutError(Exception):
            pass

        error = CustomTimeoutError("超时")
        _, detail = sanitize_error_message(error)

        assert "CustomTimeoutError" in detail
