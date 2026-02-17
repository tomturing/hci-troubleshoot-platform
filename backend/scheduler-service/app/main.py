"""
Scheduler Service - 主应用
"""

from fastapi import FastAPI
from contextlib import asynccontextmanager

import sys
import os
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '../..')))

from shared.utils.logger import get_logger
from app.config import settings

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT
    )
    
    yield
    
    logger.info(
        event="service_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )

app = FastAPI(
    title="HCI Troubleshoot - Scheduler Service",
    description="Pod调度服务",
    version="1.0.0",
    lifespan=lifespan
)

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
