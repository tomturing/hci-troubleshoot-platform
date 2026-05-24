"""
ReactEngine: ReAct 循环执行引擎

职责：
  - 执行工具调用循环（Reason → Act → Observe）
  - 高危操作确认（ConfirmService）
  - 工具结果处理
  - 流式输出推理过程

被 DiagnosticAgent 内部使用（execution_mode=react 时）
"""

import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from opentelemetry import trace
from shared.clients import AIAssistantRegistry
from shared.observability.logger import get_logger

from app.domain.agent_port import (
    AgentEvent,
    AgentInteractiveRequest,
    AgentStageUpdate,
    AgentTextChunk,
)

logger = get_logger("react-engine")
tracer = trace.get_tracer(__name__)

# 硬限制：最大推理步骤数，防止无限循环
MAX_STEPS = 15


# ─── Protocol 定义────────────────────

@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行后端协议（SCPClient、AcliClient 等实现此协议）"""
    async def execute(self, tool_name: str, args: dict) -> Any: ...


@runtime_checkable
class ConfirmServiceProtocol(Protocol):
    """人工确认服务协议"""
    async def request_confirm(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
    ): ...


@runtime_checkable
class AuditServiceProtocol(Protocol):
    """审计日志服务协议"""
    async def write(self, audit_id: str, **kwargs) -> None: ...




class ReactEngine:
    """ReAct 循环执行引擎"""

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        tool_registry: dict,  # TOOL_REGISTRY 字典
        tool_executor: ToolExecutor,
        confirm_service: ConfirmServiceProtocol | None = None,
        audit_service: AuditServiceProtocol | None = None,
    ) -> None:
        self._ai_registry = ai_registry
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._confirm_service = confirm_service
        self._audit = audit_service

    async def execute(
        self,
        *,
        session_id: str,
        system_prompt: str,
        messages: list[dict[str, Any]],
        assistant_type: str = "htp-agent",
        case_id: str = "",
        user_id: str = "",
        max_iterations: int = MAX_STEPS,
        require_all_confirm: bool = False,
    ) -> AsyncGenerator[AgentEvent, None]:
        """ReAct 循环（Reason → Act → Observe）

        Args:
            session_id: 会话 ID
            system_prompt: 系统提示词（已注入知识）
            messages: OpenAI 格式消息列表
            assistant_type: 助手类型标识
            case_id: 工单 ID
            user_id: 用户 ID
            max_iterations: 最大循环次数
            require_all_confirm: True 时所有工具调用（包括只读工具）均需确认

        Yields:
            AgentStageUpdate: 推理阶段状态（thinking、executing）
            AgentInteractiveRequest: 确认请求（如需要）
            AgentTextChunk: 最终文本回复
        """
        from shared.clients.ai_client import InvokeResult

        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            yield AgentTextChunk(content="[错误] 未找到 AI 客户端")
            return

        # 工作消息列表（在循环中动态追加）
        work_messages: list[dict] = [
            {"role": "system", "content": system_prompt},
            *messages,
        ]

        # 工具列表（OpenAI function calling 格式）
        tools = self._get_tools_for_llm()

        for step_count in range(1, max_iterations + 1):

            # ── 推理阶段 ──────────────────────────────────────────────────────
            yield AgentStageUpdate(
                stage="thinking",
                metadata={"step": step_count, "message": "正在分析..."},
            )

            # 非流式 invoke()：支持 tool_calls 解析
            try:
                invoke_result: InvokeResult = await ai_client.invoke(
                    messages=work_messages,
                    tools=tools,
                    user_id=user_id or f"case-{case_id}",
                )
            except Exception as exc:
                logger.error(
                    event="react_invoke_error",
                    error=str(exc),
                    step=step_count,
                    session_id=session_id,
                )
                yield AgentTextChunk(content=f"[错误] LLM 调用失败：{exc}")
                return

            # ── 终止条件：LLM 给出文字回复 ─────────────────────────────────
            if invoke_result.content is not None:
                # 流式输出最终文字回复
                async for chunk in ai_client.chat_completion_stream(
                    messages=work_messages,
                    user_id=user_id or f"case-{case_id}",
                ):
                    if chunk:
                        yield AgentTextChunk(content=chunk)
                return

            # ── 工具调用轮次 ──────────────────────────────────────────────────
            if not invoke_result.tool_calls:
                # invoke() 返回空（不应发生），安全退出
                logger.warning(
                    event="react_empty_result",
                    step=step_count,
                    session_id=session_id,
                )
                yield AgentTextChunk(content="诊断推理已完成。")
                return

            # 将 assistant tool_calls 消息追加到历史
            assistant_msg: dict = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": str(tc.arguments),
                        },
                    }
                    for tc in invoke_result.tool_calls
                ],
            }
            work_messages.append(assistant_msg)

            # 逐个执行工具调用
            for tc in invoke_result.tool_calls:
                tool_call_dict = {"id": tc.id, "name": tc.name, "args": tc.arguments}

                # require_all_confirm 覆盖：将只读工具也升级为需确认
                async for event in self._execute_tool_call(
                    tool_call=tool_call_dict,
                    session_id=session_id,
                    step=step_count,
                    require_all_confirm=require_all_confirm,
                ):
                    # 跳过 AgentTextChunk（工具结果不直接推流，加入历史后继续循环）
                    if not isinstance(event, AgentTextChunk):
                        yield event
                    elif event.content.startswith("工具") and "失败" in event.content:
                        # 工具执行失败时告知用户
                        yield event

                # 获取工具执行结果并追加 tool 消息
                tool_result = await self._get_tool_result(tc.name, tc.arguments)
                work_messages.append({
                    "role": "tool",
                    "tool_call_id": tc.id,
                    "content": str(tool_result),
                })

        # 超出步数限制
        yield AgentTextChunk(content="⚠️ 诊断步骤已达上限，请联系人工支持。")

    async def _get_tool_result(self, tool_name: str, tool_args: dict) -> Any:
        """执行工具并返回原始结果（不含授权检查，仅用于循环内部获取结果）。"""
        try:
            return await self._tool_executor.execute(tool_name, tool_args)
        except Exception as exc:
            logger.warning(
                event="react_tool_result_error",
                tool_name=tool_name,
                error=str(exc),
            )
            return f"工具执行失败: {exc}"

    def _get_tools_for_llm(self) -> list[dict]:
        """返回 OpenAI function calling 格式的工具列表（排除高危工具）"""
        from app.adapters.agents.htp.tool_registry import get_tools_for_llm
        return get_tools_for_llm()

    async def _execute_tool_call(
        self,
        tool_call: dict,
        session_id: str,
        step: int,
        require_all_confirm: bool = False,
    ) -> AsyncGenerator[AgentEvent, None]:
        """执行单个工具调用，含授权检查和审计记录

        Args:
            tool_call: 工具调用信息 {"id": "...", "name": "...", "args": {...}}
            session_id: 会话 ID
            step: 当前步骤数
            require_all_confirm: True 时只读工具也升级为需要用户确认（S5 修复模式用）

        Yields:
            AgentStageUpdate: 工具执行状态
            AgentInteractiveRequest: 确认请求（如需要）
            AgentTextChunk: 工具执行结果（可选）
        """
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY

        tool_name = tool_call.get("name", "")
        tool_args = tool_call.get("args", {})

        tool_def = TOOL_REGISTRY.get(tool_name)
        if not tool_def:
            yield AgentTextChunk(content=f"未知工具: {tool_name}")
            return

        # 高危工具（risk_level=3 / policy=block）直接拒绝
        if tool_def.policy == "block":
            yield AgentTextChunk(content=f"工具 {tool_name} 风险等级过高，已阻止执行")
            return

        # 确认条件：写操作(risk_level>=2) 或 require_all_confirm=True（修复模式下所有操作均需确认）
        needs_confirm = (tool_def.risk_level >= 2 or require_all_confirm) and self._confirm_service
        if needs_confirm:
            yield AgentInteractiveRequest(
                request_id=str(uuid.uuid4()),
                acp_session_id=session_id,
                kind="tool_confirm",
                title=f"确认执行：{tool_name}",
                prompt=f"将执行操作：{tool_name}，参数：{tool_args}",
                options=[
                    {"optionId": "approved", "name": "确认执行"},
                    {"optionId": "rejected", "name": "取消"},
                ],
                custom_input=False,
                metadata={
                    "tool_name": tool_name,
                    "tool_args": tool_args,
                    "risk_level": tool_def.risk_level,
                    "step": step,
                },
            )

            try:
                confirm_result = await self._confirm_service.request_confirm(
                    session_id=session_id,
                    tool_name=tool_name,
                    tool_args=tool_args,
                    risk_level=tool_def.risk_level,
                )
            except Exception as e:
                logger.error(f"确认服务异常: {e}")
                yield AgentTextChunk(content=f"确认服务暂不可用，操作 {tool_name} 已中止")
                return

            if confirm_result.value != "approved":
                yield AgentTextChunk(content="操作已取消")
                return

        # 只读且 policy=notify：执行前通知前端
        if tool_def.policy == "notify":
            yield AgentStageUpdate(
                stage="executing",
                metadata={"tool": tool_name, "args": tool_args, "message": "正在获取日志..."}
            )

        # 执行工具，记录耗时
        started_at = datetime.now(UTC)
        audit_id = str(uuid.uuid4())
        result = None
        error: str | None = None

        try:
            with tracer.start_as_current_span("tool.execute") as span:
                span.set_attribute("tool.name", tool_name)
                span.set_attribute("tool.risk_level", tool_def.risk_level)
                span.set_attribute("session_id", session_id)
                try:
                    result = await self._tool_executor.execute(tool_name, tool_args)
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    raise
        except Exception as e:
            error = str(e)
            result = f"工具执行失败: {error}"
            yield AgentTextChunk(content=f"工具 {tool_name} 执行失败: {error}")
        finally:
            completed_at = datetime.now(UTC)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            # 审计记录
            if self._audit:
                try:
                    await self._audit.write(
                        audit_id=audit_id,
                        session_id=session_id,
                        tool_name=tool_name,
                        tool_args=tool_args,
                        risk_level=tool_def.risk_level,
                        policy=tool_def.policy,
                        result=result,
                        error=error,
                        started_at=started_at,
                        completed_at=completed_at,
                        duration_ms=duration_ms,
                    )
                except Exception as audit_err:
                    logger.error(f"审计日志写入失败: {audit_err}")
