"""
KB Service - 主应用

架构决策：
- 遵循现有项目 app.state DI 模式（不使用全局变量）
- OTel 在 app 创建前初始化，FastAPIInstrumentor 在 app 创建后注入
- 保持与 conversation-service / scheduler-service 一致的启动/关闭流程
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.database.postgres import DatabaseManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry, instrument_app

from app.config import settings
from app.routes import admin, atoms, health, ingest, search
from app.services.embedding import EmbeddingService
from app.services.sop_matcher import SopMatcher

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME} v3.0",
        port=settings.SERVICE_PORT,
        embedding_dim=settings.EMBEDDING_DIM,
        chunk_size=settings.CHUNK_SIZE,
    )

    # 初始化数据库
    database_manager = DatabaseManager(settings.DATABASE_URL)

    # 初始化 Embedding 服务（z.ai 主力 + bge-small 降级）
    embedding_service = EmbeddingService(settings)

    # 初始化 SOP Matcher（从 sop_skills/ 目录加载 keywords_map.json）
    sop_matcher = SopMatcher(settings.SOP_SKILLS_DIR)
    await sop_matcher.load()

    # 存入 app.state，供路由通过 request.app.state 访问
    app.state.database_manager = database_manager
    app.state.embedding_service = embedding_service
    app.state.sop_matcher = sop_matcher

    # 注入依赖到路由模块（兼容 Depends 模式）
    ingest.set_dependencies(database_manager, embedding_service)
    search.set_dependencies(database_manager, embedding_service, sop_matcher)
    admin.set_dependencies(database_manager)
    atoms.set_dependencies(database_manager, embedding_service)

    logger.info(event="service_started", message=f"{settings.SERVICE_NAME} ready")

    yield

    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    await database_manager.close()


app = FastAPI(
    title="HCI Troubleshoot - KB Service",
    description="知识库服务（RAG 检索 + 文档入库 + SOP 匹配）",
    version="3.0.0",
    lifespan=lifespan,
)

# 注入 OpenTelemetry 中间件
instrument_app(app)

# 注册路由
app.include_router(health.router)
app.include_router(search.router)
app.include_router(ingest.router)
app.include_router(admin.router)
app.include_router(atoms.router)


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
