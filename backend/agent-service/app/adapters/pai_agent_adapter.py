"""
PaiAgentAdapter：基于 pydantic-ai 的 C 大脑实现（A/B/C 三向测试）

对接 GLM OpenAI-compatible 端点（通过 OPENCLAW_BASE_URL / OPENCLAW_API_KEY 环境变量）。
用 pydantic-ai Agent 图替代手写 ReAct 循环：
  - @agent.tool 自动从函数签名生成 JSON Schema
  - pydantic-ai 内部透明处理工具调用循环，最大轮次由 UsageLimits.request_limit=15 控制
  - 对外只暴露最终文本流（stream_text(delta=True)）

工具集（Phase 1，只读）：
  - get_sop_tree      — SOP 决策树导航（kb-service）
  - get_active_alerts — HCI 活跃告警查询（SCP API）
  - get_vm_list       — 虚拟机列表查询（SCP API）
  - get_failed_tasks  — 失败任务查询（SCP API）
  - get_cluster_detail — 集群详情查询（SCP API）

Phase 2 扩展（写操作）：DeferredToolRequests（高危工具需要用户确认）
"""

from __future__ import annotations

import dataclasses
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.messages import ModelMessage, ModelRequest, ModelResponse, TextPart, UserPromptPart
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import UsageLimits
from shared.clients import KBClient

from app.adapters.acli_adapter import AcliAdapter
from app.adapters.scp_adapter import SCPAdapter
from app.core.agent_port import AgentEvent, AgentTextChunk, AgentUnavailableError

logger = logging.getLogger("pydantic-ai-brain")
tracer = trace.get_tracer(__name__)

# HCI AI 助手系统提示
_HCI_SYSTEM_PROMPT = """你是 HCI（超融合基础架构）AI 助手，专注于辅助运维工程师诊断和解决 HCI 平台问题。

可用工具：
- get_sop_tree：获取 SOP 标准操作流程决策树（用于按步骤引导故障处理）
- get_active_alerts：查询 HCI 平台当前活跃告警
- get_vm_list：查询虚拟机列表
- get_failed_tasks：查询最近失败的操作任务
- get_cluster_detail：查询集群详情

工作方式：
1. 先查询活跃告警/失败任务，了解当前环境状态
2. 如果能匹配到 SOP 文档，通过 get_sop_tree 获取决策树并按步骤引导
3. 始终基于工具返回的实际数据进行分析，不要凭空假设环境状态
4. 对话请使用中文
"""


@dataclasses.dataclass
class PydanticAIDeps:
    """pydantic-ai Agent 工具的依赖注入（通过 ctx.deps 访问）"""

    kb_client: KBClient | None
    scp_adapter: SCPAdapter
    acli_adapter: AcliAdapter
    env_context: dict[str, Any]


def _build_agent() -> Agent[PydanticAIDeps]:
    """构建 pydantic-ai Agent，注册 HCI 只读工具集"""

    agent: Agent[PydanticAIDeps] = Agent(
        model=None,  # 运行时通过 run_stream(model=...) 传入，允许每个实例使用不同模型
        system_prompt=_HCI_SYSTEM_PROMPT,
        deps_type=PydanticAIDeps,
        usage_limits=UsageLimits(request_limit=15),  # 防止无限 ReAct 循环
        name="hci-pydantic-ai-brain",
    )

    @agent.tool
    async def get_sop_tree(ctx: RunContext[PydanticAIDeps], document_id: int) -> dict:
        """获取 SOP 标准操作流程决策树，用于按步骤引导故障处理。

        树中每个节点包含 prerequisites（前置条件）、diagnosis（判断方法）、
        solution（解决方案）、children（子节点）。
        只有当你已知 SOP 文档 ID 时才调用此工具。

        Args:
            document_id: SOP 文档 ID（从意图识别结果或历史对话中获取）
        """
        if ctx.deps.kb_client is None:
            return {"error": "KB 服务不可用，无法获取 SOP 决策树"}
        result = await ctx.deps.kb_client.get_sop_tree(document_id)
        if result is None:
            return {"error": f"SOP 文档 {document_id} 的决策树不存在或尚未生成"}
        return result

    @agent.tool
    async def get_active_alerts(ctx: RunContext[PydanticAIDeps], limit: int = 10) -> dict:
        """查询 HCI 平台当前活跃告警列表（只读，自动执行）。

        诊断故障时应首先调用此工具，了解当前告警状态。

        Args:
            limit: 返回告警数量，默认 10，最大 50
        """
        return await ctx.deps.scp_adapter.execute("get_active_alerts", {"limit": min(limit, 50)})

    @agent.tool
    async def get_vm_list(
        ctx: RunContext[PydanticAIDeps],
        name_filter: str = "",
        limit: int = 20,
    ) -> dict:
        """查询 HCI 平台虚拟机列表（只读）。

        Args:
            name_filter: 虚拟机名称关键词过滤（可选，空字符串查询全部）
            limit: 返回数量限制，默认 20
        """
        return await ctx.deps.scp_adapter.execute(
            "get_vm_list",
            {"name_filter": name_filter, "limit": limit},
        )

    @agent.tool
    async def get_failed_tasks(
        ctx: RunContext[PydanticAIDeps],
        task_type: str = "",
        limit: int = 10,
    ) -> dict:
        """查询 HCI 平台最近失败的操作任务（只读）。

        Args:
            task_type: 任务类型关键词（如"启动虚拟机"），空字符串查询所有类型
            limit: 返回数量，默认 10
        """
        args: dict[str, Any] = {"limit": limit}
        if task_type:
            args["task_type"] = task_type
        return await ctx.deps.scp_adapter.execute("get_failed_tasks", args)

    @agent.tool
    async def get_cluster_detail(ctx: RunContext[PydanticAIDeps], cluster_id: str) -> dict:
        """查询 HCI 集群详情（只读）。

        Args:
            cluster_id: 集群 ID（UUID 格式）
        """
        return await ctx.deps.scp_adapter.execute(
            "get_cluster_detail",
            {"cluster_id": cluster_id},
        )

    return agent


# 模块级单例（避免每次请求重新构建 Agent 和注册工具）
_AGENT: Agent[PydanticAIDeps] | None = None


def _get_agent() -> Agent[PydanticAIDeps]:
    """获取 pydantic-ai Agent 单例（懒初始化，线程安全由模块加载保证）"""
    global _AGENT
    if _AGENT is None:
        _AGENT = _build_agent()
    return _AGENT


def _openai_messages_to_pydantic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[ModelMessage]]:
    """将 OpenAI 格式的消息列表转换为 pydantic-ai 消息格式。

    规则：
    - 最后一条 user 消息 → user_prompt（作为 run_stream 的第一参数）
    - 之前的 user 消息 → ModelRequest(parts=[UserPromptPart(content=...)])
    - assistant 消息 → ModelResponse(parts=[TextPart(content=...)])
    - system 消息跳过（pydantic-ai 通过 Agent.system_prompt 统一处理）

    Returns:
        (user_prompt, message_history)
          user_prompt: 最后一条用户消息的文本内容（空字符串表示无 user 消息）
          message_history: 历史消息列表（pydantic-ai 格式）
    """
    # 定位最后一条 user 消息的位置
    last_user_idx = -1
    for i, msg in enumerate(messages):
        if msg.get("role") == "user":
            last_user_idx = i

    if last_user_idx == -1:
        # 没有 user 消息，返回空 prompt
        return "", []

    user_prompt = ""
    history: list[ModelMessage] = []

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        # 确保 content 是字符串
        if not isinstance(content, str):
            content = str(content) if content is not None else ""

        if i == last_user_idx:
            # 最后一条 user 消息作为 user_prompt
            user_prompt = content
            continue

        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=content)]))
        # system 消息跳过

    return user_prompt, history


class PaiAgentAdapter:
    """pydantic-ai C 大脑适配器。

    对接 GLM OpenAI-compatible 端点，用 pydantic-ai Agent 图替代手写 ReAct 循环。
    实现 AgentPort 协议，可被 AgentRouter 以 "pydantic-ai" assistant_type 路由。

    工具集（Phase 1）：SOP 决策树导航 + SCP 只读查询（无写操作）
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        scp_adapter: SCPAdapter,
        acli_adapter: AcliAdapter,
        kb_client: KBClient | None = None,
    ) -> None:
        """
        Args:
            base_url: OpenAI-compatible API base URL（例如 GLM 的 http://...）
            api_key: API 密钥
            model: 模型名称（例如 "glm-4-flash"）
            scp_adapter: SCP 平台 API 适配器（注入工具）
            acli_adapter: acli SSH 执行适配器（注入工具，Phase 1 未直接使用）
            kb_client: KB 服务客户端（可选，为 None 时 get_sop_tree 返回错误）
        """
        self._openai_model = OpenAIModel(
            model_name=model,
            base_url=base_url,
            api_key=api_key,
        )
        self._scp = scp_adapter
        self._acli = acli_adapter
        self._kb = kb_client

    @classmethod
    def from_env(
        cls,
        scp_adapter: SCPAdapter,
        acli_adapter: AcliAdapter,
        kb_client: KBClient | None = None,
    ) -> PaiAgentAdapter:
        """从环境变量构造实例（复用 OpenClaw 的 OPENCLAW_BASE_URL / OPENCLAW_API_KEY）。

        GLM_MODEL 默认值为 "glm-4-flash"。
        """
        return cls(
            base_url=os.environ["OPENCLAW_BASE_URL"],
            api_key=os.environ["OPENCLAW_API_KEY"],
            model=os.environ.get("GLM_MODEL", "glm-4-flash"),
            scp_adapter=scp_adapter,
            acli_adapter=acli_adapter,
            kb_client=kb_client,
        )

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        stream: bool = True,
        **_kwargs: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        """调用 pydantic-ai Agent，流式产出 AgentTextChunk。

        工具调用在 pydantic-ai 内部透明处理，对调用方只暴露最终文本增量流。

        Args:
            session_id: 会话 ID（用于链路追踪）
            messages: OpenAI 格式的消息列表
            env_context: 环境上下文（集群 ID、告警级别等），注入工具依赖
            stream: 是否流式输出（当前实现始终流式，此参数保留用于接口兼容）

        Raises:
            AgentUnavailableError: 调用失败时抛出，由 AgentRouter 负责降级
        """
        user_prompt, message_history = _openai_messages_to_pydantic(messages)

        if not user_prompt:
            logger.warning(
                "pydantic-ai brain: 没有 user 消息，session_id=%s",
                session_id,
            )
            yield AgentTextChunk(content="[系统提示] 未收到有效的用户消息。")
            return

        deps = PydanticAIDeps(
            kb_client=self._kb,
            scp_adapter=self._scp,
            acli_adapter=self._acli,
            env_context=env_context or {},
        )

        agent = _get_agent()

        with tracer.start_as_current_span("pydantic-ai-brain-process") as span:
            span.set_attribute("session_id", session_id)
            span.set_attribute("user_prompt_len", len(user_prompt))
            span.set_attribute("history_len", len(message_history))

            try:
                async with agent.run_stream(
                    user_prompt,
                    message_history=message_history,
                    model=self._openai_model,
                    deps=deps,
                ) as streamed:
                    async for text in streamed.stream_text(delta=True):
                        if text:
                            yield AgentTextChunk(content=text)

            except Exception as exc:
                logger.exception(
                    "pydantic-ai brain 执行异常 session_id=%s error=%s",
                    session_id,
                    exc,
                )
                raise AgentUnavailableError(
                    agent_name="pydantic-ai",
                    reason=str(exc),
                ) from exc
