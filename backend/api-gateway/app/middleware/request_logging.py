"""
请求日志中间件

记录每个入站请求的 method, path, status_code 和耗时(ms)。
使用结构化日志格式，自动包含 trace_id。
"""

import time
from collections.abc import Callable

from fastapi import Request, Response
from shared.utils.logger import get_logger
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


class RequestLoggingMiddleware(BaseHTTPMiddleware):
    """
    请求日志中间件

    对每个入站 HTTP 请求记录:
    - method: HTTP 方法
    - path: 请求路径
    - status_code: 响应状态码
    - duration_ms: 请求处理耗时（毫秒）
    """

    async def dispatch(self, request: Request, call_next: Callable) -> Response:
        # 记录请求开始时间
        start_time = time.perf_counter()

        # 调用下一个中间件/路由处理器
        response = await call_next(request)

        # 计算耗时（毫秒）
        duration_ms = int((time.perf_counter() - start_time) * 1000)

        # 记录请求日志（INFO 级别）
        logger.info(
            event="http_request",
            message=f"{request.method} {request.url.path} -> {response.status_code}",
            method=request.method,
            path=request.url.path,
            query=str(request.query_params) if request.query_params else None,
            status_code=response.status_code,
            duration_ms=duration_ms,
            client_host=request.client.host if request.client else None,
        )

        return response
