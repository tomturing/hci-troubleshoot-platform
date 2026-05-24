"""
InvestigationAgent: S1-S4 诊断调查 Agent（继承 BaseAgent）

职责：
  - 从知识库检索含结构化步骤的候选案例（top-K=15）
  - 执行案例差异诊断（CDD）贪心消除算法
  - 流式输出诊断进展（步骤执行、阶段更新）
  - 生成结构化诊断报告

两种执行模式（由路由轨道决定）：
  sop    → 直接 LLM 模式：注入 SOP 后 LLM 推理，不走 CDD
  kbd/无 → CDD 模式：案例差异诊断，结构化匹配

设计：
  - think()：根据当前 CDD 状态决定下一步工具调用（ToolCall），
             或在锁定案例后返回诊断报告（str）
  - act()：执行 ToolExecutor，返回观察结果
  - process()：CDD 驱动的完整诊断流程，含流式事件
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any

from shared.clients import AIAssistantRegistry, KBClient
from shared.observability.logger import get_logger

from app.adapters.agents.htp.kbd_differential import KBDDiagnostic
from app.adapters.agents.htp.kbd_model import KBD, kbd_from_dict
from app.domain.agent_port import (
    AgentEvent,
    AgentStageUpdate,
    AgentTextChunk,
    AgentUnavailableError,
)
from app.domain.base_agent import BaseAgent, Message, Observation, Step, ToolCall

logger = get_logger("investigation-agent")

# CDD 候选案例检索数量
DEFAULT_TOP_K = 15


class InvestigationAgent(BaseAgent):
    """S1-S4 诊断调查 Agent（CDD 驱动）。

    核心流程：
      1. 检索 top-K 候选案例（含结构化步骤）
      2. 若找到 SOP → 直接 LLM 推理（注入知识）
      3. 若找到案例 → CDD 贪心消除 → 生成报告
      4. 若无知识 → 机制推理模式（LLM 自由推理）
    """

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        kb_client: KBClient,
        tool_executor: Any,  # 实现 ToolExecutor Protocol
        top_k: int = DEFAULT_TOP_K,
    ) -> None:
        super().__init__(name="investigation-agent", max_steps=20)
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        self._tool_executor = tool_executor
        self._top_k = top_k
        # KBD 差异诊断引擎（每次 process() 调用时重新创建，保证状态隔离）
        self._kbd_diag: KBDDiagnostic | None = None

    # ─── BaseAgent 抽象方法实现 ─────────────────────────────────────────────────

    async def think(self, context: list[Message]) -> Step:
        """根据当前 CDD 状态决定下一步（仅在 run() 内部调用）。

        InvestigationAgent 的控制逻辑主要在 process() 中实现（CDD 驱动），
        此方法保留为 BaseAgent 协议的实现，供非流式路径使用。
        """
        # KBD 诊断已完成：返回最终报告
        if self._kbd_diag and self._kbd_diag.get_result():
            result = self._kbd_diag.get_result()
            return result.diagnosis_report if result else "诊断完成，请查看报告"
        return "诊断进行中"

    async def act(self, tool_call: ToolCall) -> Observation:
        """执行工具调用（由 CDD 引擎内部协调，外部调用应使用 process()）。"""
        try:
            result = await self._tool_executor.execute(tool_call.name, tool_call.args)
            return Observation(tool_call=tool_call, result=result, error=None)
        except Exception as exc:
            return Observation(tool_call=tool_call, result=None, error=exc)

    # ─── 对外接口（供 AgentRouter 调用）───────────────────────────────────────

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        category_id: str,
        diagnostic_stage: str = "S1",
        env_context: dict[str, Any] | None = None,
        assistant_type: str = "htp-agent",
        case_id: str = "",
        user_id: str = "",
    ) -> AsyncGenerator[AgentEvent, None]:
        """S1-S4 诊断调查完整流程（流式）。

        Args:
            session_id: 会话 ID
            messages: OpenAI 格式消息列表（对话历史）
            category_id: S0 确认的故障分类编码，如 "虚拟机-003"
            diagnostic_stage: 当前阶段（S1/S2/S3/S4）
            env_context: 环境上下文（含 vm_name、host_id 等占位符替换所需键值）
            assistant_type: 助手类型标识
            case_id: 工单 ID
            user_id: 用户 ID

        Yields:
            AgentStageUpdate(stage="investigation_start") — 开始
            AgentStageUpdate(stage="cdd_*")               — CDD 步骤事件
            AgentTextChunk                                — 诊断报告文本
            AgentStageUpdate(stage="S4")                  — 根因确认，推进阶段
        """
        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            raise AgentUnavailableError(
                agent_name="investigation-agent",
                reason=f"未找到助手类型 '{assistant_type}'",
            )

        user_query = self._extract_user_query(messages)

        yield AgentStageUpdate(
            stage="investigation_start",
            metadata={
                "category_id": category_id,
                "diagnostic_stage": diagnostic_stage,
                "session_id": session_id,
            },
        )

        # 1. 尝试三轨路由（优先 SOP）
        route_result = await self._kb_client.route_by_category(
            category_code=category_id,
            query=user_query,
            top_k=3,
        )

        track = (route_result or {}).get("track", "")
        sop_results = (route_result or {}).get("results", [])

        logger.info(
            event="investigation_route",
            track=track,
            result_count=len(sop_results),
            category_id=category_id,
            session_id=session_id,
        )

        # 2. SOP 轨道 → 直接 LLM 模式
        if track == "sop" and sop_results:
            sop_content = sop_results[0].get("content_md", "")
            sop_title = sop_results[0].get("title", "SOP 排障手册")
            async for event in self._process_sop_mode(
                sop_content=sop_content,
                sop_title=sop_title,
                messages=messages,
                category_id=category_id,
                diagnostic_stage=diagnostic_stage,
                ai_client=ai_client,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event
            return

        # 3. KBD/无 → CDD 模式：检索含步骤的候选案例
        raw_cases = await self._kb_client.search_cases_with_steps(
            category_id=category_id,
            query=user_query,
            top_k=self._top_k,
        )

        if not raw_cases:
            # 无案例 → 机制推理降级
            async for event in self._process_fallback_mode(
                messages=messages,
                category_id=category_id,
                diagnostic_stage=diagnostic_stage,
                ai_client=ai_client,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event
            return

        candidates: list[KBD] = [kbd_from_dict(d) for d in raw_cases]
        logger.info(
            event="kbd_diag_candidates",
            count=len(candidates),
            category_id=category_id,
            session_id=session_id,
        )

        # 4. 执行 KBD 差异诊断
        env_ctx: dict[str, str] = {
            k: str(v) for k, v in (env_context or {}).items() if isinstance(v, (str, int, float))
        }
        self._kbd_diag = KBDDiagnostic(
            ai_registry=self._ai_registry,
            tool_executor=self._tool_executor,
            assistant_type=assistant_type,
        )

        async for event in self._kbd_diag.diagnose(
            candidates=candidates,
            env_context=env_ctx,
            session_id=session_id,
            user_id=user_id,
        ):
            yield event

        # 5. 输出诊断报告
        kbd_result = self._kbd_diag.get_result()
        if kbd_result:
            # 流式输出报告文本
            for chunk in self._split_text_chunks(kbd_result.diagnosis_report, chunk_size=100):
                yield AgentTextChunk(content=chunk)

            # 推进到 S4（根因确认）
            matched_ids = [kbd.id for kbd in kbd_result.matched_kbds[:3]]
            yield AgentStageUpdate(
                stage="S4",
                metadata={
                    "category_id": category_id,
                    "matched_kbds": matched_ids,
                    "is_definitive": kbd_result.is_definitive,
                    "steps_count": len(kbd_result.steps_executed),
                },
            )

    # ─── 执行模式（内部）──────────────────────────────────────────────────────

    async def _process_sop_mode(
        self,
        sop_content: str,
        sop_title: str,
        messages: list[dict],
        category_id: str,
        diagnostic_stage: str,
        ai_client: Any,
        case_id: str,
        user_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """SOP 轨道：注入 SOP 后直接 LLM 推理（流式输出）。"""
        system_prompt = self._build_sop_prompt(
            sop_content=sop_content,
            sop_title=sop_title,
            diagnostic_stage=diagnostic_stage,
            case_id=case_id,
        )
        full_messages = [{"role": "system", "content": system_prompt}, *messages]

        yield AgentStageUpdate(
            stage="sop_reasoning",
            metadata={"sop_title": sop_title, "category_id": category_id},
        )

        async for chunk in ai_client.chat_completion_stream(
            messages=full_messages,
            user_id=user_id or f"case-{case_id}",
        ):
            if chunk:
                yield AgentTextChunk(content=chunk)

    async def _process_fallback_mode(
        self,
        messages: list[dict],
        category_id: str,
        diagnostic_stage: str,
        ai_client: Any,
        case_id: str,
        user_id: str,
    ) -> AsyncGenerator[AgentEvent, None]:
        """无知识库匹配时：机制推理降级模式（流式输出）。"""
        system_prompt = self._build_fallback_prompt(
            category_id=category_id,
            diagnostic_stage=diagnostic_stage,
            case_id=case_id,
        )
        full_messages = [{"role": "system", "content": system_prompt}, *messages]

        yield AgentStageUpdate(
            stage="mechanism_reasoning",
            metadata={"category_id": category_id, "note": "知识库无匹配，使用机制推理"},
        )

        async for chunk in ai_client.chat_completion_stream(
            messages=full_messages,
            user_id=user_id or f"case-{case_id}",
        ):
            if chunk:
                yield AgentTextChunk(content=chunk)

    # ─── Prompt 构建（内部）──────────────────────────────────────────────────

    @staticmethod
    def _build_sop_prompt(
        sop_content: str,
        sop_title: str,
        diagnostic_stage: str,
        case_id: str,
    ) -> str:
        """构建 SOP 模式 System Prompt。"""
        stage_desc_map = {
            "S1": "S1 - 故障定位", "S2": "S2 - 假设生成",
            "S3": "S3 - 验证执行", "S4": "S4 - 根因确认",
        }
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)

        return (
            "你是深信服超融合基础设施（HCI）智能排障专家助手。\n\n"
            f"【工作方法论】当前诊断阶段：{stage_desc}\n\n"
            "【知识使用规范】\n"
            "你有 SOP 排障流程可用，请严格按其步骤顺序执行，在每个判断节点收集证据后再做决策。\n\n"
            f"【SOP 排障流程 | 来源：{sop_title}】\n"
            f"{sop_content}\n\n"
            f"---\n当前工单 ID：{case_id}"
        )

    @staticmethod
    def _build_fallback_prompt(
        category_id: str,
        diagnostic_stage: str,
        case_id: str,
    ) -> str:
        """构建机制推理降级 System Prompt。"""
        stage_desc_map = {
            "S1": "S1 - 故障定位", "S2": "S2 - 假设生成",
            "S3": "S3 - 验证执行", "S4": "S4 - 根因确认",
        }
        stage_desc = stage_desc_map.get(diagnostic_stage, diagnostic_stage)

        return (
            "你是深信服超融合基础设施（HCI）智能排障专家助手。\n\n"
            f"【工作方法论】当前诊断阶段：{stage_desc}\n\n"
            "【机制推理模式】\n"
            f"当前知识库中暂未找到与分类 {category_id} 高度匹配的 SOP 或历史案例。\n"
            "请基于 HCI 平台架构机制知识进行推理：\n"
            "  - 所有推断必须标注【机制推理】\n"
            "  - 在回复末尾追加：「如能提供更具体的报错信息，我可以尝试匹配更精确的排障流程」\n\n"
            f"---\n当前工单 ID：{case_id}"
        )

    # ─── 工具方法（内部）──────────────────────────────────────────────────────

    @staticmethod
    def _extract_user_query(messages: list[dict]) -> str:
        """从消息列表中提取最后一条用户消息内容。"""
        for msg in reversed(messages):
            if msg.get("role") == "user":
                content = msg.get("content", "")
                return content[:500] if isinstance(content, str) else ""
        return ""

    @staticmethod
    def _split_text_chunks(text: str, chunk_size: int = 100) -> list[str]:
        """将长文本分割为固定大小的 chunk 列表（用于流式输出模拟）。"""
        return [text[i:i + chunk_size] for i in range(0, len(text), chunk_size)]
