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
from shared.observability.logger import get_logger
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

from app.config import settings
from app.routes import (
    admin,
    categories,
    classify,
    extract_signals,
    health,
    hits,
    ingest,
    kbd_search,
    route,
    sop_ingest,
)
from app.services.embedding import EmbeddingService

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    try:
        from shared.models.dynamic_resource import DynamicResourceRevision  # noqa: F401
        from shared.models.system_prompt import SystemPrompt  # noqa: F401
        from sqlalchemy.orm import configure_mappers

        configure_mappers()
        logger.info("SQLAlchemy mappers 编译配置成功，动态资源模型检查通过")
    except Exception as exc:
        logger.critical(f"SQLAlchemy mappers 编译失败，发现外键或元数据配置错误: {exc}", exc_info=True)
        raise

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

    # 存入 app.state，供路由通过 request.app.state 访问
    app.state.database_manager = database_manager
    app.state.embedding_service = embedding_service

    # 注入依赖到路由模块（兼容 Depends 模式）
    ingest.set_dependencies(database_manager, embedding_service)
    kbd_search.set_dependencies(database_manager, embedding_service)  # KBD 语义检索（agent 专用）
    admin.set_dependencies(database_manager, embedding_service)  # 注入 embedding 服务
    route.set_dependencies(database_manager)
    classify.set_dependencies(database_manager)
    extract_signals.set_dependencies(database_manager)  # 关键信号分级抽取
    sop_ingest.set_dependencies(database_manager)  # SOP 文档入库
    categories.set_dependencies(database_manager, embedding_service)  # 分类管理路由
    hits.set_dependencies(database_manager)  # 知识命中统计路由

    logger.info(event="service_started", message=f"{settings.SERVICE_NAME} ready")

    yield

    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    await database_manager.close()


app = FastAPI(
    title="HCI Troubleshoot - KB Service",
    description="知识库服务（KBD/SOP 管理 + 分类检索）",
    version="3.0.0",
    lifespan=lifespan,
)

# 注入 OpenTelemetry 中间件
instrument_app(app)

# H-1: 注册全局业务异常处理器
register_exception_handlers(app)

# 注册路由
app.include_router(health.router)
app.include_router(route.router)
app.include_router(ingest.router)
app.include_router(kbd_search.router)  # KBD 语义检索（agent-service 专用）
app.include_router(admin.router)
app.include_router(admin.kbd_router)  # KBD 审核路由
app.include_router(admin.sop_router)  # SOP 审核路由
app.include_router(classify.router)
app.include_router(extract_signals.router)  # 关键信号分级抽取
app.include_router(sop_ingest.router)  # SOP 文档入库
app.include_router(categories.router)  # 分类管理路由
app.include_router(hits.sop_hit_router)  # SOP 命中统计路由
app.include_router(hits.kbd_hit_router)  # KBD 命中统计路由


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
