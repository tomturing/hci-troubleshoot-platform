"""
Conversation Service - 主应用
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from shared.database.postgres import DatabaseManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry
from app.config import settings
from app.routes import conversations
from app.services.openclaw_client import OpenClawClient

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)

database_manager: DatabaseManager = None
openclaw_client: OpenClawClient = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global database_manager, openclaw_client
    
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT
    )
    
    # 初始化数据库
    database_manager = DatabaseManager(settings.DATABASE_URL)
    
    # 初始化OpenClaw客户端
    openclaw_client = OpenClawClient(
        base_url=settings.OPENCLAW_BASE_URL,
        api_key=settings.OPENCLAW_GATEWAY_TOKEN
    )
    
    # 注入依赖到路由
    conversations.set_dependencies(database_manager, openclaw_client)
    
    yield
    
    logger.info(
        event="service_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )
    if openclaw_client:
        await openclaw_client.close()
    if database_manager:
        await database_manager.close()

app = FastAPI(
    title="HCI Troubleshoot - Conversation Service",
    description="对话管理服务",
    version="1.0.0",
    lifespan=lifespan
)

# 注册路由
app.include_router(conversations.router)


@app.get("/health")
async def health_check():
    """健康检查"""
    # 简单的依赖检查
    db_status = "unknown"
    claw_status = "unknown"
    
    if openclaw_client:
        claw_status = "connected" if await openclaw_client.check_health() else "unhealthy"
        
    return {
        "status": "healthy", 
        "service": settings.SERVICE_NAME,
        "dependencies": {
            "openclaw": claw_status
        }
    }

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.SERVICE_PORT,
        reload=True
    )
