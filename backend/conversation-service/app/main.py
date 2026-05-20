"""
Conversation Service - 主应用 (v2.0 多类型AI助手)

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
- [PR-B] agent-service 拆分：移除本地 AgentRouter，改用 AgentClient HTTP 委托
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

from app.config import settings
from app.routes import audit as audit_route
from app.routes import conversations, evaluate
from app.services.agent_client import AgentClient
from shared.clients import AIAssistantRegistry, create_openclaw_client
from app.services.environment_client import EnvironmentClient
from shared.clients import KBClient
from shared.clients import SchedulerClient

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

    # 初始化 AI 助手注册表 (v2.0，保留 ai_registry 用于兜底直连路径)
    ai_registry = AIAssistantRegistry()

    # 初始化调度客户端（真实链路: Conversation -> Scheduler -> Pod）
    scheduler_client = SchedulerClient(settings.SCHEDULER_SERVICE_URL)

    # 按配置注册多助手客户端
    for assistant_type, cfg in settings.assistant_registry.items():
        if not cfg.get("enabled", True):
            continue
        base_url = cfg.get("base_url", settings.OPENCLAW_BASE_URL)
        gateway_token = cfg.get("gateway_token", settings.OPENCLAW_GATEWAY_TOKEN)
        provider_key = cfg.get("provider_api_key") or None
        model = cfg.get("model", assistant_type)
        client = create_openclaw_client(
            base_url=base_url,
            api_key=gateway_token,
            provider_api_key=provider_key,
            default_model=model,
            assistant_type=assistant_type,
        )
        is_default = bool(cfg.get("is_default", False))
        ai_registry.register(assistant_type, client, is_default=is_default)

    logger.info(
        event="ai_registry_initialized",
        message=f"Registered AI assistants: {ai_registry.list_types()}, default={ai_registry.get_default_type()}",
    )

    # 初始化 KB 客户端（可选）
    kb_client: KBClient | None = None
    if settings.KB_ENABLED:
        kb_client = KBClient(
            kb_service_url=settings.KB_SERVICE_URL,
            internal_token=settings.INTERNAL_API_TOKEN,
        )
        logger.info(event="kb_client_initialized", message=f"KB client 已初始化，目标: {settings.KB_SERVICE_URL}")

    # 初始化 Environment 客户端
    environment_client = EnvironmentClient(
        base_url=settings.CASE_SERVICE_URL,
        timeout_sec=settings.ENVIRONMENT_CONTEXT_TIMEOUT_SEC,
    )
    logger.info(
        event="environment_client_initialized",
        message=f"Environment client 已初始化，目标: {settings.CASE_SERVICE_URL}",
    )

    # [PR-B] 初始化 AgentClient（委托推理给 agent-service）
    agent_client: AgentClient | None = None
    if settings.AGENT_SERVICE_ENABLED:
        agent_client = AgentClient(settings.AGENT_SERVICE_URL)
        logger.info(
            event="agent_client_initialized",
            message=f"AgentClient 已初始化，目标: {settings.AGENT_SERVICE_URL}",
        )
    else:
        logger.info(event="agent_client_disabled", message="AGENT_SERVICE_ENABLED=false，使用直连 AI 路径")

    # 存入 app.state
    app.state.database_manager = database_manager
    app.state.ai_registry = ai_registry
    app.state.scheduler_client = scheduler_client
    app.state.kb_client = kb_client
    app.state.environment_client = environment_client
    app.state.agent_client = agent_client

    # 注入路由依赖
    conversations.set_dependencies(
        database_manager,
        ai_registry,
        scheduler_client,
        kb_client,
        environment_client,
        agent_client=agent_client,
    )
    evaluate.set_database_manager(database_manager)
    audit_route.set_audit_database_manager(database_manager)

    yield

    logger.info(event="service_stopping", message=f"Stopping {settings.SERVICE_NAME}")
    if ai_registry:
        await ai_registry.close_all()
    if database_manager:
        await database_manager.close()


app = FastAPI(
    title="HCI Troubleshoot - Conversation Service",
    description="对话管理服务 (v2.0 多类型AI助手)",
    version="2.0.0",
    lifespan=lifespan,
)

# 注入 OpenTelemetry 中间件到 app 实例
instrument_app(app)
app.add_middleware(HTTPMetricsMiddleware)

# H-1: 注册全局业务异常处理器
register_exception_handlers(app)

# 注册路由
app.include_router(conversations.router)
app.include_router(evaluate.router)
app.include_router(audit_route.router)


# 健康检查端点
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

    all_ok = db_ok and (not ai_status or any(v == "ok" for v in ai_status.values()))
    return {
        "status": "healthy" if all_ok else "degraded",
        "service": settings.SERVICE_NAME,
        "version": "2.1.0",
        "dependencies": {
            "database": "ok" if db_ok else "unavailable",
            "ai_assistants": ai_status,
            "kb": kb_ok,
        },
    }


@app.get("/health/live")
async def health_live():
    """存活探针：Pod 认为进程存活即返回 200"""
    return {"status": "alive"}


@app.get("/health/startup")
async def health_startup():
    """启动探针：验证 DB 连接是否就绪"""
    db_manager = getattr(app.state, "database_manager", None)
    if db_manager and not await db_manager.health_check():
        return {"status": "not_ready", "reason": "database_unavailable"}
    return {"status": "ready"}


@app.get("/health/ready")
async def health_ready():
    """就绪探针：验证所有依赖服务"""
    checks: dict = {}
    all_ok = True

    db_manager = getattr(app.state, "database_manager", None)
    if db_manager:
        checks["database"] = "ok" if await db_manager.health_check() else "unavailable"
        if checks["database"] != "ok":
            all_ok = False

    registry = getattr(app.state, "ai_registry", None)
    if registry:
        try:
            ai_status = await registry.health_check_all()
            checks["ai_assistants"] = ai_status
        except Exception as e:
            checks["ai_assistants"] = {"error": str(e)}
            all_ok = False

    kb_client = getattr(app.state, "kb_client", None)
    if kb_client:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(f"{settings.KB_SERVICE_URL}/health")
            checks["kb"] = "ok" if resp.status_code == 200 else "degraded"
        except Exception:
            checks["kb"] = "unavailable"
            all_ok = False

    return {
        "status": "ready" if all_ok else "not_ready",
        "checks": checks,
    }
