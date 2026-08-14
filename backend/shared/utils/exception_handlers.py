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
import uuid

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from shared.observability.logger import get_logger
from shared.utils.exceptions import ErrorCode, HCIException

logger = get_logger("exception-handler")


def _request_context(request: Request) -> dict[str, str]:
    """提取可跨网关关联的请求上下文；不记录 Authorization 和请求正文。"""
    return {
        "request_id": request.headers.get("X-Request-ID") or str(uuid.uuid4()),
        "trace_id": request.headers.get("X-Trace-Id") or "",
        "path": str(request.url.path),
        "method": request.method,
    }


def _error_response(code: str, message: str, status: int, context: dict[str, str]) -> JSONResponse:
    """生成统一格式的错误响应"""
    headers = {"X-Request-ID": context["request_id"]}
    if context.get("trace_id"):
        headers["X-Trace-Id"] = context["trace_id"]
    return JSONResponse(
        status_code=status,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


def register_exception_handlers(app: FastAPI) -> None:
    """
    向 FastAPI 应用注册全局异常处理器。

    处理顺序（FastAPI 按注册顺序精确匹配类型）：
    1. HCIException（及子类）→ 业务错误响应
    2. HTTPException         → 保持 FastAPI detail 兼容，同时写结构化日志
    3. RequestValidationError → 记录字段级校验错误
    4. Exception             → 兜底 500，不泄露内部堆栈
    """

    @app.exception_handler(HCIException)
    async def hci_exception_handler(request: Request, exc: HCIException) -> JSONResponse:
        context = _request_context(request)
        logger.warning(
            event="hci_exception",
            code=exc.code.value,
            message=exc.message,
            detail=exc.detail,
            context=exc.context,
            **context,
        )
        return _error_response(exc.code.value, exc.message, exc.http_status, context)

    @app.exception_handler(HTTPException)
    async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
        context = _request_context(request)
        detail = exc.detail
        code = detail.get("code") if isinstance(detail, dict) else f"HTTP_{exc.status_code}"
        logger.warning(
            event="http_exception",
            code=code,
            status_code=exc.status_code,
            detail=detail,
            **context,
        )
        # 既有前端依赖 detail；保留原格式，同时让响应头携带关联 ID。
        response = JSONResponse(status_code=exc.status_code, content={"detail": detail})
        response.headers["X-Request-ID"] = context["request_id"]
        if context.get("trace_id"):
            response.headers["X-Trace-Id"] = context["trace_id"]
        return response

    @app.exception_handler(RequestValidationError)
    async def request_validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        context = _request_context(request)
        # Pydantic model_validator 会把原始 ValueError 放进 ctx.error。直接交给
        # JSONResponse 会再次抛出 TypeError，把本应清晰的 422 参数错误伪装成 500。
        errors = jsonable_encoder(exc.errors(), custom_encoder={Exception: str})
        logger.warning(
            event="request_validation_failed",
            code="REQUEST_VALIDATION_ERROR",
            status_code=422,
            validation_errors=errors,
            **context,
        )
        response = JSONResponse(status_code=422, content={"detail": errors})
        response.headers["X-Request-ID"] = context["request_id"]
        if context.get("trace_id"):
            response.headers["X-Trace-Id"] = context["trace_id"]
        return response

    @app.exception_handler(Exception)
    async def unhandled_exception_handler(request: Request, exc: Exception) -> JSONResponse:
        # 打印完整堆栈到日志（不暴露给客户端）
        context = _request_context(request)
        logger.error(
            event="unhandled_exception",
            exc_type=type(exc).__name__,
            exc_message=str(exc),
            traceback=traceback.format_exc(),
            **context,
        )
        return _error_response(
            ErrorCode.INTERNAL_ERROR.value,
            "服务内部错误，请稍后重试",
            500,
            context,
        )
