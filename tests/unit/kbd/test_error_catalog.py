"""KBD Pipeline 错误码必须稳定且对操作者可解释。"""
from __future__ import annotations

import httpx
from kbd.error_catalog import humanize_error


def test_timeout_has_stable_chinese_retryable_error():
    result = humanize_error(httpx.ReadTimeout("slow"))

    assert result.code == "LLM_TIMEOUT"
    assert result.retryable is True
    assert "超时" in result.message


def test_rate_limit_has_stable_error_code():
    request = httpx.Request("POST", "http://kb-service/api/kb/classify")
    response = httpx.Response(429, request=request, headers={"Retry-After": "2"})

    result = humanize_error(httpx.HTTPStatusError("limited", request=request, response=response))

    assert result.code == "LLM_RATE_LIMITED"
    assert result.retryable is True


def test_wrapped_vision_job_rate_limit_is_not_unclassified():
    result = humanize_error(RuntimeError("Vision Job abc 失败：Provider 返回 429 Too Many Requests"))

    assert result.code == "LLM_RATE_LIMITED"
    assert result.retryable is True
