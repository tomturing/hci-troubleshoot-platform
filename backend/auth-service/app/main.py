"""auth-service 主应用。

职责：账号密码登录（customer/admin 双 realm）、JWT(RS256+JWKS) 签发、
登录审计、会话写入。不做授权（角色权限仍由下游既有逻辑判断）。
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.observability.logger import get_logger
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

# 触发 repository 模块级 engine 初始化
import app.services.repository  # noqa: F401
from app.config import settings
from app.routes import auth

init_telemetry(settings.SERVICE_NAME)
logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info(event="service_starting", message=f"Starting {settings.SERVICE_NAME}", port=settings.SERVICE_PORT)
    yield
    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")


app = FastAPI(
    title="HCI Troubleshoot - Auth Service",
    description="统一认证服务：账号密码登录、JWT(RS256+JWKS)签发、会话与登录审计",
    version="0.1.0",
    lifespan=lifespan,
)

instrument_app(app)
register_exception_handlers(app)

app.include_router(auth.router)


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点。"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health_check():
    return {"status": "healthy", "service": settings.SERVICE_NAME}


@app.get("/health/live")
async def health_live():
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready():
    return {"status": "ready"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
