"""
Eval Service - 主应用

AI 评分 + 质量统计独立微服务：
- 从 conversation-service 迁移 /api/conversations/{id}/evaluate、/api/stats/*
- 新增 /api/stats/agents（三大脑实时指标对比）

eval-service 直接访问数据库（assistant_evaluation、conversation 表），
不依赖 conversation-service 或 agent-service。
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

from app.config import settings
from app.routes.agent_stats import router as agent_stats_router
from app.routes.evaluate import router as evaluate_router
from app.routes.evaluate import set_database_manager

init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT,
    )

    # 初始化数据库连接（eval-service 需要读写 assistant_evaluation 和 conversation 表）
    database_manager = DatabaseManager(settings.DATABASE_URL)
    set_database_manager(database_manager)

    logger.info(event="database_initialized", message="数据库连接池已初始化")

    yield

    await database_manager.close()
    logger.info(event="service_stopped", message=f"{settings.SERVICE_NAME} 已停止")


# ── FastAPI 应用 ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="HCI Eval Service",
    description="AI 评分与质量统计微服务",
    version="1.0.0",
    lifespan=lifespan,
)

instrument_app(app)
app.add_middleware(HTTPMetricsMiddleware)
register_exception_handlers(app)

# 路由挂载
app.include_router(evaluate_router)
app.include_router(agent_stats_router)


@app.get("/health/live")
async def health_live() -> dict:
    """存活探针"""
    return {"status": "alive"}


@app.get("/health/ready")
async def health_ready() -> dict:
    """就绪探针"""
    return {"status": "ready"}


@app.get("/metrics")
async def metrics() -> Response:
    """Prometheus 指标"""
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
