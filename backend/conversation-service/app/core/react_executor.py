"""
ReAct 推理执行器——推理→工具调用→授权检查→执行→观察循环

ReAct（Reasoning + Acting）是 AI Agent 主动诊断的核心执行器：
  1. 让 GLM 分析当前信息，判断下一步行动（Reason）
  2. GLM 决定调用工具时，执行工具调用（Act）
  3. 将工具结果加入上下文，继续推理（Observe）
  4. 循环直到 GLM 生成最终回复（stop）或到达 MAX_STEPS

风险控制策略：
  risk_level=1（只读）：自动执行
  risk_level=2（写操作）：通过 Redis + SSE 请求用户确认，阻塞等待 120s
  risk_level=3（高危）：直接 block，不执行
"""

import logging
import uuid
from collections.abc import AsyncGenerator
from datetime import UTC, datetime
from typing import Any, Protocol, runtime_checkable

from opentelemetry import trace

from .glm_client import GLMClient, LLMResponse, ToolCall
from .tool_registry import TOOL_REGISTRY, get_tools_for_llm

logger = logging.getLogger(__name__)
tracer = trace.get_tracer(__name__)

# 硬限制：最大推理步骤数，防止无限循环
MAX_STEPS = 15


@runtime_checkable
class ToolExecutor(Protocol):
    """工具执行后端协议（SCPAdapter 等实现此协议）"""

    async def execute(self, tool_name: str, args: dict) -> Any:
        """执行工具调用，返回工具结果"""
        ...


@runtime_checkable
class ConfirmServiceProtocol(Protocol):
    """人工确认服务协议"""

    async def request_confirm(
        self,
        session_id: str,
        tool_name: str,
        tool_args: dict,
        risk_level: int,
    ) -> bool:
        """请求用户确认，返回 True=确认，False=取消/超时"""
        ...


@runtime_checkable
class AuditServiceProtocol(Protocol):
    """审计日志服务协议"""

    async def write(self, audit_id: str, **kwargs) -> None:
        """写入审计日志"""
        ...


@runtime_checkable
class SSEEmitterProtocol(Protocol):
    """SSE 推送器协议"""

    async def emit(self, session_id: str, data: dict) -> None:
        """向指定会话推送 SSE 事件"""
        ...


class AgentState:
    """单次 ReAct 执行的状态（对应 LangGraph State 概念）"""

    def __init__(self, session_id: str, messages: list[dict]):
        self.session_id = session_id
        self.messages = messages.copy()
        self.step_count = 0
        self.tool_results: list[dict] = []


class ReactExecutor:
    """ReAct 推理执行器"""

    def __init__(
        self,
        glm_client: GLMClient,
        tool_executor: ToolExecutor,
        confirm_service: ConfirmServiceProtocol,
        audit_service: AuditServiceProtocol,
        sse_emitter: SSEEmitterProtocol,
    ):
        self.glm = glm_client
        self.tool_executor = tool_executor
        self.confirm_service = confirm_service
        self.audit = audit_service
        self.sse = sse_emitter

    async def run(
        self,
        state: AgentState,
        system_prompt: str,
    ) -> AsyncGenerator[str, None]:
        """
        主 ReAct 循环，通过 SSE 实时返回内容。

        循环终止条件：
          1. GLM 返回 finish_reason="stop"（生成最终回复）
          2. 达到 MAX_STEPS 限制
          3. 工具执行出现不可恢复错误
        """
        messages = [
            {"role": "system", "content": system_prompt},
            *state.messages,
        ]
        tools = get_tools_for_llm()

        while state.step_count < MAX_STEPS:
            state.step_count += 1

            # 推理阶段（Reason）：通知前端 AI 正在思考
            await self.sse.emit(state.session_id, {
                "type": "thinking",
                "step": state.step_count,
                "message": "正在分析...",
            })

            response: LLMResponse = await self.glm.chat(
                messages=messages,
                tools=tools,
            )

            # 没有工具调用 = GLM 生成了最终文字回复
            if not response.tool_calls:
                yield response.content or ""
                return

            # 执行阶段（Act）：处理所有工具调用
            tool_results = []
            for tool_call in response.tool_calls:
                result = await self._execute_tool_call(tool_call, state)
                tool_results.append(result)

            # 观察阶段（Observe）：将工具结果加入消息历史
            messages.append({
                "role": "assistant",
                "content": response.content,
                "tool_calls": [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": str(tc.args),
                        },
                    }
                    for tc in response.tool_calls
                ],
            })
            for r in tool_results:
                messages.append({
                    "role": "tool",
                    "tool_call_id": r["tool_call_id"],
                    "content": str(r["result"]),
                })

        # 达到 MAX_STEPS 限制
        yield "⚠️ 诊断步骤已达上限，请联系人工支持"

    async def _execute_tool_call(self, tool_call: ToolCall, state: AgentState) -> dict:
        """
        执行单个工具调用，含授权检查和审计记录。

        工具审计日志在 finally 块中写入（不可因审计失败阻断工具执行）。
        """
        tool_def = TOOL_REGISTRY.get(tool_call.name)
        if not tool_def:
            return {"tool_call_id": tool_call.id, "result": f"未知工具: {tool_call.name}"}

        # 高危工具（risk_level=3 / policy=block）直接拒绝
        if tool_def.policy == "block":
            return {
                "tool_call_id": tool_call.id,
                "result": f"工具 {tool_call.name} 风险等级过高，已阻止执行",
            }

        # 写操作（risk_level >= 2）需要用户确认
        if tool_def.risk_level >= 2:
            # 先推送 confirm_request 事件给前端
            await self.sse.emit(state.session_id, {
                "type": "confirm_request",
                "tool_name": tool_call.name,
                "tool_args": tool_call.args,
                "risk_level": tool_def.risk_level,
                "risk_description": f"将执行操作：{tool_call.name}，参数：{tool_call.args}",
                "timeout_seconds": 120,
            })

            try:
                confirmed = await self.confirm_service.request_confirm(
                    session_id=state.session_id,
                    tool_name=tool_call.name,
                    tool_args=tool_call.args,
                    risk_level=tool_def.risk_level,
                )
            except Exception as e:
                # Redis 不可用时，高风险工具 fallback 为 block（安全优先）
                logger.error(f"确认服务异常，工具 {tool_call.name} 被阻止 [session={state.session_id}]: {e}")
                return {
                    "tool_call_id": tool_call.id,
                    "result": f"确认服务暂不可用，操作 {tool_call.name} 已中止",
                }

            if not confirmed:
                return {
                    "tool_call_id": tool_call.id,
                    "result": "用户已取消该操作",
                }

        # 只读且 policy=notify：执行前通知前端
        if tool_def.policy == "notify":
            await self.sse.emit(state.session_id, {
                "type": "tool_executing",
                "tool": tool_call.name,
                "args": tool_call.args,
            })

        # 执行工具，记录耗时
        started_at = datetime.now(UTC)
        audit_id = str(uuid.uuid4())
        result = None
        error: str | None = None

        try:
            with tracer.start_as_current_span("tool.execute") as span:
                span.set_attribute("tool.name", tool_call.name)
                span.set_attribute("tool.risk_level", tool_def.risk_level)
                span.set_attribute("session_id", state.session_id)
                try:
                    result = await self.tool_executor.execute(tool_call.name, tool_call.args)
                except Exception as e:
                    span.record_exception(e)
                    span.set_status(trace.StatusCode.ERROR, str(e))
                    raise
        except Exception as e:
            error = str(e)
            result = f"工具执行失败: {error}"
        finally:
            completed_at = datetime.now(UTC)
            duration_ms = int((completed_at - started_at).total_seconds() * 1000)

            # 审计记录（在 finally 写入，写入失败不阻断工具执行）
            try:
                await self.audit.write(
                    audit_id=audit_id,
                    session_id=state.session_id,
                    tool_name=tool_call.name,
                    tool_args=tool_call.args,
                    risk_level=tool_def.risk_level,
                    policy=tool_def.policy,
                    result=result,
                    error=error,
                    started_at=started_at,
                    completed_at=completed_at,
                    duration_ms=duration_ms,
                )
            except Exception as audit_err:
                logger.error(
                    f"审计日志写入失败（不影响工具执行）[session={state.session_id} tool={tool_call.name}]: {audit_err}"
                )

        return {"tool_call_id": tool_call.id, "result": result}
