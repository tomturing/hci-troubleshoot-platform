"""
Conversation Service - 主应用 (v2.0 多类型AI助手)

变更记录:
- 使用 app.state 替代全局变量进行依赖注入
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from shared.database.postgres import DatabaseManager
from shared.utils.logger import get_logger
from shared.utils.otel import init_telemetry, instrument_app

from app.config import settings
from app.routes import conversations
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
        model = cfg.get("model", assistant_type)
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

    # 兼容现有路由注入方式
    conversations.set_dependencies(database_manager, ai_registry, scheduler_client, kb_client)

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

# 注册路由
app.include_router(conversations.router)


@app.get("/health")
async def health_check():
    """健康检查"""
    ai_status = {}

    registry = getattr(app.state, "ai_registry", None)
    if registry:
        ai_status = await registry.health_check_all()

    return {
        "status": "healthy",
        "service": settings.SERVICE_NAME,
        "version": "2.0.0",
        "dependencies": {"ai_assistants": ai_status},
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run("main:app", host="0.0.0.0", port=settings.SERVICE_PORT, reload=True)
