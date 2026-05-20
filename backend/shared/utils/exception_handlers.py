"""
FastAPI 全局异常处理器 — H-1 统一错误响应

使用方式（在各服务的 app.py 中注册）：
    from shared.utils.exception_handlers import register_exception_handlers
    register_exception_handlers(app)

注册后：
- HCIException 及其子类 → {"error": {"code": ..., "message": ...}} + 对应 HTTP 状态码
- 未捕获的 Exception    → {"error": {"code": "INTERNAL_ERROR", "message": "服务内部错误"}} + 500
"""

import traceback

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from shared.utils.exceptions import ErrorCode, HCIException
from shared.observability.logger import get_logger

logger = get_logger("exception-handler")


def _error_response(code: str, message: str, status: int) -> JSONResponse:
    """生成统一格式的错误响应"""
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    向 FastAPI 应用注册全局异常处理器。

    处理顺序（FastAPI 按注册顺序精确匹配类型）：
    1. HCIException（及子类）→ 业务错误响应
    2. Exception             → 兜底 500，不泄露内部堆栈
    """

    @app.exception_handler(HCIException)
    async def hci_exception_handler(request: Request, exc: HCIException) -> JSONResponse:
        logger.warning(
            event="hci_exception",
            code=exc.code.value,
            message=exc.message,
            detail=exc.detail,
            context=exc.context,
            path=str(request.url.path),
            method=request.method,
        )
        return _error_response(exc.code.value, exc.message, exc.http_status)

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # 打印完整堆栈到日志（不暴露给客户端）
        logger.error(
            event="unhandled_exception",
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            traceback=traceback.format_exc(),
            path=str(request.url.path),
            method=request.method,
        )
        return _error_response(
            ErrorCode.INTERNAL_ERROR.value,
            "服务内部错误，请稍后重试",
            500,
        )
