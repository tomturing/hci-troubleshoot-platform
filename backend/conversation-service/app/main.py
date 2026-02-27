"""
Conversation Service - 主应用 (v2.0 多类型AI助手)
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from shared.database.postgres import DatabaseManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry, instrument_app
from app.config import settings
from app.routes import conversations
from app.services.ai_client import AIAssistantRegistry, create_openclaw_client

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)

database_manager: DatabaseManager = None
ai_registry: AIAssistantRegistry = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global database_manager, ai_registry
    
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME} (v2.0)",
        port=settings.SERVICE_PORT
    )
    
    # 初始化数据库
    database_manager = DatabaseManager(settings.DATABASE_URL)
    
    # 初始化 AI 助手注册表 (v2.0)
    ai_registry = AIAssistantRegistry()
    
    # 注册 OpenClaw 客户端 (默认助手)
    openclaw_client = create_openclaw_client(
        base_url=settings.OPENCLAW_BASE_URL,
        api_key=settings.OPENCLAW_GATEWAY_TOKEN
    )
    ai_registry.register("openclaw", openclaw_client)
    
    # TODO: 未来可在此注册更多AI助手类型
    # 例如: ai_registry.register("chatgpt", create_chatgpt_client(...))
    
    logger.info(
        event="ai_registry_initialized",
        message=f"Registered AI assistants: {ai_registry.list_types()}"
    )
    
    # 注入依赖到路由
    conversations.set_dependencies(database_manager, ai_registry)
    
    yield
    
    logger.info(
        event="service_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )
    if ai_registry:
        await ai_registry.close_all()
    if database_manager:
        await database_manager.close()

app = FastAPI(
    title="HCI Troubleshoot - Conversation Service",
    description="对话管理服务 (v2.0 多类型AI助手)",
    version="2.0.0",
    lifespan=lifespan
)

# 注入 OpenTelemetry 中间件到 app 实例
instrument_app(app)

# 注册路由
app.include_router(conversations.router)


@app.get("/health")
async def health_check():
    """健康检查"""
    ai_status = {}
    
    if ai_registry:
        ai_status = await ai_registry.health_check_all()
        
    return {
        "status": "healthy", 
        "service": settings.SERVICE_NAME,
        "version": "2.0.0",
        "dependencies": {
            "ai_assistants": ai_status
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
