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
from shared.utils.exception_handlers import register_exception_handlers
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app

from app.adapters.brain_router import BrainRouter
from app.adapters.htp_brain_adapter import HTPBrainAdapter
from app.adapters.ops_agent_brain_adapter import OpsAgentBrainAdapter
from app.config import settings
from app.routes import audit as audit_route
from app.routes import conversations, evaluate
from app.services.ai_client import AIAssistantRegistry, create_openclaw_client
from app.services.environment_client import EnvironmentClient
from app.services.kb_client import KBClient
from app.services.scheduler_client import SchedulerClient

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)

@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME} (v2.0)",
        port=settings.SERVICE_PORT
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
        # gateway_token: 内部 OpenClaw 网关鉴权，对直连外部 API（dashscope 等）可为空
        gateway_token = cfg.get("gateway_token", settings.OPENCLAW_GATEWAY_TOKEN)
        # provider_api_key: 外部 LLM 提供商密钥（dashscope/qwen/zhipu 等），与 gateway_token 分离
        # 若未配置则回退到 OPENCLAW_API_KEY 环境变量（ai_client.py 内处理）
        provider_key = cfg.get("provider_api_key") or None
        model = cfg.get("model", assistant_type)
        client = create_openclaw_client(
            base_url=base_url,
            api_key=gateway_token,
            provider_api_key=provider_key,
            default_model=model,
            assistant_type=assistant_type,
        )
        # is_default=true 的助手作为 ops-agent 降级时的首选
        is_default = bool(cfg.get("is_default", False))
        ai_registry.register(assistant_type, client, is_default=is_default)

    logger.info(
        event="ai_registry_initialized",
        message=f"Registered AI assistants: {ai_registry.list_types()}, default={ai_registry.get_default_type()}"
    )

    # 初始化 KB 客户端（可选：KB_ENABLED=false 时跳过）
    kb_client: KBClient | None = None
    if settings.KB_ENABLED:
        kb_client = KBClient(
            kb_service_url=settings.KB_SERVICE_URL,
            internal_token=settings.INTERNAL_API_TOKEN,
        )
        logger.info(event="kb_client_initialized", message=f"KB client 已初始化，目标: {settings.KB_SERVICE_URL}")

    # 初始化 Environment 客户端（用于获取 S0 阶段环境上下文）
    environment_client = EnvironmentClient(
        base_url=settings.CASE_SERVICE_URL,
        timeout_sec=settings.ENVIRONMENT_CONTEXT_TIMEOUT_SEC,
    )
    logger.info(
        event="environment_client_initialized",
        message=f"Environment client 已初始化，目标: {settings.CASE_SERVICE_URL}"
    )

    # 存入 app.state
    app.state.database_manager = database_manager
    app.state.ai_registry = ai_registry
    app.state.scheduler_client = scheduler_client
    app.state.kb_client = kb_client
    app.state.environment_client = environment_client

    # T1-6: 组装大脑路由器（BrainRouter）
    htp_adapter = HTPBrainAdapter(ai_registry=ai_registry, scheduler_client=scheduler_client)
    ops_adapter = None
    if settings.OPS_AGENT_ENABLED:
        ops_adapter = OpsAgentBrainAdapter(base_url=settings.OPS_AGENT_BASE_URL)
    pydantic_ai_adapter = None
    if settings.PYDANTIC_AI_ENABLED:
        from app.adapters.acli_adapter import AcliAdapter
        from app.adapters.pydantic_ai_brain_adapter import PydanticAIBrainAdapter
        from app.adapters.scp_adapter import SCPAdapter
        _scp = SCPAdapter(base_url=settings.SCP_BASE_URL, api_key=settings.SCP_API_KEY)
        _acli = AcliAdapter()
        pydantic_ai_adapter = PydanticAIBrainAdapter.from_env(
            scp_adapter=_scp,
            acli_adapter=_acli,
            kb_client=kb_client,
        )
        logger.info(event="pydantic_ai_adapter_initialized", message="pydantic-ai C 大脑已启用")
    brain_router = BrainRouter(
        htp_adapter=htp_adapter,
        ops_agent_adapter=ops_adapter,
        pydantic_ai_adapter=pydantic_ai_adapter,
        ai_registry=ai_registry,
    )
    app.state.brain_router = brain_router

    # 兼容现有路由注入方式
    conversations.set_dependencies(database_manager, ai_registry, scheduler_client, kb_client, environment_client, router=brain_router)
    evaluate.set_database_manager(database_manager)
    audit_route.set_audit_database_manager(database_manager)

    yield

    logger.info(
        event="service_stopping",
        message=f"Stopping {settings.SERVICE_NAME}"
    )
    if ai_registry:
        await ai_registry.close_all()
    if ops_adapter:
        await ops_adapter.close()
    if pydantic_ai_adapter is not None and hasattr(pydantic_ai_adapter, "aclose"):
        await pydantic_ai_adapter.aclose()
    if database_manager:
        await database_manager.close()

app = FastAPI(
    title="HCI Troubleshoot - Conversation Service",
    description="对话管理服务 (v2.0 多类型AI助手)",
    version="2.0.0",
    lifespan=lifespan
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
            "kb_service": kb_ok,
        },
    }


@app.get("/metrics")
async def metrics():
    """Prometheus 指标抓取端点"""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── J-2：三级探针分级健康端点 ──────────────────────────────────────
@app.get("/health/live")
async def health_live():
    """Liveness 探针：只检查进程存活，不检查外部依赖"""
    return {"status": "alive"}


@app.get("/health/startup")
async def health_startup():
    """Startup 探针：初始化完成（DB 连接已建立）后返回 200"""
    db_manager = getattr(app.state, "database_manager", None)
    if db_manager and not await db_manager.health_check():
        from fastapi import HTTPException
        raise HTTPException(status_code=503, detail="DB 未就绪，仍在初始化")
    return {"status": "started"}


@app.get("/health/ready")
async def health_ready():
    """Readiness 探针：所有依赖就绪时才加入 Service 流量"""
    checks: dict[str, str] = {}

    db_manager = getattr(app.state, "database_manager", None)
    if db_manager:
        checks["database"] = "ok" if await db_manager.health_check() else "unavailable"

    kb_client = getattr(app.state, "kb_client", None)
    if kb_client:
        import httpx
        try:
            async with httpx.AsyncClient(timeout=1.0) as client:
                resp = await client.get(f"{settings.KB_SERVICE_URL}/health")
            checks["kb_service"] = "ok" if resp.status_code == 200 else "degraded"
        except Exception:
            checks["kb_service"] = "unavailable"

    degraded = any(v != "ok" for v in checks.values())
    from fastapi.responses import JSONResponse
    return JSONResponse(
        status_code=503 if degraded else 200,
        content={"status": "degraded" if degraded else "ready", "checks": checks},
    )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=settings.SERVICE_PORT,
        reload=True
    )
