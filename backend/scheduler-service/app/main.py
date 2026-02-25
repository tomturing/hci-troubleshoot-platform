"""
Scheduler Service - 主应用
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager
import asyncio

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry
from app.config import settings
from app.services.k8s_client import K8sClient
from app.services.scheduler_service import SchedulerService
from app.routes import scheduler_routes

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)

# 全局实例
k8s_client: K8sClient = None
scheduler_service: SchedulerService = None

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    global k8s_client, scheduler_service
    
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT
    )
    
    # 初始化
    k8s_client = K8sClient()
    scheduler_service = SchedulerService(k8s_client)
    
    # 注入依赖
    scheduler_routes.set_scheduler_service(scheduler_service)
    
    # 启动后台任务 (Pod池维护)
    # 注意: FastAPI的lifespan是同步的还是异步的? 是异步的.
    # start() 方法如果是长时间运行的任务，应该用 create_task
    # 但 scheduler_service.start() 只是初始化和启动后台任务
    initialization_task = asyncio.create_task(scheduler_service.start())
    
    yield
    
    logger.info(
        event="service_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )
    # 清理工作 (如果有)

app = FastAPI(
    title="HCI Troubleshoot - Scheduler Service",
    description="Pod调度与生命周期管理服务",
    version="1.0.0",
    lifespan=lifespan
)

# 注册路由
app.include_router(scheduler_routes.router)


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
