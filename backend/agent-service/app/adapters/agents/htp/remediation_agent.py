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

import uuid
from collections.abc import AsyncGenerator
from typing import Any

from shared.cdd.kbd_model import KBD
from shared.clients import AIAssistantRegistry, DiagnosticItemClient, KBClient
from shared.observability.logger import get_logger

from app.adapters.agents.htp.react_engine import ReactEngine
from app.domain.agent_port import (
    AgentEvent,
    AgentStageUpdate,
    AgentUnavailableError,
)
from app.domain.base_agent import BaseAgent, Message, Observation, Step, ToolCall

logger = get_logger("remediation-agent")


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
        diagnostic_item_client: DiagnosticItemClient | None = None,
        db_session_factory: Any = None,
    ) -> None:
        super().__init__(name="remediation-agent", max_steps=10)
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        self._react_engine = react_engine
        self._diagnostic_item_client = diagnostic_item_client
        if db_session_factory is None:
            from shared.utils.prompt_loader import create_mock_session_factory

            self._db_session_factory = create_mock_session_factory()
        else:
            self._db_session_factory = db_session_factory

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

        from shared.utils.prompt_loader import StrictPromptLoader

        async with self._db_session_factory() as session:
            base_identity = await StrictPromptLoader.load_and_validate(session, "base_identity_v1", [])
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", ["stage_desc"]
            )
            s5_template = await StrictPromptLoader.load_and_validate(
                session, "s5_solution_v1", ["root_cause", "solution"]
            )
            base_context = await StrictPromptLoader.load_and_validate(session, "base_case_context_v1", ["case_id"])

        formatted_methodology = base_methodology.format(stage_desc="S5 - 方案输出与修复执行")
        formatted_s5 = s5_template.format(
            root_cause=root_cause,
            solution=solution,
        )
        formatted_context = base_context.format(case_id=case_id)

        system_prompt = "\n\n".join([base_identity, formatted_methodology, formatted_s5, formatted_context])

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

        # ─── S5：插入解决方案条目 ──────────────────────────────────────
        if self._diagnostic_item_client and session_id:
            await self._diagnostic_item_client.create_item(
                conversation_id=uuid.UUID(session_id),
                stage="S5",
                type="solution",
                seq=1,
                content={
                    "root_cause": root_cause,
                    "solution": solution,
                    "matched_kbds": [kbd.id for kbd in matched_kbds] if matched_kbds else [],
                    "require_all_confirm": True,
                },
                status="confirmed",
            )
            logger.info(
                event="s5_solution_inserted",
                conversation_id=session_id,
                root_cause=root_cause[:100],
            )

        # 修复流程完成，推进到 S6 验证闭环
        yield AgentStageUpdate(
            stage="S6",
            metadata={
                "session_id": session_id,
                "note": "修复步骤已完成，请验证故障是否已解决",
            },
        )
