"""
API Gateway - 主应用

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
- CORS 使用显式来源列表
"""

import json
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from opentelemetry import trace as otel_trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.database.postgres import DatabaseManager
from shared.database.redis import RedisManager
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.middleware.error_handler import SecureErrorHandlerMiddleware  # 安全异常处理中间件
from app.routes import (
    assistants,
    audit,
    bridge_logs,
    capabilities,
    cases,
    conversations,
    diagnosis,
    environments,
    health,
    kb,
    simulations,
    terminal,
    websocket,
)
from app.services.session import SessionManager
from app.services.terminal import TerminalService

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    logger.info(event="service_starting", message=f"Starting {settings.SERVICE_NAME}", port=settings.SERVICE_PORT)

    redis_manager = RedisManager(settings.REDIS_URL)
    await redis_manager.connect()

    session_manager = SessionManager(redis_manager)

    # 数据库连接（终端操作录制写库需要）
    # DatabaseManager.__init__ 中已创建引擎，无需显式 connect()
    db_manager = DatabaseManager(settings.DATABASE_URL)

    # 终端服务：注入 db_manager，修复操作记录无法写库的问题（T4）
    terminal_service = TerminalService(redis_manager, db_manager=db_manager)
    await terminal_service.start()

    # 存入 app.state
    app.state.redis_manager = redis_manager
    app.state.db_manager = db_manager
    app.state.session_manager = session_manager
    app.state.terminal_service = terminal_service

    # 兼容现有路由注入方式
    websocket.set_session_manager(session_manager)
    # 部分分支的 websocket 路由未实现 terminal service 注入函数，做兼容判断
    if hasattr(websocket, "set_terminal_service"):
        websocket.set_terminal_service(terminal_service)

    yield

    # 关闭
    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    await terminal_service.shutdown()
    await db_manager.close()
    await redis_manager.close()


class TraceIDMiddleware(BaseHTTPMiddleware):
    """将 OTel Trace ID 注入到响应头 X-Trace-Id，使前端可直接用于 Grafana Tempo 查询。

    降级策略：若当前请求无有效 OTel Span（如健康检查），则使用 uuid4() 保持向后兼容。
    """

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)

        # 优先从 OTel Span Context 获取真实 Trace ID（32 位十六进制）
        span = otel_trace.get_current_span()
        ctx = span.get_span_context()
        if ctx and ctx.is_valid:
            trace_id = format(ctx.trace_id, "032x")
        else:
            # 无 OTel Span 时（如 /health）降级为 UUID，保持响应头始终存在
            trace_id = request.headers.get("X-Trace-Id") or str(uuid.uuid4())

        response.headers["X-Trace-Id"] = trace_id
        return response


class RequestSizeLimitMiddleware(BaseHTTPMiddleware):
    """请求体大小限制中间件，防止 DoS 攻击。

    限制：
    - 默认最大请求体: 1MB
    - SSE 流式端点例外（允许更大请求体）
    - 拒绝非豁免路径的 chunked 编码请求（绕过 Content-Length 检测的
      已知旁路，安全审计 2026-08-19）
    """

    MAX_REQUEST_SIZE = 1 * 1024 * 1024  # 1MB
    EXEMPT_PATHS = [
        "/api/conversations/",
        "/api/terminal/",
    ]

    async def dispatch(self, request: Request, call_next):
        is_exempt = any(
            request.url.path.startswith(path) for path in self.EXEMPT_PATHS
        )

        # chunked 编码无 Content-Length，会绕过下方大小检测；
        # JSON API 客户端正常都带 Content-Length，非豁免路径直接拒绝
        transfer_encoding = request.headers.get("transfer-encoding", "").lower()
        if "chunked" in transfer_encoding and not is_exempt:
            logger.warning(
                event="chunked_encoding_rejected",
                path=request.url.path,
            )
            return Response(
                content=json.dumps({"detail": "请提供 Content-Length（不支持 chunked 编码）"}),
                status_code=411,
                media_type="application/json",
            )

        # 检查请求体大小
        content_length = request.headers.get("content-length")
        if content_length:
            try:
                size = int(content_length)
                max_size = 10 * 1024 * 1024 if is_exempt else self.MAX_REQUEST_SIZE

                if size > max_size:
                    logger.warning(
                        event="request_too_large",
                        path=request.url.path,
                        content_length=size,
                        max_allowed=max_size,
                    )
                    return Response(
                        content=json.dumps({
                            "detail": f"请求体过大，最大允许 {max_size // (1024*1024)}MB"
                        }),
                        status_code=413,
                        media_type="application/json",
                    )
            except ValueError:
                pass

        return await call_next(request)


app = FastAPI(title="HCI Troubleshoot - API Gateway", description="API网关服务", version="1.0.0", lifespan=lifespan)

# 注入 OpenTelemetry 中间件到 app 实例（必须在 app 创建后调用）
instrument_app(app)
register_exception_handlers(app)

# 中间件 — CORS 使用显式来源列表，避免 allow_origins=["*"] + allow_credentials=True 的 RFC 6454 违规
# 注意：中间件注册顺序很重要，从下往上执行（最后注册的最先执行）
app.add_middleware(RequestSizeLimitMiddleware)  # 安全修复：请求体大小限制
app.add_middleware(SecureErrorHandlerMiddleware)  # 安全修复：异常处理信息泄漏防护
app.add_middleware(TraceIDMiddleware)
app.add_middleware(HTTPMetricsMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.cors_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(websocket.router)
app.include_router(cases.router)
app.include_router(conversations.router)
app.include_router(assistants.router)
app.include_router(environments.router)
app.include_router(kb.router)
app.include_router(kb.categories_router)
app.include_router(kb.catalogs_router)
app.include_router(kb.kbd_router)
app.include_router(kb.sop_admin_router)
app.include_router(kb.vm_console_router)
app.include_router(terminal.router)
app.include_router(audit.router)
app.include_router(health.router)
app.include_router(capabilities.router)
app.include_router(bridge_logs.router)
app.include_router(simulations.router)
app.include_router(diagnosis.router)


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
