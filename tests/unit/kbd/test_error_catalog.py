"""KBD Pipeline 错误码必须稳定且对操作者可解释。"""
from __future__ import annotations

import httpx
from kbd.error_catalog import JobFailureError, humanize_error


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


def test_job_failure_keeps_job_id_and_concrete_service_reason():
    result = humanize_error(
        JobFailureError("Vision", "093739b6b18f", "seq=0: VISION_API_KEY 未配置")
    )

    assert result.code == "VISION_JOB_FAILED"
    assert "job_id=093739b6b18f" in result.message
    assert "VISION_API_KEY 未配置" in result.message
    assert result.detail == "seq=0: VISION_API_KEY 未配置"
    assert result.action


def test_unexpected_error_exposes_sanitized_reason_instead_of_vague_jsonl_hint():
    result = humanize_error(ValueError("字段 image_items 不是数组 token=secret-value"))

    assert result.code == "PIPELINE_UNEXPECTED"
    assert "ValueError" in result.message
    assert "image_items 不是数组" in result.message
    assert "secret-value" not in result.message
