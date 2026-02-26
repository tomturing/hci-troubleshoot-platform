"""
API Gateway - 主应用
"""

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from contextlib import asynccontextmanager

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from shared.utils.otel import init_telemetry, instrument_app
from shared.database.redis import RedisManager
from shared.utils.logger import get_logger
from app.config import settings
from app.services.session import SessionManager
from app.routes import websocket, health, cases, conversations

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)

# 全局管理器
redis_manager: RedisManager = None
session_manager: SessionManager = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global redis_manager, session_manager
    
    # 启动
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT
    )
    
    redis_manager = RedisManager(settings.REDIS_URL)
    await redis_manager.connect()
    
    session_manager = SessionManager(redis_manager)
    websocket.set_session_manager(session_manager)
    
    yield
    
    # 关闭
    logger.info(
        event="service_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )
    await redis_manager.close()

app = FastAPI(
    title="HCI Troubleshoot - API Gateway",
    description="API网关服务",
    version="1.0.0",
    lifespan=lifespan
)

# 注入 OpenTelemetry 中间件到 app 实例（必须在 app 创建后调用）
instrument_app(app)

# 中间件
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# 注册路由
app.include_router(websocket.router)
app.include_router(cases.router)
app.include_router(conversations.router)
app.include_router(health.router)

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.SERVICE_PORT,
        reload=True
    )
