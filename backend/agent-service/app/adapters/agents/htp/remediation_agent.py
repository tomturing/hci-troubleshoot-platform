"""
RemediationAgent: S5 方案输出与修复执行 Agent（继承 BaseAgent）

职责：
  - 接收 S4 根因确认后的匹配案例
  - 生成结构化修复方案
  - 使用 ReactEngine（require_all_confirm=True）执行修复操作
  - 每个修复步骤执行前均需用户确认

与 InvestigationAgent 的区别：
  - InvestigationAgent：只读工具 + CDD 诊断（S1-S4）
  - RemediationAgent：写操作工具 + require_all_confirm=True（S5）
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from shared.clients import AIAssistantRegistry, KBClient
from shared.observability.logger import get_logger

from app.adapters.agents.htp.kbd_model import KBD
from app.adapters.agents.htp.react_engine import ReactEngine
from app.domain.agent_port import (
    AgentEvent,
    AgentStageUpdate,
    AgentUnavailableError,
)
from app.domain.base_agent import BaseAgent, Message, Observation, Step, ToolCall

logger = get_logger("remediation-agent")


# S5 专用 Prompt
_S5_SYSTEM_PROMPT_TEMPLATE = """\
你是深信服超融合基础设施（HCI）智能排障专家助手。

【工作方法论】当前诊断阶段：S5 - 方案输出与修复执行

【修复操作规范】
1. 先解释修复原理，让工程师理解每步操作的目的
2. 每个修复步骤执行前会弹出确认对话框，工程师确认后才执行
3. 区分「临时修复」和「永久解决方案」，明确标注
4. 执行后验证：每个修复步骤完成后，立即执行验证命令确认效果
5. 若修复失败，停止操作并给出人工介入建议

【已确认根因】
{root_cause}

【推荐修复方案】
{solution}

⚠️ 重要提示：以下所有操作步骤均需工程师逐步确认后才会执行。

---
当前工单 ID：{case_id}\
"""


class RemediationAgent(BaseAgent):
    """S5 方案输出与修复执行 Agent（继承 BaseAgent）。

    核心设计：
      - require_all_confirm=True：所有工具调用（含只读验证）均需用户确认
      - ReactEngine 驱动：LLM 决定修复步骤顺序，工程师逐步确认执行
      - 失败即停：任意步骤用户拒绝 → 停止，不继续后续修复
    """

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        kb_client: KBClient,
        react_engine: ReactEngine,
    ) -> None:
        super().__init__(name="remediation-agent", max_steps=10)
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        self._react_engine = react_engine

    # ─── BaseAgent 抽象方法实现 ─────────────────────────────────────────────────

    async def think(self, context: list[Message]) -> Step:
        """RemediationAgent 由 ReactEngine 驱动，此方法不应被外部调用。"""
        raise NotImplementedError("RemediationAgent 使用 ReactEngine，不单独调用 think()")

    async def act(self, tool_call: ToolCall) -> Observation:
        """RemediationAgent 由 ReactEngine 驱动，此方法不应被外部调用。"""
        raise NotImplementedError("RemediationAgent 使用 ReactEngine，不单独调用 act()")

    # ─── 对外接口（供 AgentRouter 调用）───────────────────────────────────────

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        matched_kbds: list[KBD] | None = None,
        root_cause: str = "",
        solution: str = "",
        assistant_type: str = "htp-agent",
        case_id: str = "",
        user_id: str = "",
    ) -> AsyncGenerator[AgentEvent, None]:
        """S5 修复执行流程（流式）。

        Args:
            session_id: 会话 ID
            messages: OpenAI 格式消息列表（含对话历史）
            matched_kbds: S4 确认的匹配 KBD 列表（用于提取 root_cause/solution）
            root_cause: 已确认根因（优先使用，为空时从 matched_cases 提取）
            solution: 推荐方案（优先使用，为空时从 matched_cases 提取）
            assistant_type: 助手类型标识
            case_id: 工单 ID
            user_id: 用户 ID

        Yields:
            AgentStageUpdate(stage="remediation_start") — 开始修复
            AgentStageUpdate(stage="thinking")          — LLM 推理中
            AgentInteractiveRequest(kind="tool_confirm") — 每步操作确认
            AgentTextChunk                              — LLM 输出 / 操作结果
            AgentStageUpdate(stage="S6")                — 修复完成，推进到验证闭环
        """
        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            raise AgentUnavailableError(
                agent_name="remediation-agent",
                reason=f"未找到助手类型 '{assistant_type}'",
            )

        # 从 matched_kbds 提取根因和方案（若未显式传入）
        if not root_cause and matched_kbds:
            root_cause = matched_kbds[0].root_cause
        if not solution and matched_kbds:
            solution = matched_kbds[0].solution

        root_cause = root_cause or "根因待确认"
        solution = solution or "请根据诊断结果制定修复方案"

        system_prompt = _S5_SYSTEM_PROMPT_TEMPLATE.format(
            root_cause=root_cause,
            solution=solution,
            case_id=case_id,
        )

        yield AgentStageUpdate(
            stage="remediation_start",
            metadata={
                "root_cause": root_cause,
                "session_id": session_id,
                "require_all_confirm": True,
            },
        )

        logger.info(
            event="remediation_start",
            session_id=session_id,
            root_cause=root_cause[:100],
        )

        # 使用 ReactEngine 执行修复循环
        # require_all_confirm=True：所有工具（含只读验证）均需确认
        async for event in self._react_engine.execute(
            session_id=session_id,
            system_prompt=system_prompt,
            messages=messages,
            assistant_type=assistant_type,
            case_id=case_id,
            user_id=user_id,
            max_iterations=self.max_steps,
            require_all_confirm=True,
        ):
            yield event

        # 修复流程完成，推进到 S6 验证闭环
        yield AgentStageUpdate(
            stage="S6",
            metadata={
                "session_id": session_id,
                "note": "修复步骤已完成，请验证故障是否已解决",
            },
        )
