"""
Unit tests for shared.governance.llm_governor
"""

from unittest.mock import AsyncMock

import httpx
import pytest
from shared.governance.llm_governor import (
    call_with_llm_governance,
    classify_error_type,
    extract_retry_after,
    is_retryable_llm_error,
)
from shared.utils.exceptions import AIStreamError, ErrorCode


class TestLLMGovernorErrorClassification:
    def test_timeout_exceptions_are_retryable(self):
        req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
        assert is_retryable_llm_error(httpx.ConnectTimeout("Connection timed out", request=req)) is True
        assert is_retryable_llm_error(httpx.ReadTimeout("Read timed out", request=req)) is True
        assert is_retryable_llm_error(httpx.WriteTimeout("Write timed out", request=req)) is True
        assert is_retryable_llm_error(httpx.PoolTimeout("Pool timed out", request=req)) is True

    def test_connect_and_protocol_errors_are_retryable(self):
        req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
        assert is_retryable_llm_error(httpx.ConnectError("Connection refused", request=req)) is True
        assert is_retryable_llm_error(httpx.RemoteProtocolError("Incomplete chunked read", request=req)) is True

    def test_http_status_codes(self):
        req = httpx.Request("POST", "https://api.example.com/v1/chat/completions")
        resp_429 = httpx.Response(429, request=req)
        resp_500 = httpx.Response(500, request=req)
        resp_502 = httpx.Response(502, request=req)
        resp_503 = httpx.Response(503, request=req)
        resp_401 = httpx.Response(401, request=req)
        resp_400 = httpx.Response(400, request=req)

        assert is_retryable_llm_error(httpx.HTTPStatusError("429 Rate Limit", request=req, response=resp_429)) is True
        assert is_retryable_llm_error(httpx.HTTPStatusError("500 Error", request=req, response=resp_500)) is True
        assert is_retryable_llm_error(httpx.HTTPStatusError("502 Bad Gateway", request=req, response=resp_502)) is True
        assert is_retryable_llm_error(httpx.HTTPStatusError("503 Unavailable", request=req, response=resp_503)) is True

        assert (
            is_retryable_llm_error(httpx.HTTPStatusError("401 Unauthorized", request=req, response=resp_401)) is False
        )
        assert is_retryable_llm_error(httpx.HTTPStatusError("400 Bad Request", request=req, response=resp_400)) is False

    def test_aistream_errors(self):
        assert is_retryable_llm_error(AIStreamError(code=ErrorCode.AI_TIMEOUT, message="timeout")) is True
        assert is_retryable_llm_error(AIStreamError(code=ErrorCode.AI_RATE_LIMITED, message="rate limited")) is True
        assert is_retryable_llm_error(AIStreamError(code=ErrorCode.AI_UNAVAILABLE, message="unavailable")) is True
        assert is_retryable_llm_error(AIStreamError(code=ErrorCode.AI_AUTH_FAILED, message="auth failed")) is False

    def test_extract_retry_after(self):
        req = httpx.Request("POST", "https://api.example.com")
        resp_with_header = httpx.Response(429, headers={"Retry-After": "3.5"}, request=req)
        err = httpx.HTTPStatusError("Rate Limit", request=req, response=resp_with_header)
        assert extract_retry_after(err) == 3.5

        resp_no_header = httpx.Response(500, request=req)
        err_no_header = httpx.HTTPStatusError("Server Error", request=req, response=resp_no_header)
        assert extract_retry_after(err_no_header) is None

    def test_classify_error_type(self):
        req = httpx.Request("POST", "https://api.example.com")
        assert classify_error_type(httpx.ConnectTimeout("timed out", request=req)) == "connecttimeout"
        assert classify_error_type(httpx.ConnectError("refused", request=req)) == "connect_error"
        resp_503 = httpx.Response(503, request=req)
        assert classify_error_type(httpx.HTTPStatusError("503", request=req, response=resp_503)) == "http_503"
        assert classify_error_type(AIStreamError(code=ErrorCode.AI_TIMEOUT, message="t")) == ErrorCode.AI_TIMEOUT


@pytest.mark.asyncio
class TestLLMGovernorExecution:
    async def test_call_with_governance_success(self):
        mock_fn = AsyncMock(return_value={"result": "ok"})
        res = await call_with_llm_governance("test_op", mock_fn, max_attempts=3)
        assert res == {"result": "ok"}
        assert mock_fn.call_count == 1

    async def test_call_with_governance_retries_on_transient_error(self):
        req = httpx.Request("POST", "https://api.example.com")
        mock_fn = AsyncMock(
            side_effect=[
                httpx.ConnectTimeout("Connect timeout", request=req),
                {"result": "recovered"},
            ]
        )
        res = await call_with_llm_governance("test_retry", mock_fn, max_attempts=3, base_delay=0.01, cap=0.1)
        assert res == {"result": "recovered"}
        assert mock_fn.call_count == 2

    async def test_call_with_governance_fails_fast_on_non_retriable_error(self):
        req = httpx.Request("POST", "https://api.example.com")
        resp_401 = httpx.Response(401, request=req)
        mock_fn = AsyncMock(side_effect=httpx.HTTPStatusError("401", request=req, response=resp_401))

        with pytest.raises(httpx.HTTPStatusError):
            await call_with_llm_governance("test_auth_fail", mock_fn, max_attempts=3)
        assert mock_fn.call_count == 1

    async def test_call_with_governance_exhausts_retries(self):
        req = httpx.Request("POST", "https://api.example.com")
        mock_fn = AsyncMock(side_effect=httpx.ConnectTimeout("Always timeout", request=req))

        with pytest.raises(httpx.ConnectTimeout):
            await call_with_llm_governance("test_exhaust", mock_fn, max_attempts=3, base_delay=0.01, cap=0.05)
        assert mock_fn.call_count == 3
