"""
自定义异常类 — 结构化错误信息

包含两个层次：
1. HCIException 层次结构：服务间通用业务异常，携带 HTTP 状态码和错误码，
   供 FastAPI exception handler 统一转换为 JSON 错误响应。
2. AIStreamError：SSE 流式响应专用，确保错误帧不破坏流结构。
"""

import json
import re
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# ============================================================================
# ErrorCode 枚举
# ============================================================================

class ErrorCode(StrEnum):
    """通用错误码枚举（SSE 错误 + REST 错误共用）"""

    # AI 服务相关错误
    AI_UPSTREAM_ERROR = "AI_UPSTREAM_ERROR"  # AI 上游服务错误（502/503 等）
    AI_TIMEOUT = "AI_TIMEOUT"  # AI 服务超时
    AI_AUTH_FAILED = "AI_AUTH_FAILED"  # AI 认证失败
    AI_RATE_LIMITED = "AI_RATE_LIMITED"  # AI 服务限流
    AI_UNAVAILABLE = "AI_UNAVAILABLE"  # AI 服务不可用

    # 网关相关错误
    GATEWAY_ERROR = "GATEWAY_ERROR"  # 网关通用错误
    STREAMING_ERROR = "STREAMING_ERROR"  # 流传输错误

    # 业务逻辑错误
    CONVERSATION_NOT_FOUND = "CONVERSATION_NOT_FOUND"
    CASE_NOT_FOUND = "CASE_NOT_FOUND"
    RESOURCE_NOT_FOUND = "RESOURCE_NOT_FOUND"

    # 外部/内部服务调用错误
    EXTERNAL_SERVICE_ERROR = "EXTERNAL_SERVICE_ERROR"  # 调用内部微服务超时或异常
    EXTERNAL_SERVICE_TIMEOUT = "EXTERNAL_SERVICE_TIMEOUT"

    # 配置/环境错误
    CONFIGURATION_ERROR = "CONFIGURATION_ERROR"  # 缺少或错误的配置

    # 输入校验错误
    VALIDATION_ERROR = "VALIDATION_ERROR"

    # 通用错误
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 内部错误（不暴露详情）


# ============================================================================
# H-1: HCIException 统一业务异常层次结构
# ============================================================================

class HCIException(Exception):
    """
    HCI 平台基础业务异常。

    所有业务异常应继承此类，FastAPI 全局 exception handler 将其统一转换为：
        {"error": {"code": ..., "message": ..., "detail": ...}}

    Attributes:
        code:        ErrorCode 枚举值
        message:     用户可见的友好提示（不含敏感信息）
        detail:      调试信息（仅开发/日志可见，禁止透传给前端）
        http_status: 建议的 HTTP 响应状态码
        context:     附加上下文（如 trace_id、resource_id），用于日志
    """

    def __init__(
        self,
        message: str,
        *,
        code: ErrorCode = ErrorCode.INTERNAL_ERROR,
        detail: str = "",
        http_status: int = 500,
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.detail = detail
        self.http_status = http_status
        self.context: dict[str, Any] = context or {}

    def to_dict(self) -> dict[str, Any]:
        """序列化为 JSON 错误响应体（detail 字段不对外暴露）"""
        return {"code": self.code.value, "message": self.message}

    def __str__(self) -> str:
        parts = [f"[{self.code}] {self.message}"]
        if self.detail:
            parts.append(f"detail={self.detail}")
        if self.context:
            parts.append(f"context={self.context}")
        return " | ".join(parts)


class ExternalServiceError(HCIException):
    """
    调用内部微服务（kb-service、scheduler-service 等）失败。

    示例：
        raise ExternalServiceError(
            service="kb-service",
            message="知识库搜索超时",
            detail="POST /search timeout after 5s",
            http_status=502,
        )
    """

    def __init__(
        self,
        message: str,
        *,
        service: str = "",
        detail: str = "",
        http_status: int = 502,
        context: dict[str, Any] | None = None,
    ) -> None:
        ctx = dict(context or {})
        if service:
            ctx["service"] = service
        code = (
            ErrorCode.EXTERNAL_SERVICE_TIMEOUT
            if "timeout" in detail.lower() or "timed out" in detail.lower()
            else ErrorCode.EXTERNAL_SERVICE_ERROR
        )
        super().__init__(message, code=code, detail=detail, http_status=http_status, context=ctx)


class ResourceNotFoundError(HCIException):
    """
    目标资源不存在（数据库查询为 None 等）。

    示例：
        raise ResourceNotFoundError("工单", resource_id=case_id)
    """

    def __init__(
        self,
        resource_type: str,
        *,
        resource_id: str | int | None = None,
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        rid_str = f" (id={resource_id})" if resource_id is not None else ""
        message = f"{resource_type}{rid_str} 不存在"
        ctx = dict(context or {})
        if resource_id is not None:
            ctx["resource_id"] = str(resource_id)
        ctx["resource_type"] = resource_type
        super().__init__(
            message,
            code=ErrorCode.RESOURCE_NOT_FOUND,
            detail=detail or message,
            http_status=404,
            context=ctx,
        )


class ConfigurationError(HCIException):
    """
    环境变量或服务配置缺失 / 错误。

    此类错误通常在启动阶段抛出，应导致服务拒绝启动而非静默运行。

    示例：
        raise ConfigurationError("OPENCLAW_API_KEY 未配置")
    """

    def __init__(
        self,
        message: str,
        *,
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(
            message,
            code=ErrorCode.CONFIGURATION_ERROR,
            detail=detail,
            http_status=500,
            context=context,
        )


class ValidationError(HCIException):
    """
    请求参数或业务规则校验失败（区别于 FastAPI 自带的 RequestValidationError）。

    示例：
        raise ValidationError("conversation_id 不能为空", field="conversation_id")
    """

    def __init__(
        self,
        message: str,
        *,
        field: str = "",
        detail: str = "",
        context: dict[str, Any] | None = None,
    ) -> None:
        ctx = dict(context or {})
        if field:
            ctx["field"] = field
        super().__init__(
            message,
            code=ErrorCode.VALIDATION_ERROR,
            detail=detail,
            http_status=422,
            context=ctx,
        )


@dataclass
class AIStreamError(Exception):
    """
    SSE 流错误异常

    用于在 SSE 流中传递结构化错误信息，确保：
    - message: 用户可见的友好提示
    - detail: 调试信息（不暴露敏感内容）
    - code: 错误码，前端可用于国际化

    示例:
        raise AIStreamError(
            code=ErrorCode.AI_UPSTREAM_ERROR,
            message="AI 服务暂时不可用",
            detail="status 502"
        )
    """

    code: ErrorCode
    message: str
    detail: str = ""

    def __post_init__(self):
        # 调用父类 Exception 的初始化，确保 args 被正确设置
        super().__init__(self.message)
        # 确保消息不含换行符，避免破坏 SSE 帧结构
        self.message = self.message.replace("\n", " ").replace("\r", "")
        self.detail = self.detail.replace("\n", " ").replace("\r", "")

    def to_sse_data(self) -> str:
        """
        生成 SSE data 行的 JSON 内容

        返回已序列化的 JSON 字符串，确保：
        - 特殊字符被正确转义
        - 无换行符破坏 SSE 帧结构
        """
        return json.dumps(
            {"code": self.code.value, "message": self.message, "detail": self.detail},
            ensure_ascii=False,
        )

    def __str__(self) -> str:
        return self.to_sse_data()


def sanitize_error_message(error: Exception) -> tuple[str, str]:
    """
    清理异常信息，移除敏感内容

    Args:
        error: 原始异常

    Returns:
        (message, detail): 清理后的消息和详情
    """
    error_str = str(error)
    error_type = type(error).__name__

    # 移除可能包含的 URL（敏感信息）
    # 匹配 http:// 或 https:// 开头的 URL
    url_pattern = r"https?://[^\s]+"
    sanitized = re.sub(url_pattern, "[URL]", error_str)

    return sanitized, f"type={error_type}"
