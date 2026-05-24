"""
Agent Service - 主应用

负责 AI 推理引擎的独立微服务：
- AgentRouter（大脑路由器）
- IntentAgent（S0 意图识别）
- DiagnosticAgent（S1+ 诊断推理）
- OpsAgentAdapter（ops-agent B 大脑，ACP 协议）
- PaiAgentAdapter（pydantic-ai C 大脑，原生 Agent）
- HTTP SSE 端点：POST /v1/agent/stream

从 conversation-service 拆分，遵循单一职责原则：
  conversation-service = 对话状态管理
  agent-service         = AI 推理引擎 + 知识检索

架构设计（v3.0）：
  - S0（意图识别）→ IntentAgent（注入分类列表 + 环境上下文）
  - S1+（诊断推理）→ DiagnosticAgent（三轨路由：SOP/KBD/机制推理）
  - ReactEngine → 工具调用循环（可选，execution_mode=react）
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from shared.clients import AIAssistantRegistry, KBClient, create_openclaw_client
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

from app.adapters.agents.agent_router import AgentRouter
from app.adapters.agents.htp.confirm_service import ConfirmService
from app.adapters.agents.htp.diagnostic_agent import DiagnosticAgent
from app.adapters.agents.htp.intent_agent import IntentAgent
from app.adapters.agents.htp.react_engine import ReactEngine
from app.adapters.agents.ops.ops_agent_adapter import OpsAgentAdapter
from app.config import settings
from app.routes.agent import router as agent_router_route
from app.routes.agent import set_agent_router

if TYPE_CHECKING:
    from app.adapters.clients.acli_client import AcliClient
    from app.adapters.clients.scp_client import SCPClient

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
        base_url = cfg.get("base_url", settings.LLM_BASE_URL)
        api_key = cfg.get("api_key") or cfg.get("provider_api_key") or settings.LLM_API_KEY
        model = cfg.get("model", assistant_type)
        client = create_openclaw_client(
            base_url=base_url,
            api_key=api_key,
            provider_api_key=None,
            default_model=model,
            assistant_type=assistant_type,
        )
        is_default = bool(cfg.get("is_default", False))
        ai_registry.register(assistant_type, client, is_default=is_default)

    logger.info(
        event="ai_registry_initialized",
        message=f"Registered AI assistants: {ai_registry.list_types()}",
    )

    # ── KB 客户端（IntentAgent + DiagnosticAgent 使用）──────────────────────────
    kb_client: KBClient | None = None
    if settings.KB_ENABLED:
        kb_client = KBClient(
            kb_service_url=settings.KB_SERVICE_URL,
            internal_token=settings.INTERNAL_API_TOKEN,
        )
        logger.info(
            event="kb_client_initialized",
            message="KB 客户端已初始化",
            kb_service_url=settings.KB_SERVICE_URL,
        )

    # ── Redis（confirm_service 使用）─────────────────────────────────────────────
    redis_client: Redis | None = None
    try:
        redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info(event="redis_connected", message="Redis 连接成功")
    except Exception as exc:
        logger.warning(
            event="redis_unavailable",
            message=f"Redis 不可达，ReAct 确认功能降级: {exc}",
        )
        redis_client = None

    # ── IntentAgent（S0 意图识别）──────────────────────────────────────────────────
    intent_agent = IntentAgent(
        ai_registry=ai_registry,
        kb_client=kb_client,
    )

    # ── DiagnosticAgent（S1+ 诊断推理）─────────────────────────────────────────────
    # 先实例化 ReactEngine（可选）
    react_engine: ReactEngine | None = None
    if settings.REACT_ENABLED:
        # 实例化工具执行客户端
        from app.adapters.clients.acli_client import AcliClient
        from app.adapters.clients.scp_client import SCPClient

        scp_client = SCPClient.from_env()
        acli_client = AcliClient.from_env()

        # 实例化确认服务（依赖 Redis）
        confirm_service: ConfirmService | None = None
        if redis_client is not None:
            confirm_service = ConfirmService(redis=redis_client)

        # 实例化审计服务（简单文件日志实现）
        audit_service = FileAuditService()

        # 实例化复合工具执行器（合并 SCP 和 Acli）
        tool_executor = CompositeToolExecutor(
            scp=scp_client,
            acli=acli_client,
        )

        # 实例化 ReactEngine
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY
        react_engine = ReactEngine(
            ai_registry=ai_registry,
            tool_registry=TOOL_REGISTRY,
            tool_executor=tool_executor,
            confirm_service=confirm_service,
            audit_service=audit_service,
        )

        logger.info(
            event="react_engine_initialized",
            message="ReAct 引擎已初始化",
        )

    # 实例化 DiagnosticAgent
    diagnostic_agent = DiagnosticAgent(
        ai_registry=ai_registry,
        kb_client=kb_client,
        react_engine=react_engine,
    )

    # ── OpsAgent 适配器（可选）─────────────────────────────────────────────────────
    ops_adapter: OpsAgentAdapter | None = None
    if settings.OPS_AGENT_ENABLED:
        ops_adapter = OpsAgentAdapter(base_url=settings.OPS_AGENT_BASE_URL)

    # ── PaiAgent 适配器（可选）─────────────────────────────────────────────────────
    pai_adapter = None
    if settings.PYDANTIC_AI_ENABLED:
        from app.adapters.agents.pai.pai_agent_adapter import PaiAgentAdapter
        from app.adapters.clients.acli_client import AcliClient

        _acli = AcliClient.from_env()
        pai_adapter = PaiAgentAdapter.from_env(
            scp_client=None,
            acli_client=_acli,
            kb_client=kb_client,
        )

    # ── 组装 AgentRouter ────────────────────────────────────────────────────────────
    agent_router = AgentRouter(
        intent_agent=intent_agent,
        diagnostic_agent=diagnostic_agent,
        ops_agent_adapter=ops_adapter,
        pai_adapter=pai_adapter,
        ai_registry=ai_registry,
    )

    # 注入路由模块
    set_agent_router(agent_router)

    logger.info(
        event="agent_router_initialized",
        message="AgentRouter 初始化完成（v3.0 架构）",
        ops_enabled=settings.OPS_AGENT_ENABLED,
        pai_enabled=settings.PYDANTIC_AI_ENABLED,
        react_enabled=settings.REACT_ENABLED,
        kb_enabled=settings.KB_ENABLED,
    )

    yield

    # ── 清理 ────────────────────────────────────────────────────────────────────────
    if redis_client:
        await redis_client.aclose()
    logger.info(event="service_stopped", message=f"{settings.SERVICE_NAME} 已停止")


# ── 辅助类：审计服务（简单文件日志实现）─────────────────────────────────────────────


class FileAuditService:
    """简单的文件审计服务（ReAct 工具执行日志）

    生产环境应替换为：
    - 写入数据库（audit_logs 表）
    - 写入 Loki 日志系统
    """

    async def write(self, audit_id: str, **kwargs) -> None:
        """写入审计日志"""
        logger.info(
            f"Audit: id={audit_id} session={kwargs.get('session_id')} "
            f"tool={kwargs.get('tool_name')} risk={kwargs.get('risk_level')} "
            f"duration={kwargs.get('duration_ms')}ms "
            f"authorized_by={kwargs.get('authorized_by', 'N/A')}"
        )


class CompositeToolExecutor:
    """复合工具执行器：合并 SCP 和 Acli 客户端

    实现 ToolExecutor Protocol，根据工具 category 分发到对应客户端。
    """

    def __init__(
        self,
        scp: "SCPClient",
        acli: "AcliClient",
    ) -> None:
        self._scp = scp
        self._acli = acli

    async def execute(self, tool_name: str, args: dict) -> any:
        """执行工具调用，根据工具类型分发"""
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY

        tool_def = TOOL_REGISTRY.get(tool_name)
        if not tool_def:
            return {"error": f"未知工具: {tool_name}"}

        if tool_def.category == "scp":
            return await self._scp.execute(tool_name, args)
        elif tool_def.category == "acli":
            return await self._acli.execute(tool_name, args)
        else:
            return {"error": f"工具类别 {tool_def.category} 无对应执行器"}


# ── FastAPI 应用 ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="HCI Agent Service",
    description="AI 推理引擎微服务（v3.0：IntentAgent + DiagnosticAgent）",
    version="3.0.0",
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
