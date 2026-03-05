"""
API Gateway - 主应用

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
- CORS 使用显式来源列表
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from shared.database.redis import RedisManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry, instrument_app

from app.config import settings
from app.routes import assistants, cases, conversations, health, kb, websocket
from app.services.session import SessionManager

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

    # 存入 app.state
    app.state.redis_manager = redis_manager
    app.state.session_manager = session_manager

    # 兼容现有路由注入方式
    websocket.set_session_manager(session_manager)

    yield

    # 关闭
    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    await redis_manager.close()


app = FastAPI(title="HCI Troubleshoot - API Gateway", description="API网关服务", version="1.0.0", lifespan=lifespan)

# 注入 OpenTelemetry 中间件到 app 实例（必须在 app 创建后调用）
instrument_app(app)

# 中间件 — CORS 使用显式来源列表，避免 allow_origins=["*"] + allow_credentials=True 的 RFC 6454 违规
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
app.include_router(kb.router)
app.include_router(health.router)

if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
