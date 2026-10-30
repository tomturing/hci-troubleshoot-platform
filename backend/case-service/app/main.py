"""
Case Service - 主应用

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
"""

import asyncio
from contextlib import asynccontextmanager, suppress

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

from app.config import settings
from app.routes import cases, environments, log_collect, log_collect_scenarios

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # 启动
    logger.info(event="service_starting", message=f"Starting {settings.SERVICE_NAME}", port=settings.SERVICE_PORT)

    database_manager = DatabaseManager(settings.DATABASE_URL)

    # 存入 app.state
    app.state.database_manager = database_manager

    # 兼容现有路由注入方式
    cases.set_database_manager(database_manager)
    environments.set_database_manager(database_manager)
    log_collect.set_database_manager(database_manager)
    log_collect_scenarios.set_database_manager(database_manager)

    # 接管上一个进程遗留的代传（发布/重启/OOM 后不靠人工干预）：
    # 租约过期的 running 与优雅退出时交还的 pending 都会在这里被重新拉起。
    relay_reconcile = asyncio.create_task(log_collect.reconcile_relays_on_startup())

    yield

    # 关闭
    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    relay_reconcile.cancel()
    with suppress(asyncio.CancelledError):
        await relay_reconcile
    # 必须在关库之前交还租约：pending 是「下一个实例该接管」的唯一凭据；
    # 若进程被 SIGKILL 写不进去，则由租约过期兼底。
    await log_collect.shutdown_relays()
    await database_manager.close()


app = FastAPI(title="HCI Troubleshoot - Case Service", description="工单管理服务", version="1.0.0", lifespan=lifespan)

# 注入 OpenTelemetry 中间件到 app 实例
instrument_app(app)
app.add_middleware(HTTPMetricsMiddleware)

# H-1: 注册全局业务异常处理器
register_exception_handlers(app)

# 注册路由
app.include_router(cases.router)
app.include_router(environments.router)
app.include_router(log_collect.router)
# 数据面直连：/api/direct/log-collect-uploads 必须绕过 API Gateway（1 MiB 限制）。
app.include_router(log_collect.upload_router)
# 故障场景字典：客户读（/api/log-collect/scenarios）+ 管理端读写（/internal/log-collect/scenarios）
app.include_router(log_collect_scenarios.router)
app.include_router(log_collect_scenarios.admin_router)


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


@app.get("/health")
async def health_check():
    """健康检查"""
    return {"status": "healthy", "service": settings.SERVICE_NAME}


# ── J-2：三级探针分级健康端点 ─────────────────────
@app.get("/health/live")
async def health_live():
    """Liveness 探针：只检查进程存活，不检查外部依赖"""
    return {"status": "alive"}


@app.get("/health/startup")
async def health_startup():
    """Startup 探针：初始化完成后返回 200"""
    return {"status": "started"}


@app.get("/health/ready")
async def health_ready():
    """Readiness 探针：服务就绪时才加入流量"""
    return {"status": "ready"}


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
