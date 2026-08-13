"""离线诊断服务统一错误契约。"""

from typing import Any

from fastapi import FastAPI, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

logger = get_logger("diagnosis-service-errors")


class DiagnosisError(Exception):
    """离线诊断领域错误。"""

    def __init__(
        self,
        *,
        code: str,
        message: str,
        http_status: int,
        retryable: bool = False,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.http_status = http_status
        self.retryable = retryable
        self.details = details or {}


def _response(
    *,
    status_code: int,
    code: str,
    message: str,
    retryable: bool,
    details: dict[str, Any] | list[Any] | None = None,
) -> JSONResponse:
    """创建符合诊断领域契约的错误响应。"""

    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "trace_id": get_current_trace_id(),
                "retryable": retryable,
                "details": details or {},
            }
        },
    )


def register_error_handlers(app: FastAPI) -> None:
    """注册领域错误、参数错误和未捕获异常处理器。"""

    @app.exception_handler(DiagnosisError)
    async def diagnosis_error_handler(request: Request, exc: DiagnosisError) -> JSONResponse:
        logger.warning(
            event="diagnosis_error",
            code=exc.code,
            message=exc.message,
            path=request.url.path,
            method=request.method,
        )
        return _response(
            status_code=exc.http_status,
            code=exc.code,
            message=exc.message,
            retryable=exc.retryable,
            details=exc.details,
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
        return _response(
            status_code=422,
            code="VALIDATION_ERROR",
            message="请求参数校验失败",
            retryable=False,
            details=jsonable_encoder(exc.errors()),
        )

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            event="diagnosis_unhandled_error",
            message="诊断服务发生未处理异常",
            path=request.url.path,
            method=request.method,
            error=exc,
        )
        return _response(
            status_code=500,
            code="INTERNAL_ERROR",
            message="服务内部错误，请稍后重试",
            retryable=True,
        )
