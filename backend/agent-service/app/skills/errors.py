"""
Skill 执行异常层次 — 明确划分「Agent/技能域」与「LLM/提供商域」问题边界。

为什么需要这一层（第一性原理）
----------------------------
旧实现把 skill 运行器的**所有**异常（含 LLM 后端超时、限流、鉴权失败、网络不可达）
统一泛化为 "Skill 不存在、未启用或输出不可用"。这导致排障时被误导去查「平台技能管理」，
而真实根因往往是 LLM 后端。本模块把异常按责任边界二分，且各自携带机器可识别的
`error_code` 与 `boundary`，从根本上杜绝误导。

问题边界（对抗性审查结论）
--------------------------
- Agent/Skill 域（boundary="agent"）：平台配置、技能定义、变量声明、输出解析等
  **平台自身因素**。责任方 = 平台。排障应查：技能管理（是否创建/启用）、SOP 变量声明、
  `output_path` 配置、工具管理、AI 客户端注册。
  - SkillNotFoundError          技能不存在或未启用
  - SkillToolDependencyError    技能 allowed_tools 引用了不存在/未启用的工具
  - SkillAIClientError          技能所需的 AI 客户端（assistant_type）未配置
  - SkillOutputUnavailableError 技能执行成功但未返回所请求的变量/路径
- LLM/提供商域（boundary="llm"）：外部 LLM 后端（火山 ARK 等）可用性，**与技能配置无关**。
  责任方 = LLM 提供商/基础设施。排障应查：LLM 后端健康度、API Key、配额、模型部署、网络。
  - SkillLLMError               LLM 调用失败基类
  - SkillLLMTimeoutError       读/连接超时（AI_TIMEOUT）
  - SkillLLMRateLimitError     限流（AI_RATE_LIMITED）
  - SkillLLMAuthError           鉴权失败（AI_AUTH_FAILED）
  - SkillLLMUpstreamError      上游 5xx/502/503（AI_UPSTREAM_ERROR）
  - SkillLLMUnavailableError   服务不可用/网络不可达（AI_UNAVAILABLE）
  - SkillLLMModelNotFoundError  模型不存在（chat/completions 404）
  - SkillLLMOutputError        LLM 返回内容不符合预期（非合法 JSON / 结构错误）

关键不变量：凡是 LLM 域异常，其 `boundary` 必为 "llm"，且文案中明确声明「与技能配置无关」。
凡是 Agent/技能域异常，其 `boundary` 必为 "agent"，且文案指向具体平台配置项。
"""

from __future__ import annotations

from typing import Any

import httpx
from shared.utils.exceptions import AIStreamError, ErrorCode


class SkillError(Exception):
    """Skill 执行异常基类。

    Attributes:
        boundary:   问题归属边界，"agent" 或 "llm"。
        error_code: 机器可识别错误码（供前端/日志分类与国际化）。
        user_message: 用户可见、不含敏感信息的友好说明。
        detail:    调试细节（仅日志可见，禁止透传前端）。
        context:   附加上下文（trace_id、skill_name 等），用于日志。
    """

    boundary: str = "agent"
    error_code: str = "skill_error"

    def __init__(
        self,
        user_message: str,
        *,
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(user_message)
        # 用具体子类的类属性覆盖基类默认值，避免实例属性被基类写死
        self.boundary = type(self).boundary
        self.error_code = type(self).error_code
        self.user_message = user_message
        self.detail = detail
        self.context: dict[str, Any] = context or {}

    def __str__(self) -> str:
        base = f"[{self.boundary}:{self.error_code}] {self.user_message}"
        if self.detail:
            base += f" | detail={self.detail}"
        return base


# ============================================================================
# Agent / Skill 域错误（平台自身责任，与 LLM 后端无关）
# ============================================================================


class SkillNotFoundError(SkillError):
    """技能不存在或未启用（技能管理里未配置或未开启 is_active）。"""

    boundary = "agent"
    error_code = "skill_not_found_or_disabled"

    def __init__(self, skill_name: str, *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        super().__init__(
            f"动态 Skill「{skill_name}」不存在或未启用：请到「平台技能管理」确认该技能已创建且 is_active=true",
            detail=detail or f"skill_name={skill_name}",
            context=context,
        )
        self.skill_name = skill_name


class SkillToolDependencyError(SkillError):
    """技能的 allowed_tools 引用了不存在或未启用的工具。"""

    boundary = "agent"
    error_code = "skill_tool_dependency_missing"

    def __init__(
        self,
        skill_name: str,
        missing_tools: list[str],
        *,
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            f"动态 Skill「{skill_name}」声明的工具 {missing_tools} 不存在或未启用：请检查工具管理",
            detail=detail,
            context=context,
        )
        self.skill_name = skill_name
        self.missing_tools = missing_tools


class SkillAIClientError(SkillError):
    """技能所需的 AI 客户端（assistant_type）未配置。"""

    boundary = "agent"
    error_code = "skill_ai_client_missing"

    def __init__(self, assistant_type: str, *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        super().__init__(
            f"未找到 Skill 执行所需的 AI 客户端（assistant_type={assistant_type}）：请检查平台 AI 客户端注册配置",
            detail=detail,
            context=context,
        )
        self.assistant_type = assistant_type


class SkillOutputUnavailableError(SkillError):
    """技能执行完成但输出中不含所请求的变量/路径（指令或 output_path 配置问题）。"""

    boundary = "agent"
    error_code = "skill_output_unavailable"

    def __init__(
        self,
        skill_name: str,
        variable_name: str | None = None,
        output_path: str | None = None,
        *,
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        where = f" 路径 {output_path}" if output_path else (f" 变量 {variable_name}" if variable_name else "")
        super().__init__(
            f"动态 Skill「{skill_name}」已执行，但未返回{where}的可用值：请检查技能指令与 SOP 变量的 output_path 配置",
            detail=detail,
            context=context,
        )
        self.skill_name = skill_name


# ============================================================================
# LLM / 提供商域错误（外部依赖责任，与技能配置无关）
# ============================================================================


class SkillLLMError(SkillError):
    """Skill 执行期间的 LLM 后端调用失败（与技能配置无关）。

    排障方向：查 LLM 后端健康度 / API Key / 配额 / 模型部署 / 网络，而非改技能。
    """

    boundary = "llm"
    error_code = "skill_llm_failed"

    def __init__(
        self,
        user_message: str,
        *,
        llm_code: str = "AI_UNAVAILABLE",
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(user_message, detail=detail, context=context)
        self.llm_code = llm_code


class SkillLLMTimeoutError(SkillLLMError):
    """LLM 读/连接超时（AI_TIMEOUT）。"""

    error_code = "skill_llm_timeout"

    def __init__(self, *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        super().__init__(
            "Skill 执行时 LLM 后端响应超时（AI_TIMEOUT）：与技能配置无关，请检查 LLM 后端健康度或稍后重试",
            llm_code="AI_TIMEOUT",
            detail=detail,
            context=context,
        )


class SkillLLMRateLimitError(SkillLLMError):
    """LLM 限流（AI_RATE_LIMITED）。"""

    error_code = "skill_llm_rate_limited"

    def __init__(self, *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        super().__init__(
            "Skill 执行时 LLM 后端限流（AI_RATE_LIMITED）：与技能配置无关，请检查配额或稍后重试",
            llm_code="AI_RATE_LIMITED",
            detail=detail,
            context=context,
        )


class SkillLLMAuthError(SkillLLMError):
    """LLM 鉴权失败（AI_AUTH_FAILED）。"""

    error_code = "skill_llm_auth_failed"

    def __init__(self, *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        super().__init__(
            "Skill 执行时 LLM 后端鉴权失败（AI_AUTH_FAILED）：与技能配置无关，请检查 LLM_API_KEY / 模型授权",
            llm_code="AI_AUTH_FAILED",
            detail=detail,
            context=context,
        )


class SkillLLMUpstreamError(SkillLLMError):
    """LLM 上游返回服务端错误（AI_UPSTREAM_ERROR，如 5xx/502/503）。"""

    error_code = "skill_llm_upstream"

    def __init__(
        self,
        status_code: int | None = None,
        *,
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        msg = "Skill 执行时 LLM 上游返回服务端错误（AI_UPSTREAM_ERROR）"
        if status_code:
            msg += f"（HTTP {status_code}）"
        msg += "：与技能配置无关，请检查 LLM 后端状态"
        super().__init__(msg, llm_code="AI_UPSTREAM_ERROR", detail=detail, context=context)
        self.status_code = status_code


class SkillLLMUnavailableError(SkillLLMError):
    """LLM 服务不可用 / 网络不可达（AI_UNAVAILABLE）。"""

    error_code = "skill_llm_unavailable"

    def __init__(self, *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        super().__init__(
            "Skill 执行时 LLM 后端不可用/网络不可达（AI_UNAVAILABLE）：与技能配置无关，请检查 LLM 端点网络连通性",
            llm_code="AI_UNAVAILABLE",
            detail=detail,
            context=context,
        )


class SkillLLMModelNotFoundError(SkillLLMError):
    """LLM 模型不存在（chat/completions 返回 404）。"""

    error_code = "skill_llm_model_not_found"

    def __init__(self, model: str = "", *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        model_hint = f" model={model}" if model else ""
        super().__init__(
            f"Skill 执行时 LLM 模型不存在（{model_hint.strip()}）：与技能配置无关，请检查 LLM 模型部署与 base_url",
            llm_code="AI_UNAVAILABLE",
            detail=detail,
            context=context,
        )
        self.model = model


class SkillLLMOutputError(SkillLLMError):
    """LLM 返回内容不符合预期（非合法 JSON / 结构错误）。

    属 LLM 输出质量问题（模型未遵守「仅输出 JSON」约束），与平台技能配置无关。
    """

    error_code = "skill_llm_output_invalid"

    def __init__(self, reason: str = "", *, detail: str = "", context: dict[str, Any] | None = None) -> None:
        super().__init__(
            "Skill 执行时 LLM 返回内容不符合预期（非合法 JSON 或结构错误）：与技能配置无关，"
            "属 LLM 输出质量问题，请检查 LLM 后端/模型或稍后重试",
            llm_code="AI_OUTPUT_INVALID",
            detail=detail or reason,
            context=context,
        )


def classify_llm_exception(exc: Exception) -> SkillLLMError:
    """将 LLM 调用抛出的异常（AIStreamError 或 httpx/底层异常）分类为明确的 SkillLLMError 子类。

    仅负责「LLM/提供商域」异常的分类；Agent/技能域异常（SkillNotFoundError 等）不应传入此处。
    任何无法精确归类的未知异常，兜底为 SkillLLMUnavailableError，但**始终标注 boundary="llm"**，
    绝不伪装成技能缺失。
    """
    detail = str(exc)

    # 1. 结构化 AIStreamError：直接按 code 映射（ai_client.invoke 失败的统一出口）
    if isinstance(exc, AIStreamError):
        code = exc.code
        if code == ErrorCode.AI_TIMEOUT:
            return SkillLLMTimeoutError(detail=exc.detail or detail)
        if code == ErrorCode.AI_RATE_LIMITED:
            return SkillLLMRateLimitError(detail=exc.detail or detail)
        if code == ErrorCode.AI_AUTH_FAILED:
            return SkillLLMAuthError(detail=exc.detail or detail)
        if code == ErrorCode.AI_UPSTREAM_ERROR:
            return SkillLLMUpstreamError(detail=exc.detail or detail)
        if code == ErrorCode.AI_UNAVAILABLE:
            return SkillLLMUnavailableError(detail=exc.detail or detail)
        return SkillLLMError(
            f"Skill 执行时 LLM 调用失败（{code}）：与技能配置无关",
            llm_code=str(code),
            detail=exc.detail or detail,
        )

    # 2. httpx / 传输层：按异常类型与 HTTP 状态码映射
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(exc, httpx.TimeoutException):
        return SkillLLMTimeoutError(detail=detail)
    if isinstance(exc, httpx.ConnectError):
        return SkillLLMUnavailableError(detail=detail)
    if status_code == 401 or status_code == 403:
        return SkillLLMAuthError(detail=detail)
    if status_code == 404:
        return SkillLLMModelNotFoundError(detail=detail)
    if status_code == 429:
        return SkillLLMRateLimitError(detail=detail)
    if isinstance(status_code, int) and 500 <= status_code < 600:
        return SkillLLMUpstreamError(status_code=status_code, detail=detail)
    if isinstance(exc, httpx.TransportError):
        return SkillLLMUnavailableError(detail=detail)

    # 3. 兜底：未知异常归为不可用，但明确标注「LLM 域」，不甩锅给技能
    return SkillLLMUnavailableError(detail=f"{type(exc).__name__}: {detail}")
