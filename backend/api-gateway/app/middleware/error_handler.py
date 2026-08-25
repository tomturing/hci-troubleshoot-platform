"""
全局安全异常处理中间件

安全审计 2026-08-19 修复（信息泄漏防护，CWE-209）：
未捕获异常的完整堆栈只进结构化日志；对客户端一律返回通用错误消息。
error_id（uuid4）用于日志与客户端反馈的关联追踪。
"""

import traceback
import uuid
from uuid import UUID

from fastapi import Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from shared.observability.logger import get_logger

logger = get_logger("error-handler")


class SecureErrorHandlerMiddleware(BaseHTTPMiddleware):
    """
    兜底异常处理中间件。

    说明：HTTPException / RequestValidationError 由 FastAPI 的
    ExceptionMiddleware 在更内层处理，不会以异常形式进入本 dispatch；
    此处只兜未捕获的 Exception（防堆栈/内部信息泄漏到响应）。
    """

    async def dispatch(self, request: Request, call_next):
        try:
            return await call_next(request)
        except RequestValidationError:
            # 理论上由内层处理；防御性兜底，不外泄堆栈
            return JSONResponse(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                content={"error": "validation_error", "message": "请求参数验证失败"},
            )
        except Exception:
            error_id = uuid.uuid4()
            logger.error(
                event="unhandled_exception",
                error_id=str(error_id),
                path=request.url.path,
                method=request.method,
                stack_trace=traceback.format_exc(),
            )
            return JSONResponse(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                content={
                    "error": "internal_error",
                    "message": "服务暂时不可用，请稍后重试",
                    "error_id": str(error_id),
                },
            )


def register_exception_handlers(app):
    """兼容占位：FastAPI 应用统一使用 shared.utils.exception_handlers 注册。"""
    return None
