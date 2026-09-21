"""
Skill 执行异常边界测试。

验证两类核心不变量：
1. LLM/提供商域异常（SkillLLM*）必须 boundary="llm"，且 classify_llm_exception 将
   AIStreamError / httpx 异常精确归类，绝不伪装成技能缺失。
2. Agent/技能域异常（SkillNotFoundError 等）必须 boundary="agent"，且文案指向具体平台配置项。
"""

import httpx
import pytest

from shared.utils.exceptions import AIStreamError, ErrorCode

from app.skills.errors import (
    SkillError,
    SkillLLMAuthError,
    SkillLLMError,
    SkillLLMModelNotFoundError,
    SkillLLMOutputError,
    SkillLLMRateLimitError,
    SkillLLMTimeoutError,
    SkillLLMUnavailableError,
    SkillLLMUpstreamError,
    SkillNotFoundError,
    SkillOutputUnavailableError,
    classify_llm_exception,
)


# ── LLM/提供商域：classify_llm_exception 精确归类 ──────────────────────────────


def test_classify_ai_stream_error_maps_all_codes():
    """AIStreamError 的每个错误码都应映射为对应 LLM 域子类，且 boundary=llm。"""
    cases = {
        ErrorCode.AI_TIMEOUT: SkillLLMTimeoutError,
        ErrorCode.AI_RATE_LIMITED: SkillLLMRateLimitError,
        ErrorCode.AI_AUTH_FAILED: SkillLLMAuthError,
        ErrorCode.AI_UPSTREAM_ERROR: SkillLLMUpstreamError,
        ErrorCode.AI_UNAVAILABLE: SkillLLMUnavailableError,
    }
    for code, expected in cases.items():
        exc = AIStreamError(code=code, message="m", detail="d")
        result = classify_llm_exception(exc)
        assert isinstance(result, expected)
        assert result.boundary == "llm"
        assert result.llm_code == code.value


def test_classify_ai_stream_error_unknown_code_falls_back_to_base_llm():
    exc = AIStreamError(code=ErrorCode.INTERNAL_ERROR, message="m", detail="d")
    result = classify_llm_exception(exc)
    assert isinstance(result, SkillLLMError)
    assert result.boundary == "llm"
    assert result.llm_code == ErrorCode.INTERNAL_ERROR.value


def test_classify_httpx_timeout_is_llm_timeout():
    result = classify_llm_exception(httpx.ReadTimeout("read timeout"))
    assert isinstance(result, SkillLLMTimeoutError)
    assert result.boundary == "llm"
    assert result.llm_code == "AI_TIMEOUT"


def test_classify_httpx_connect_error_is_unavailable():
    result = classify_llm_exception(httpx.ConnectError("refused"))
    assert isinstance(result, SkillLLMUnavailableError)
    assert result.boundary == "llm"


def test_classify_http_status_codes():
    """HTTP 状态码应精确归类，且均属 LLM 域。"""
    assert isinstance(classify_llm_exception(_status_exc(401)), SkillLLMAuthError)
    assert isinstance(classify_llm_exception(_status_exc(403)), SkillLLMAuthError)
    assert isinstance(classify_llm_exception(_status_exc(404)), SkillLLMModelNotFoundError)
    assert isinstance(classify_llm_exception(_status_exc(429)), SkillLLMRateLimitError)
    assert isinstance(classify_llm_exception(_status_exc(500)), SkillLLMUpstreamError)
    assert isinstance(classify_llm_exception(_status_exc(503)), SkillLLMUpstreamError)


def test_classify_unknown_exception_falls_back_to_unavailable_not_skill():
    """未知异常必须兜底为 LLM 域不可用，绝不能伪装成技能缺失。"""
    result = classify_llm_exception(ValueError("generator didn't stop after throw()"))
    assert isinstance(result, SkillLLMUnavailableError)
    assert result.boundary == "llm"


# ── Agent/技能域：边界与文案 ─────────────────────────────────────────────────


def test_skill_not_found_is_agent_boundary():
    exc = SkillNotFoundError("hci-alert-parsing")
    assert exc.boundary == "agent"
    assert exc.error_code == "skill_not_found_or_disabled"
    assert "hci-alert-parsing" in exc.user_message
    assert "技能管理" in exc.user_message  # 指向具体平台配置项


def test_skill_output_unavailable_is_agent_boundary():
    exc = SkillOutputUnavailableError("hci-alert-parsing", variable_name="node_ip", output_path="values.node_ip")
    assert exc.boundary == "agent"
    assert exc.error_code == "skill_output_unavailable"


def test_skill_error_hierarchy():
    """LLM 域异常必须是 SkillLLMError 子类，且最终继承 SkillError。"""
    for cls in (
        SkillLLMTimeoutError,
        SkillLLMRateLimitError,
        SkillLLMAuthError,
        SkillLLMUpstreamError,
        SkillLLMUnavailableError,
        SkillLLMModelNotFoundError,
        SkillLLMOutputError,
    ):
        assert issubclass(cls, SkillLLMError)
        assert issubclass(cls, SkillError)


def _status_exc(status: int) -> Exception:
    """构造带 response.status_code 的最小桩异常（避免依赖 httpx 完整异常构造）。"""
    exc: Exception = ValueError("http error")
    exc.response = type("Resp", (), {"status_code": status})()  # type: ignore[attr-defined]
    return exc
