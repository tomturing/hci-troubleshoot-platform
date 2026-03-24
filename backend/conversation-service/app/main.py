"""
Conversation Service - 主应用 (v2.0 多类型AI助手)

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from shared.database.postgres import DatabaseManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry, instrument_app

from app.config import settings
from app.routes import conversations, evaluate, audit as audit_route
from app.services.ai_client import AIAssistantRegistry, create_openclaw_client
from app.services.kb_client import KBClient
from app.services.scheduler_client import SchedulerClient

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(
        event="service_starting", message=f"Starting {settings.SERVICE_NAME} (v2.0)", port=settings.SERVICE_PORT
    )

    # 初始化数据库
    database_manager = DatabaseManager(settings.DATABASE_URL)

    # 初始化 AI 助手注册表 (v2.0)
    ai_registry = AIAssistantRegistry()

    # 初始化调度客户端（真实链路: Conversation -> Scheduler -> Pod）
    scheduler_client = SchedulerClient(settings.SCHEDULER_SERVICE_URL)

    # 按配置注册多助手客户端（统一 OpenAI-compatible /v1/chat/completions 接口）
    for assistant_type, cfg in settings.assistant_registry.items():
        if not cfg.get("enabled", True):
            continue
        base_url = cfg.get("base_url", settings.OPENCLAW_BASE_URL)
        gateway_token = cfg.get("gateway_token", settings.OPENCLAW_GATEWAY_TOKEN)
        model = cfg.get("model") or settings.OPENCLAW_DEFAULT_MODEL
        client = create_openclaw_client(
            base_url=base_url,
            api_key=gateway_token,
            default_model=model,
            assistant_type=assistant_type,
        )
        ai_registry.register(assistant_type, client)

    logger.info(event="ai_registry_initialized", message=f"Registered AI assistants: {ai_registry.list_types()}")

    # 存入 app.state
    app.state.database_manager = database_manager
    app.state.ai_registry = ai_registry
    app.state.scheduler_client = scheduler_client

    # 初始化 KB 客户端（可选：KB_ENABLED=false 时跳过）
    kb_client: KBClient | None = None
    if settings.KB_ENABLED:
        kb_client = KBClient(
            kb_service_url=settings.KB_SERVICE_URL,
            internal_token=settings.INTERNAL_API_TOKEN,
        )
        logger.info(event="kb_client_initialized", message=f"KB client 已初始化，目标: {settings.KB_SERVICE_URL}")
    app.state.kb_client = kb_client

    # 初始化 Redis（人工确认服务依赖）
    redis_client = None
    try:
        from redis.asyncio import from_url as redis_from_url
        redis_client = redis_from_url(settings.REDIS_URL, decode_responses=False)
        await redis_client.ping()
        logger.info(event="redis_connected", message=f"Redis 已连接: {settings.REDIS_URL}")
    except Exception as e:
        logger.warning(event="redis_unavailable", message=f"Redis 连接失败，人工确认功能不可用: {e}")
        redis_client = None
    app.state.redis_client = redis_client

    # 初始化 Phase 3/4 ReAct 相关服务（可选，需配置 SCP_BASE_URL + SCP_API_KEY）
    scp_adapter = None
    glm_client = None
    tool_router = None
    confirm_service = None
    knowledge_extractor = None
    if settings.REACT_ENABLED and settings.SCP_BASE_URL and settings.SCP_API_KEY:
        try:
            from app.adapters.scp_adapter import SCPAdapter
            from app.adapters.acli_adapter import AcliAdapter
            from app.adapters.tool_router import ToolRouter
            from app.core.glm_client import GLMClient

            api_key = settings.OPENCLAW_API_KEY or settings.OPENCLAW_GATEWAY_TOKEN
            glm_client = GLMClient(
                base_url=settings.OPENCLAW_BASE_URL,
                api_key=api_key,
                model=settings.GLM_MODEL,
            )
            scp_adapter = SCPAdapter(
                base_url=settings.SCP_BASE_URL,
                api_key=settings.SCP_API_KEY,
            )
            acli_adapter = AcliAdapter.from_env()
            tool_router = ToolRouter(scp=scp_adapter, acli=acli_adapter)
            # 初始化知识提炼服务（依赖 GLMClient + KB_SERVICE_URL）
            from app.services.knowledge_extractor import KnowledgeExtractor
            knowledge_extractor = KnowledgeExtractor.from_env(glm_client)
            logger.info(
                event="react_initialized",
                message="ReAct 引擎已初始化（GLMClient + ToolRouter + KnowledgeExtractor）",
            )
        except Exception as e:
            logger.warning(event="react_init_failed", message=f"ReAct 引擎初始化失败: {e}")

    # 初始化确认服务（依赖 Redis）
    if redis_client is not None:
        from app.services.confirm_service import ConfirmService
        confirm_service = ConfirmService(redis=redis_client)
        logger.info(event="confirm_service_ready", message="人工确认服务已就绪")

    app.state.scp_adapter = scp_adapter
    app.state.glm_client = glm_client

    # 兼容现有路由注入方式
    conversations.set_dependencies(
        database_manager,
        ai_registry,
        scheduler_client,
        kb_client,
        redis=redis_client,
        tool_router=tool_router,
        confirm_service=confirm_service,
        glm_client=glm_client,
        knowledge_extractor=knowledge_extractor,
    )
    evaluate.set_database_manager(database_manager)
    audit_route.set_audit_database_manager(database_manager)

    yield

    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    if ai_registry:
        await ai_registry.close_all()
    if database_manager:
        await database_manager.close()
    if redis_client:
        await redis_client.aclose()


app = FastAPI(
    title="HCI Troubleshoot - Conversation Service",
    description="对话管理服务 (v2.0 多类型AI助手)",
    version="2.0.0",
    lifespan=lifespan,
)

# 注入 OpenTelemetry 中间件到 app 实例
instrument_app(app)

# 注册路由
app.include_router(conversations.router)
app.include_router(evaluate.router)
app.include_router(audit_route.router)


@app.get("/health")
async def health_check():
    """健康检查，验证 DB + AI 助手 + KB 服务"""
    ai_status: dict = {}
    db_ok = False
    kb_ok: str = "disabled"

    db_manager = getattr(app.state, "database_manager", None)
    if db_manager:
        db_ok = await db_manager.health_check()

    registry = getattr(app.state, "ai_registry", None)
    if registry:
        ai_status = await registry.health_check_all()

    kb_client = getattr(app.state, "kb_client", None)
    if kb_client:
        import httpx

        try:
            async with httpx.AsyncClient(timeout=0.5) as client:
                resp = await client.get(f"{settings.KB_SERVICE_URL}/health")
            kb_ok = "ok" if resp.status_code == 200 else "degraded"
        except Exception:
            kb_ok = "unavailable"

    all_ok = db_ok and (not ai_status or any(v for v in ai_status.values()))
    return {
        "status": "healthy" if all_ok else "degraded",
        "service": settings.SERVICE_NAME,
        "version": "2.1.0",
        "dependencies": {
            "database": "ok" if db_ok else "unavailable",
            "ai_assistants": ai_status,
            "kb_service": kb_ok,
        },
    }


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
