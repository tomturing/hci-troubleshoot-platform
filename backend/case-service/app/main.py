"""
Case Service - 主应用

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from shared.database.postgres import DatabaseManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry, instrument_app

from app.config import settings
from app.routes import cases

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT
    )

    database_manager = DatabaseManager(settings.DATABASE_URL)

    # 存入 app.state
    app.state.database_manager = database_manager

    # 兼容现有路由注入方式
    cases.set_database_manager(database_manager)

    yield

    # 关闭
    logger.info(
        event="service_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )
    await database_manager.close()

app = FastAPI(
    title="HCI Troubleshoot - Case Service",
    description="工单管理服务",
    version="1.0.0",
    lifespan=lifespan
)

# 注入 OpenTelemetry 中间件到 app 实例
instrument_app(app)

# 注册路由
app.include_router(cases.router)

@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": settings.SERVICE_NAME}

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.SERVICE_PORT,
        reload=True
    )
