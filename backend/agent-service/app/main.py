"""
Agent Service - 主应用

负责 AI 推理引擎的独立微服务：
- AgentRouter（大脑路由）
- HTPAgentAdapter（原有 HTP 大脑，S0-S6 阶段推理）
- OpsAgentAdapter（ops-agent B 大脑，ACP 协议）
- PaiAgentAdapter（pydantic-ai C 大脑，原生 Agent）
- HTTP SSE 端点：POST /v1/agent/stream

从 conversation-service 拆分，遵循单一职责原则：
  conversation-service = 对话状态管理
  agent-service         = AI 推理引擎
"""

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from shared.clients import AIAssistantRegistry, KBClient, SchedulerClient, create_openclaw_client
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

from app.adapters.agent_router import AgentRouter
from app.adapters.htp_agent_adapter import HTPAgentAdapter
from app.adapters.ops_agent_adapter import OpsAgentAdapter
from app.config import settings
from app.routes.agent import router as agent_router_route
from app.routes.agent import set_agent_router
from app.services.confirm_service import ConfirmService

# 在应用创建前初始化 OpenTelemetry
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

    # ── AI 助手注册表 ──────────────────────────────────────────────────────────
    ai_registry = AIAssistantRegistry()
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
        message=f"Registered AI assistants: {ai_registry.list_types()}",
    )

    # ── 调度客户端（HTP 大脑使用）────────────────────────────────────────────
    scheduler_client = SchedulerClient(settings.SCHEDULER_SERVICE_URL)

    # ── KB 客户端（可选）─────────────────────────────────────────────────────
    kb_client: KBClient | None = None
    if settings.KB_ENABLED:
        kb_client = KBClient(
            kb_service_url=settings.KB_SERVICE_URL,
            internal_token=settings.INTERNAL_API_TOKEN,
        )

    # ── Redis（confirm_service / dialog_tools）───────────────────────────────
    redis_client: Redis | None = None
    try:
        redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info(event="redis_connected", message="Redis 连接成功")
    except Exception as exc:
        logger.warning(
            event="redis_unavailable",
            message=f"Redis 不可达，REACT 相关功能降级: {exc}",
        )
        redis_client = None

    # ── ConfirmService（ReAct 人工确认，依赖 Redis）──────────────────────────
    confirm_service: ConfirmService | None = None
    if redis_client is not None:
        confirm_service = ConfirmService(  # noqa: F841
            redis=redis_client,
        )

    # ── HTP 大脑适配器 ────────────────────────────────────────────────────────
    htp_adapter = HTPAgentAdapter(
        ai_registry=ai_registry,
        scheduler_client=scheduler_client,
    )

    # ── OpsAgent 适配器（可选）───────────────────────────────────────────────
    ops_adapter: OpsAgentAdapter | None = None
    if settings.OPS_AGENT_ENABLED:
        ops_adapter = OpsAgentAdapter(base_url=settings.OPS_AGENT_BASE_URL)

    # ── PaiAgent 适配器（可选）───────────────────────────────────────────────
    pai_adapter = None
    if settings.PYDANTIC_AI_ENABLED:
        from app.adapters.acli_adapter import AcliAdapter
        from app.adapters.pai_agent_adapter import PaiAgentAdapter
        from app.adapters.scp_adapter import SCPAdapter

        _scp = SCPAdapter(base_url=settings.SCP_BASE_URL, api_key=settings.SCP_API_KEY)
        _acli = AcliAdapter.from_env()
        pai_adapter = PaiAgentAdapter.from_env(
            scp_adapter=_scp,
            acli_adapter=_acli,
            kb_client=kb_client,
        )

    # ── 组装 AgentRouter ──────────────────────────────────────────────────────
    agent_router = AgentRouter(
        htp_adapter=htp_adapter,
        ops_agent_adapter=ops_adapter,
        pai_adapter=pai_adapter,
        ai_registry=ai_registry,
    )

    # 注入路由模块
    set_agent_router(agent_router)

    logger.info(
        event="agent_router_initialized",
        message="AgentRouter 初始化完成",
        ops_enabled=settings.OPS_AGENT_ENABLED,
        pai_enabled=settings.PYDANTIC_AI_ENABLED,
        react_enabled=settings.REACT_ENABLED,
    )

    yield

    # ── 清理 ──────────────────────────────────────────────────────────────────
    if redis_client:
        await redis_client.aclose()
    logger.info(event="service_stopped", message=f"{settings.SERVICE_NAME} 已停止")


# ── FastAPI 应用 ──────────────────────────────────────────────────────────────

app = FastAPI(
    title="HCI Agent Service",
    description="AI 推理引擎微服务（从 conversation-service 拆分）",
    version="1.0.0",
    lifespan=lifespan,
)

instrument_app(app)
app.add_middleware(HTTPMetricsMiddleware)
register_exception_handlers(app)

# 路由挂载
app.include_router(agent_router_route)


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
