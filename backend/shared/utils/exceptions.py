"""
自定义异常类 — 结构化错误信息

用于 SSE 流式响应中的错误回传，确保：
1. 错误信息可被前端解析
2. 不泄露敏感信息（如内部 URL）
3. SSE 帧结构不被破坏（无换行符）
"""

import json
import re
from dataclasses import dataclass
from enum import StrEnum


class ErrorCode(StrEnum):
    """SSE 错误码枚举"""

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

    # 通用错误
    INTERNAL_ERROR = "INTERNAL_ERROR"  # 内部错误（不暴露详情）


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
