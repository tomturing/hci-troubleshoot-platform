"""
Agent Service - 主应用

负责 AI 推理引擎的独立微服务：
- AgentRouter（大脑路由器）
- TriageAgent（S0 意图识别）
- InvestigationAgent（S1-S4 诊断调查）
- RemediationAgent（S5 修复执行）
- OpsAgentAdapter（ops-agent B 大脑，ACP 协议）
- PaiAgentAdapter（pydantic-ai C 大脑，原生 Agent）
- HTTP SSE 端点：POST /v1/agent/stream

从 conversation-service 拆分，遵循单一职责原则：
  conversation-service = 对话状态管理
  agent-service         = AI 推理引擎 + 知识检索

架构设计（v3.0）：
  - S0（意图识别）→ TriageAgent（继承 BaseAgent）
  - S1-S4（诊断调查）→ InvestigationAgent（继承 BaseAgent）
  - S5（修复执行）→ RemediationAgent（继承 BaseAgent）
"""

from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any

from fastapi import FastAPI
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from redis.asyncio import Redis
from shared.clients import AIAssistantRegistry, KBClient, create_openclaw_client
from shared.database.postgres import DatabaseManager
from shared.database.redis import RedisManager
from shared.observability.logger import get_logger
from shared.observability.metrics import HTTPMetricsMiddleware
from shared.observability.otel import init_telemetry, instrument_app
from shared.utils.exception_handlers import register_exception_handlers

from app.adapters.agents.agent_router import AgentRouter
from app.adapters.agents.htp.confirm_service import ConfirmService
from app.adapters.agents.htp.investigation_agent import InvestigationAgent  # T-AGT-11：主用 S1-S4
from app.adapters.agents.htp.react_engine import ReactEngine
from app.adapters.agents.htp.remediation_agent import RemediationAgent  # T-AGT-12：S5 修复执行
from app.adapters.agents.htp.triage_agent import TriageAgent  # T-AGT-10：S0 意图识别
from app.adapters.agents.ops.ops_agent_adapter import OpsAgentAdapter
from app.config import settings
from app.routes.agent import router as agent_router_route
from app.routes.agent import set_agent_router, set_confirm_service

if TYPE_CHECKING:
    from app.adapters.clients.acli_client import AcliClient
    from app.adapters.clients.scp_client import SCPClient
    from app.tools.base_tool import ToolDefinition

from app.tools.acli.executor import BridgeRelayExecutor  # T-TOOL-16：acli category 路由

# 在应用创建前初始化 OpenTelemetry
init_telemetry(settings.SERVICE_NAME)

logger = get_logger(settings.SERVICE_NAME, settings.LOG_LEVEL)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """应用生命周期管理"""
    # ── 编译 SQLAlchemy 所有模型，拦截任何 NoReferencedTableError 外键元数据配置错误 ────
    try:
        from shared.models.audit import AuditLog  # noqa: F401
        from shared.models.conversation import Conversation  # noqa: F401
        from shared.models.system_prompt import SystemPrompt  # noqa: F401
        from sqlalchemy.orm import configure_mappers

        configure_mappers()
        logger.info("SQLAlchemy mappers 编译配置成功，外键检查通过")
    except Exception as e:
        logger.critical(f"SQLAlchemy mappers 编译失败，发现外键或元数据配置错误: {e}", exc_info=True)
        raise e

    logger.info(
        event="service_starting",
        message=f"Starting {settings.SERVICE_NAME}",
        port=settings.SERVICE_PORT,
    )

    # ── 数据库连接（用于工具注册表加载）──────────────────────────────────────────────
    db_manager = DatabaseManager(settings.DATABASE_URL)

    # ── AI 助手注册表 ──────────────────────────────────────────────────────────
    from app.services.prompt_audit import PromptAuditService

    PromptAuditService.initialize(db_manager.async_session_factory)

    async def prompt_audit_cb(
        conversation_id: str, assistant_type: str, messages: list[dict[str, Any]], case_id: str = ""
    ) -> None:
        await PromptAuditService.write_prompt_audit(
            conversation_id=conversation_id,
            assistant_type=assistant_type,
            messages=messages,
            case_id=case_id,
        )

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
        # 注入全量原始 prompt 审计回调
        client.prompt_audit_callback = prompt_audit_cb
        is_default = bool(cfg.get("is_default", False))
        ai_registry.register(assistant_type, client, is_default=is_default)

    logger.info(
        event="ai_registry_initialized",
        message=f"Registered AI assistants: {ai_registry.list_types()}",
    )

    # ── KB 客户端（TriageAgent + InvestigationAgent 使用）──────────────────────────
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

    # ── Redis（confirm_service + BridgeRelayExecutor 使用）───────────────────────
    redis_client: Redis | None = None
    redis_manager: RedisManager | None = None
    try:
        redis_client = Redis.from_url(settings.REDIS_URL, decode_responses=True)
        await redis_client.ping()
        logger.info(event="redis_connected", message="Redis 连接成功")

        # 为 BridgeRelayExecutor 创建 RedisManager 实例
        redis_manager = RedisManager(settings.REDIS_URL)
        await redis_manager.connect()
        logger.info(event="redis_manager_connected", message="RedisManager 已连接")
    except Exception as exc:
        logger.warning(
            event="redis_unavailable",
            message=f"Redis 不可达，ReAct 确认功能降级: {exc}",
        )
        redis_client = None

    # ── TriageAgent（S0 意图识别）──────────────────────────────────────────────────
    # T-AGT-10：TriageAgent 替换 IntentAgent（继承 BaseAgent）
    triage_agent = TriageAgent(
        ai_registry=ai_registry,
        kb_client=kb_client,
        db_session_factory=db_manager.async_session_factory,
    )

    # ── 工具执行器（S1-S5 阶段共用）─────────────────────────────────────────────
    # 先实例化 ReactEngine（可选）
    react_engine: ReactEngine | None = None
    # ── 实例化工具执行客户端（优先从环境变量加载，无论是否启用 REACT，保证 S1-S4 诊断可用） ────
    from app.adapters.clients.acli_client import AcliClient
    from app.adapters.clients.scp_client import SCPClient

    try:
        scp_client = SCPClient.from_env()
        logger.info("SCP 客户端从环境变量初始化成功")
    except Exception as e:
        logger.warning(f"SCP 客户端初始化跳过或失败 (可能是缺少 SCP_BASE_URL 环境变量): {e}")
        scp_client = None

    try:
        acli_client = AcliClient.from_env()
        logger.info("Acli 客户端从环境变量初始化成功")
    except Exception as e:
        logger.warning(f"Acli 客户端初始化失败: {e}")
        acli_client = None
    tool_executor = None  # 确保变量存在，供 InvestigationAgent 使用
    confirm_service: ConfirmService | None = None
    from app.services.tool_audit import DbAuditService, ToolAuditService

    ToolAuditService.initialize(db_manager.async_session_factory)
    audit_service = DbAuditService()

    # ── 加载工具注册表（从数据库）──────────────────────────────────────────────────────
    # 无论是否启用 REACT，都需要加载工具注册表（InvestigationAgent 等也依赖）
    from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY, load_tool_registry

    # 使用 session factory 创建 session（get_session 是 async generator，不能用 async with）
    async with db_manager.async_session_factory() as db_session:
        loaded_registry = await load_tool_registry(db_session)
        TOOL_REGISTRY.update(loaded_registry)

    if settings.REACT_ENABLED:
        # 实例化确认服务（依赖 Redis）
        if redis_client is not None:
            confirm_service = ConfirmService(redis=redis_client)

        # 实例化复合工具执行器（合并 SCP、Acli 和 SOP）
        # T-TOOL-16：添加 BridgeRelayExecutor 参数
        tool_executor = CompositeToolExecutor(
            scp=scp_client,
            acli=acli_client,
            kb_client=kb_client,
            redis_manager=redis_manager,
            conversation_service_url=settings.CONVERSATION_SERVICE_URL,
            internal_token=settings.INTERNAL_API_TOKEN,
        )

        # 实例化 ReactEngine
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

    # ── InvestigationAgent（S1-S4 诊断调查）──────────────────────────────────────────────
    # T-AGT-11：主用 S1-S4 阶段，继承 BaseAgent
    # 使用已有的 tool_executor（如果 REACT_ENABLED），否则创建新的
    investigation_tool_executor = tool_executor or CompositeToolExecutor(
        scp=scp_client,
        acli=acli_client,
        kb_client=kb_client,
        redis_manager=redis_manager,
        conversation_service_url=settings.CONVERSATION_SERVICE_URL,
        internal_token=settings.INTERNAL_API_TOKEN,
    )
    investigation_agent = InvestigationAgent(
        ai_registry=ai_registry,
        kb_client=kb_client,
        tool_executor=investigation_tool_executor,
        conversation_service_url=settings.CONVERSATION_SERVICE_URL,  # T-AGT-22
        internal_token=settings.INTERNAL_API_TOKEN,  # T-AGT-22
        top_k=15,
        confirm_service=confirm_service,
        audit_service=audit_service,
        db_session_factory=db_manager.async_session_factory,
    )
    logger.info(
        event="investigation_agent_initialized",
        conversation_service_url=settings.CONVERSATION_SERVICE_URL,
        message="InvestigationAgent 已初始化（S1-S4，支持 SOP ReactEngine）",
    )

    # ── RemediationAgent（S5 修复执行）────────────────────────────────────────────────
    # T-AGT-12：require_all_confirm=True，所有工具调用均需用户确认
    remediation_agent: RemediationAgent | None = None
    if react_engine is not None:
        remediation_agent = RemediationAgent(
            ai_registry=ai_registry,
            kb_client=kb_client,
            react_engine=react_engine,
            db_session_factory=db_manager.async_session_factory,
        )
        logger.info(
            event="remediation_agent_initialized",
            message="RemediationAgent 已初始化（S5 修复执行，require_all_confirm=True）",
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
        from app.adapters.clients.scp_client import SCPClient

        _acli = AcliClient.from_env()
        # 复用 ReAct 块创建的 scp_client（如果已初始化），否则新建
        _scp = scp_client if scp_client is not None else SCPClient.from_env()
        pai_adapter = PaiAgentAdapter.from_env(
            scp_client=_scp,
            acli_client=_acli,
            kb_client=kb_client,
        )

    # ── 组装 AgentRouter ────────────────────────────────────────────────────────────
    # T-AGT-10：TriageAgent 处理 S0 意图识别
    # T-AGT-11：InvestigationAgent 处理 S1-S4 诊断调查
    # T-AGT-12：RemediationAgent 处理 S5 修复执行
    agent_router = AgentRouter(
        triage_agent=triage_agent,
        investigation_agent=investigation_agent,
        remediation_agent=remediation_agent,
        ops_agent_adapter=ops_adapter,
        pai_adapter=pai_adapter,
        ai_registry=ai_registry,
    )

    # 注入路由模块
    set_agent_router(agent_router)
    # 注入 ConfirmService（用于 ReAct 确认回路）
    set_confirm_service(confirm_service)

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
    if redis_manager:
        await redis_manager.close()
    await db_manager.close()
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
    """复合工具执行器：合并 SCP、Acli 和 SOP 客户端

    实现 ToolExecutor Protocol，根据工具 category 分发到对应客户端。

    T-TOOL-16：新增 acli category 路由，所有 acli/bash 工具通过 BridgeRelayExecutor 执行。
    """

    def __init__(
        self,
        scp: "SCPClient",
        acli: "AcliClient",
        kb_client: KBClient | None = None,
        redis_manager: RedisManager | None = None,
        conversation_service_url: str | None = None,
        internal_token: str | None = None,
        conversation_id: str | None = None,
    ) -> None:
        self._scp = scp
        self._acli = acli
        self._kb_client = kb_client
        self._conversation_id = conversation_id

        # T-TOOL-16：实例化 BridgeRelayExecutor（用于 acli category）
        if redis_manager and conversation_service_url and internal_token:
            self._bridge_executor = BridgeRelayExecutor(
                redis=redis_manager,
                conversation_service_url=conversation_service_url,
                internal_token=internal_token,
            )
        else:
            self._bridge_executor = None

    async def execute(
        self,
        tool_name: str,
        args: dict,
        *,
        tool_def: "ToolDefinition | None" = None,
        conversation_id: str | None = None,
        **kwargs: Any,
    ) -> Any:
        """执行工具调用，根据工具类型分发

        Args:
            tool_name: 工具名称
            args: 工具参数
            tool_def: 工具定义（可选，如未传入则从 TOOL_REGISTRY 获取）
            conversation_id: 会话 ID (可选)
            **kwargs: 额外透传参数（如 exec_id）

        Returns:
            执行结果（字符串或字典）
        """
        from app.adapters.agents.htp.sop_tools import get_sop_node
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY

        # 如果未传入 tool_def，则从 TOOL_REGISTRY 获取
        if tool_def is None:
            tool_def = TOOL_REGISTRY.get(tool_name)
        if not tool_def:
            return {"error": f"未知工具: {tool_name}"}

        # 如果未传入 conversation_id，则使用初始化时的值
        effective_conversation_id = conversation_id or self._conversation_id

        if tool_def.category == "scp":
            if self._scp is None:
                return {"error": "SCPClient 客户端未初始化，请检查 SCP_BASE_URL 环境变量是否已正确配置"}
            return await self._scp.execute(tool_name, args)
        elif tool_def.category == "acli":
            # T-TOOL-16：acli category 路由到 BridgeRelayExecutor
            # bash_exec 和 acli_exec 及插件工具均走此路径
            if self._bridge_executor is None:
                return {"error": "BridgeRelayExecutor 未初始化，无法执行 acli 工具"}
            if effective_conversation_id is None:
                return {"error": "缺少 conversation_id，无法执行 acli 工具"}

            result = await self._bridge_executor.execute(
                tool_name,
                args,
                conversation_id=effective_conversation_id,
                node_ip=args.get("node_ip"),
                risk_level=tool_def.risk_level,
                policy=tool_def.policy,
                usage_template=tool_def.usage_template,
                **kwargs,
            )

            # 返回 stdout 或错误信息
            return result.stdout or f"[exit_code={result.exit_code}]"
        elif tool_def.category == "sop":
            # SOP 工具需要 kb_client 和 sop_document_id
            if tool_name == "get_sop_node":
                if self._kb_client is None:
                    return {"error": "KB 客户端未初始化，无法执行 SOP 工具"}
                sop_document_id = args.get("sop_document_id")
                if not sop_document_id:
                    return {"error": "get_sop_node 工具缺少 sop_document_id 参数"}
                return await get_sop_node(
                    node_id=args.get("node_id", ""),
                    sop_document_id=int(sop_document_id),
                    kb_client=self._kb_client,
                )
            else:
                return {"error": f"SOP 工具 {tool_name} 未实现"}
        else:
            return {"error": f"工具类别 {tool_def.category} 无对应执行器"}


# ── FastAPI 应用 ────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="HCI Agent Service",
    description="AI 推理引擎微服务（v4.3：TriageAgent + InvestigationAgent + RemediationAgent）",
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
