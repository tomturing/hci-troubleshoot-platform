"""
API Gateway - 主应用

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
- CORS 使用显式来源列表
"""

import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import Response
from opentelemetry import trace as otel_trace
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.database.postgres import DatabaseManager
from shared.database.redis import RedisManager
from shared.utils.logger import get_logger
from shared.utils.metrics import HTTPMetricsMiddleware
from shared.utils.otel import init_telemetry, instrument_app
from starlette.middleware.base import BaseHTTPMiddleware

from app.config import settings
from app.routes import assistants, cases, conversations, environments, health, kb, terminal, websocket
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


app = FastAPI(title="HCI Troubleshoot - API Gateway", description="API网关服务", version="1.0.0", lifespan=lifespan)

# 注入 OpenTelemetry 中间件到 app 实例（必须在 app 创建后调用）
instrument_app(app)

# 中间件 — CORS 使用显式来源列表，避免 allow_origins=["*"] + allow_credentials=True 的 RFC 6454 违规
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
app.include_router(kb.kbd_router)
app.include_router(kb.sop_admin_router)
app.include_router(terminal.router)
app.include_router(health.router)


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
