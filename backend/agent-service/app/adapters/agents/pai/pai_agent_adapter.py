"""
PaiAgentAdapter：基于 pydantic-ai 的 C 大脑实现（A/B/C 三向测试）

对接 GLM OpenAI-compatible 端点（通过 LLM_BASE_URL / LLM_API_KEY 环境变量）。
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

import asyncio
import contextlib
import dataclasses
import logging
import os
from collections.abc import AsyncGenerator
from typing import Any

from opentelemetry import trace
from pydantic_ai import Agent
from pydantic_ai.messages import (
    AgentStreamEvent,
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelMessage,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.openai import OpenAIModel
from pydantic_ai.tools import RunContext
from pydantic_ai.usage import UsageLimits
from shared.clients import KBClient

from app.adapters.clients.acli_client import AcliClient
from app.adapters.clients.scp_client import SCPClient
from app.domain.agent_port import (
    AgentEvent,
    AgentStageUpdate,
    AgentTextChunk,
    AgentUnavailableError,
)

logger = logging.getLogger("pydantic-ai-brain")
tracer = trace.get_tracer(__name__)

# HCI AI 助手系统提示（基础模板，运行时注入 category_id）
_HCI_SYSTEM_PROMPT_BASE = """你是 HCI（超融合基础架构）AI 助手，专注于辅助运维工程师诊断和解决 HCI 平台问题。

可用工具：
- search_sop_by_category：根据故障分类编码获取 SOP 文档 ID（优先使用此工具）
- get_sop_tree：获取 SOP 标准操作流程决策树（用于按步骤引导故障处理）
- get_active_alerts：查询 HCI 平台当前活跃告警
- get_vm_list：查询虚拟机列表
- get_failed_tasks：查询最近失败的操作任务
- get_cluster_detail：查询集群详情

工作方式：
1. 如果已知故障分类编码（category_code），优先调用 search_sop_by_category 获取 SOP 文档 ID
2. 获取到 document_id 后，调用 get_sop_tree 获取决策树并按步骤引导
3. 如果没有 SOP 文档，先查询活跃告警/失败任务，了解当前环境状态
4. 始终基于工具返回的实际数据进行分析，不要凭空假设环境状态
5. 对话请使用中文
"""

_HCI_SYSTEM_PROMPT_WITH_CATEGORY = """你是 HCI（超融合基础架构）AI 助手，专注于辅助运维工程师诊断和解决 HCI 平台问题。

当前故障分类：{category_code}（已确认）

可用工具：
- search_sop_by_category：根据故障分类编码获取 SOP 文档 ID（使用当前分类编码）
- get_sop_tree：获取 SOP 标准操作流程决策树（用于按步骤引导故障处理）
- get_active_alerts：查询 HCI 平台当前活跃告警
- get_vm_list：查询虚拟机列表
- get_failed_tasks：查询最近失败的操作任务
- get_cluster_detail：查询集群详情

工作方式：
1. 已确认故障分类为 {category_code}，优先调用 search_sop_by_category("{category_code}") 获取 SOP 文档
2. 获取到 document_id 后，调用 get_sop_tree 获取决策树并按步骤引导
3. 如果没有 SOP 文档，查询活跃告警/失败任务，了解当前环境状态
4. 始终基于工具返回的实际数据进行分析，不要凭空假设环境状态
5. 对话请使用中文
"""


@dataclasses.dataclass
class PydanticAIDeps:
    """pydantic-ai Agent 工具的依赖注入（通过 ctx.deps 访问）"""

    kb_client: KBClient | None
    scp_client: SCPClient
    acli_client: AcliClient
    env_context: dict[str, Any]
    category_id: str | None = None  # S0 确认的故障分类编码
    # 工具调用事件队列（用于 T-AGT-14 可观测性）
    tool_event_queue: asyncio.Queue[AgentStageUpdate] = dataclasses.field(default_factory=lambda: asyncio.Queue())


def _build_agent() -> Agent[PydanticAIDeps]:
    """构建 pydantic-ai Agent，注册 HCI 只读工具集"""

    def dynamic_system_prompt(ctx: RunContext[PydanticAIDeps]) -> str:
        """动态生成 system prompt，根据是否有 category_id 选择不同模板"""
        if ctx.deps.category_id:
            return _HCI_SYSTEM_PROMPT_WITH_CATEGORY.format(category_code=ctx.deps.category_id)
        return _HCI_SYSTEM_PROMPT_BASE

    agent: Agent[PydanticAIDeps] = Agent(
        model=None,  # 运行时通过 run_stream(model=...) 传入，允许每个实例使用不同模型
        system_prompt=dynamic_system_prompt,
        deps_type=PydanticAIDeps,
        usage_limits=UsageLimits(request_limit=15),  # 防止无限 ReAct 循环
        name="hci-pydantic-ai-brain",
    )

    @agent.tool
    async def search_sop_by_category(
        ctx: RunContext[PydanticAIDeps],
        category_code: str,
        query: str = "",
    ) -> dict:
        """根据故障分类编码获取 SOP 文档 ID。

        这是获取 SOP 的第一步：先通过分类编码路由到对应的 SOP 文档。
        返回结果包含 track（sop/kbd/human_escalation）和 results 列表。
        如果 track 为 sop，results 中会包含 document_id。

        Args:
            category_code: 故障分类编码，如 "虚拟机-003"。如果已知当前分类，可直接使用。
            query: 用户问题描述（可选，用于语义相关性排序）
        """
        if ctx.deps.kb_client is None:
            return {"error": "KB 服务不可用，无法搜索 SOP 文档"}

        # 如果未传入 category_code，尝试使用 deps 中的 category_id
        if not category_code and ctx.deps.category_id:
            category_code = ctx.deps.category_id

        if not category_code:
            return {"error": "缺少故障分类编码（category_code），无法搜索 SOP"}

        result = await ctx.deps.kb_client.route_by_category(
            category_code=category_code,
            query=query,
            top_k=5,
        )
        if result is None:
            return {"error": f"分类 {category_code} 未匹配到任何知识内容"}

        # 提取 SOP 文档信息
        track = result.get("track", "")
        results = result.get("results", [])

        if track == "sop" and results:
            # 返回 SOP 文档列表，包含 document_id
            sop_docs = []
            for item in results:
                sop_docs.append(
                    {
                        "document_id": item.get("id"),
                        "title": item.get("title"),
                        "content_md": item.get("content_md", "")[:200],  # 截取前 200 字符
                    }
                )
            return {"track": "sop", "sop_documents": sop_docs}
        elif track == "kbd":
            return {"track": "kbd", "message": "该分类匹配到历史案例（KBD），无 SOP 文档", "results": results[:3]}
        elif track == "human_escalation":
            return {"track": "human_escalation", "message": "该分类需要人工介入，无 SOP 文档"}
        else:
            return {"track": track, "message": "未找到相关 SOP 文档", "results": results}

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
        return await ctx.deps.scp_client.execute("get_active_alerts", {"limit": min(limit, 50)})

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
        return await ctx.deps.scp_client.execute(
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
        return await ctx.deps.scp_client.execute("get_failed_tasks", args)

    @agent.tool
    async def get_cluster_detail(ctx: RunContext[PydanticAIDeps], cluster_id: str) -> dict:
        """查询 HCI 集群详情（只读）。

        Args:
            cluster_id: 集群 ID（UUID 格式）
        """
        return await ctx.deps.scp_client.execute(
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


def _create_event_stream_handler(
    tool_event_queue: asyncio.Queue[AgentStageUpdate],
) -> callable:
    """创建 event_stream_handler 用于拦截工具调用事件（T-AGT-14）。

    Args:
        tool_event_queue: 工具事件队列，用于传递事件到主流程

    Returns:
        EventStreamHandler 函数，接收 RunContext 和事件流
    """

    async def handler(
        ctx: RunContext[PydanticAIDeps],
        events: AsyncGenerator[AgentStreamEvent, None],
    ) -> None:
        """事件流处理器，拦截工具调用/结果事件并写入队列。"""
        async for event in events:
            # 拦截工具调用开始事件
            if isinstance(event, FunctionToolCallEvent):
                part = event.part
                tool_name = part.tool_name
                args = part.args if part.args else {}
                logger.info(
                    "pydantic-ai brain: 工具调用开始 tool_name=%s args=%s",
                    tool_name,
                    args,
                )
                # 写入阶段更新事件（工具调用开始）
                await tool_event_queue.put(
                    AgentStageUpdate(
                        stage="tool_call",
                        metadata={
                            "tool_name": tool_name,
                            "tool_args": args,
                            "tool_call_id": part.tool_call_id,
                            "status": "pending",
                        },
                    )
                )
            # 拦截工具调用结果事件
            elif isinstance(event, FunctionToolResultEvent):
                result_part = event.result
                tool_name = result_part.tool_name
                content = result_part.content
                # 如果是 ToolReturnPart，提取内容
                if isinstance(result_part, ToolReturnPart):
                    result_content = content
                    error = None
                else:
                    # RetryPromptPart 表示工具执行失败，需要重试
                    result_content = str(content)
                    error = "工具执行失败，需要重试"

                logger.info(
                    "pydantic-ai brain: 工具调用完成 tool_name=%s result_type=%s",
                    tool_name,
                    type(result_part).__name__,
                )
                # 写入阶段更新事件（工具调用完成）
                await tool_event_queue.put(
                    AgentStageUpdate(
                        stage="tool_result",
                        metadata={
                            "tool_name": tool_name,
                            "tool_result": result_content,
                            "tool_call_id": result_part.tool_call_id,
                            "status": "completed" if error is None else "error",
                            "error": error,
                        },
                    )
                )

    return handler


def _openai_messages_to_pydantic(
    messages: list[dict[str, Any]],
) -> tuple[str, list[ModelMessage]]:
    """将 OpenAI 格式的消息列表转换为 pydantic-ai 消息格式。

    规则：
    - 最后一条 user 消息 → user_prompt（作为 run_stream 的第一参数）
    - 之前的 user 消息 → ModelRequest(parts=[UserPromptPart(content=...)])
    - assistant 消息 → ModelResponse(parts=[TextPart(content=...)])
    - system 消息：内容合并追加到下一条 user 消息尾部（格式：\\n\\n[系统上下文]\\n{content}）

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
        # 没有 user 消息，返回空 prompt（system 消息无处合并，记录日志）
        system_count = sum(1 for msg in messages if msg.get("role") == "system")
        if system_count > 0:
            logger.warning(
                "pydantic-ai brain: 有 %d 条 system 消息但没有 user 消息可合并",
                system_count,
            )
        return "", []

    user_prompt = ""
    history: list[ModelMessage] = []

    # 收集待合并的 system 消息内容（用于追加到下一条 user 消息）
    pending_system_content: list[str] = []
    merged_system_count = 0

    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content", "")
        # 确保 content 是字符串
        if not isinstance(content, str):
            content = str(content) if content is not None else ""

        if role == "system":
            # 收集 system 消息内容，等待合并到下一条 user 消息
            pending_system_content.append(content)
            continue

        # 合并待处理的 system 内容到当前 user 消息
        if role == "user" and pending_system_content:
            merged_system_count += len(pending_system_content)
            system_block = "\n\n[系统上下文]\n" + "\n".join(pending_system_content)
            content = content + system_block
            pending_system_content.clear()

        if i == last_user_idx:
            # 最后一条 user 消息作为 user_prompt
            user_prompt = content
            continue

        if role == "user":
            history.append(ModelRequest(parts=[UserPromptPart(content=content)]))
        elif role == "assistant":
            history.append(ModelResponse(parts=[TextPart(content=content)]))

    # 记录合并日志
    if merged_system_count > 0:
        logger.info(
            "pydantic-ai brain: 合并了 %d 条 system 消息到 user 消息",
            merged_system_count,
        )

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
        scp_client: SCPClient,
        acli_client: AcliClient,
        kb_client: KBClient | None = None,
    ) -> None:
        """
        Args:
            base_url: OpenAI-compatible API base URL（例如 GLM 的 http://...）
            api_key: API 密钥
            model: 模型名称（例如 "glm-5"）
            scp_client: SCP 平台 API 客户端（注入工具）
            acli_client: acli SSH 执行客户端（注入工具，Phase 1 未直接使用）
            kb_client: KB 服务客户端（可选，为 None 时 get_sop_tree 返回错误）
        """
        self._openai_model = OpenAIModel(
            model_name=model,
            base_url=base_url,
            api_key=api_key,
        )
        self._scp = scp_client
        self._acli = acli_client
        self._kb = kb_client

    @classmethod
    def from_env(
        cls,
        scp_client: SCPClient | None,
        acli_client: AcliClient,
        kb_client: KBClient | None = None,
    ) -> PaiAgentAdapter:
        """从环境变量构造实例（使用 LLM_BASE_URL / LLM_API_KEY）。

        GLM_MODEL 默认值为 "glm-5"。
        """
        return cls(
            base_url=os.environ.get("LLM_BASE_URL", "https://coding.dashscope.aliyuncs.com/v1"),
            api_key=os.environ.get("LLM_API_KEY", ""),
            model=os.environ.get("GLM_MODEL", "glm-5"),
            scp_client=scp_client,
            acli_client=acli_client,
            kb_client=kb_client,
        )

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        stream: bool = True,
        category_id: str | None = None,
        **_kwargs: Any,
    ) -> AsyncGenerator[AgentEvent, None]:
        """调用 pydantic-ai Agent，流式产出 AgentTextChunk 和工具调用事件。

        通过 event_stream_handler 拦截工具调用事件，yield AgentStageUpdate。

        Args:
            session_id: 会话 ID（用于链路追踪）
            messages: OpenAI 格式的消息列表
            env_context: 环境上下文（集群 ID、告警级别等），注入工具依赖
            stream: 是否流式输出（当前实现始终流式，此参数保留用于接口兼容）
            category_id: S0 确认的故障分类编码（如 "虚拟机-003"），用于 SOP 三轨路由

        Raises:
            AgentUnavailableError: 调用失败时抛出，由 AgentRouter 负责降级
        """
        # 写入 100% 全量原始 Prompt 审计
        from app.services.prompt_audit import PromptAuditService
        asyncio.create_task(
            PromptAuditService.write_prompt_audit(
                conversation_id=session_id,
                assistant_type="pydantic-ai",
                messages=messages,
            )
        )

        user_prompt, message_history = _openai_messages_to_pydantic(messages)

        if not user_prompt:
            logger.warning(
                "pydantic-ai brain: 没有 user 消息，session_id=%s",
                session_id,
            )
            yield AgentTextChunk(content="[系统提示] 未收到有效的用户消息。")
            return

        # 创建事件队列（用于收集工具调用事件）
        tool_event_queue: asyncio.Queue[AgentStageUpdate] = asyncio.Queue()

        deps = PydanticAIDeps(
            kb_client=self._kb,
            scp_client=self._scp,
            acli_client=self._acli,
            env_context=env_context or {},
            category_id=category_id,
            tool_event_queue=tool_event_queue,
        )

        agent = _get_agent()

        # 创建 event_stream_handler 用于拦截工具调用事件
        event_handler = _create_event_stream_handler(tool_event_queue)

        with tracer.start_as_current_span("pydantic-ai-brain-process") as span:
            span.set_attribute("session_id", session_id)
            span.set_attribute("user_prompt_len", len(user_prompt))
            span.set_attribute("history_len", len(message_history))
            if category_id:
                span.set_attribute("category_id", category_id)

            try:
                async with agent.run_stream(
                    user_prompt,
                    message_history=message_history,
                    model=self._openai_model,
                    deps=deps,
                    event_stream_handler=event_handler,
                ) as streamed:
                    # 同时迭代文本流和事件队列
                    # 使用一个合并队列来统一处理两种事件源
                    merged_queue: asyncio.Queue[AgentEvent] = asyncio.Queue()

                    async def text_stream_task():
                        """消费文本流，写入合并队列"""
                        async for text in streamed.stream_text(delta=True):
                            if text:
                                await merged_queue.put(AgentTextChunk(content=text))
                        # 文本流结束，写入结束标记
                        await merged_queue.put(None)

                    # 启动文本流任务（后台运行）
                    text_task = asyncio.create_task(text_stream_task())

                    # 启动工具事件转移任务（BUG-R04: 替换忙等待轮询）
                    # 使用 await 阻塞等待，避免 10ms 空转消耗 CPU
                    async def tool_event_drain_task():
                        """将 tool_event_queue 的事件转入 merged_queue（阻塞等待）"""
                        while True:
                            event = await tool_event_queue.get()
                            await merged_queue.put(event)

                    drain_task = asyncio.create_task(tool_event_drain_task())

                    # 主循环：从合并队列阻塞读取（无超时，不空转）
                    try:
                        while True:
                            item = await merged_queue.get()
                            if item is None:
                                # 文本流结束，退出主循环
                                break
                            yield item
                    finally:
                        # 停止 drain 任务
                        drain_task.cancel()
                        with contextlib.suppress(asyncio.CancelledError):
                            await drain_task

                    # 等待文本流任务完成
                    await text_task

                    # 处理剩余的工具事件
                    while not tool_event_queue.empty():
                        event = tool_event_queue.get_nowait()
                        yield event

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
