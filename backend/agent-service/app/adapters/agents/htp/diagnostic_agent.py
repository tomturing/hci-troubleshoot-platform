"""
DiagnosticAgent: S1+ 诊断推理 Agent

职责：
  - 按 category_id 获取 SOP/KBD（KBClient）
  - 构建 5段式 Prompt（注入知识）
  - 执行推理（Direct 模式或 ReAct 模式）
  - 返回诊断结果 + 知识命中信息

执行模式：
  - direct: 直接调用 LLM（无工具调用）
  - react: ReAct 循环（工具调用 + 高危确认）
"""

from collections.abc import AsyncGenerator
from typing import Any

from shared.clients import AIAssistantRegistry, KBClient
from shared.observability.logger import get_logger

from app.domain.agent_port import (
    AgentEvent,
    AgentStageUpdate,
    AgentTextChunk,
    AgentUnavailableError,
)

logger = get_logger("diagnostic-agent")


# ─── 5段式 Prompt 常量定义────────────────────

# Segment 1: 专家身份定义（同 IntentAgent）
SEGMENT_IDENTITY = """你是深信服超融合基础设施（HCI）智能排障专家助手。
你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、
IPMI硬件管理、acli诊断工具集的完整用法。
你的目标是协助现场工程师快速定位和解决 HCI 平台故障。"""

# Segment 2: 诊断方法论
SEGMENT_METHODOLOGY = """【工作方法论】
当前诊断阶段：{stage_desc}

标准诊断流程：
S0 意图识别：从客户描述提取关键实体，确认客户真实问题
S1 故障定位：向客户提出 1-3 个精准确认问题，定位到最小故障分类
S2 假设生成：列出 2-3 个最可能的根因假设，按概率排序
S3 验证执行：逐一执行诊断命令，收集系统状态证据
S4 根因确认：根据证据确定根因
S5 方案输出：提供明确可执行的修复步骤
S6 验证闭环：确认问题已解决，记录知识"""

# Segment 3: 推理规范
SEGMENT_REASONING_MODE = """【知识使用规范】
根据当前可用的参考资料，你的工作模式如下：

当有「SOP排障流程」时：
  - 这是针对此类故障的权威操作手册，按其步骤顺序执行
  - 在决策节点（有判断条件的步骤），先执行命令获取证据再做判断
  - 严格区分「临时修复步骤」和「永久解决方案」

当有「历史案例参考」时：
  - 这是历史上发生过的相似故障记录，用于辅助假设形成
  - 重点关注「根因」字段：它揭示了深层原因，是生成假设的核心依据
  - 不要直接照搬解决方案，当前环境版本/配置可能不同，需要先确认

当两者均无时（机制推理模式）：
  - 基于你对 HCI 平台工作原理的训练知识进行推理
  - 所有推断结论必须明确标注【机制推理】
  - 增加一条追问：收集更多信息以便触发知识库匹配"""

# Segment 4-SOP: SOP轨道命中时注入
SEGMENT_SOP_REFERENCE = """【SOP 排障流程 | 来源：{sop_source}】
{sop_content}

请严格按照上述排障流程执行，在每个判断节点收集证据后再做决策。"""

# Segment 4-Case: KB案例轨道命中时注入
SEGMENT_CASE_REFERENCE = """【历史案例参考 | {case_count} 条相似案例】
{case_content}

上述案例供参考，重点关注「根因」字段中揭示的深层原因，用于形成假设。
当前环境可能与案例版本不同，执行修复方案前请先确认版本适用性。"""

# Segment 4-Fallback: 双轨均未命中时
SEGMENT_NO_REFERENCE = """【机制推理模式】
当前知识库中暂未找到与此故障高度匹配的 SOP 或历史案例。
请基于 HCI 平台架构机制知识进行推理：
  - 所有推断必须标注【机制推理】以提示用户这不是经过验证的排障步骤
  - 在回复末尾追加：「如您能提供更具体的报错信息（如错误码、任务ID），我可以尝试匹配更精确的排障流程」"""

# Segment 5: 当前工单上下文
SEGMENT_CONTEXT_TEMPLATE = "---\n当前工单 ID：{case_id}"

# 诊断阶段描述映射
STAGE_DESC_MAP = {
    "S0": "S0 - 意图识别",
    "S1": "S1 - 故障定位",
    "S2": "S2 - 假设生成",
    "S3": "S3 - 验证执行",
    "S4": "S4 - 根因确认",
    "S5": "S5 - 方案输出",
    "S6": "S6 - 验证闭环",
}


class DiagnosticAgent:
    """S1+ 诊断推理 Agent"""

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        kb_client: KBClient,
        react_engine: Any | None = None,  # ReactEngine，可选
    ) -> None:
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        self._react_engine = react_engine

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        category_id: str,
        diagnostic_stage: str = "S1",
        env_context: dict[str, Any] | None = None,
        assistant_type: str = "htp-agent",
        execution_mode: str = "direct",  # direct / react
        case_id: str = "",
        user_id: str = "",
        system_prompt_override: str | None = None,
    ) -> AsyncGenerator[AgentEvent, None]:
        """S1+ 诊断流程

        Args:
            session_id: 会话 ID
            messages: OpenAI 格式消息列表
            category_id: S0 确认的分类（用于三轨路由）
            diagnostic_stage: 当前诊断阶段（S1/S2/S3/S4/S5/S6）
            env_context: 环境上下文（可选）
            assistant_type: 助手类型标识
            execution_mode: 执行模式（direct / react）
            case_id: 工单 ID
            user_id: 用户 ID
            system_prompt_override: 自定义 system_prompt（可选，覆盖默认构建）

        Yields:
            AgentTextChunk: 流式文本
            AgentInteractiveRequest: 高危操作确认（ReactEngine 输出）
            AgentStageUpdate: 知识命中信息
        """
        # 1. 三轨路由：获取 SOP/KBD
        route_result = await self._kb_client.route_by_category(
            category_code=category_id,
            query=messages[-1].get("content", ""),
            top_k=5,
        )

        # 2. 提取知识命中信息
        sop_document_id: int | None = None
        kbd_candidate_ids: list[int] = []
        knowledge_content: str = ""
        knowledge_track: str = "mechanism"
        sop_title: str = ""

        track = route_result.get("track", "human_escalation")
        results = route_result.get("results", [])

        logger.info(
            event="knowledge_route",
            message=f"三轨路由结果: track={track}, results={len(results)}",
            category_id=category_id,
            track=track,
        )

        if track == "sop" and results:
            sop_document_id = results[0].get("id")
            sop_title = results[0].get("title", "SOP 排障手册")
            knowledge_content = results[0].get("content_md", "")
            knowledge_track = "sop"
        elif track == "kbd" and results:
            kbd_candidate_ids = [r.get("id") for r in results if r.get("id")]
            knowledge_content = "\n\n---\n".join([
                f"[{i+1}] 来源：{r.get('title', '未知')}（KBD-{r.get('id')}）\n{r.get('content_md', '')}"
                for i, r in enumerate(results[:3])
            ])
            knowledge_track = "kb_case"

        # 3. 构建 system_prompt（如果未提供 override）
        if system_prompt_override:
            system_prompt = system_prompt_override
        else:
            system_prompt = self._build_diagnostic_prompt(
                category_id=category_id,
                knowledge_content=knowledge_content,
                knowledge_track=knowledge_track,
                sop_title=sop_title,
                diagnostic_stage=diagnostic_stage,
                case_id=case_id,
            )

        # 4. 执行推理
        if execution_mode == "react" and self._react_engine:
            # ReAct 模式（工具调用）
            for event in self._react_engine.execute(
                session_id=session_id,
                system_prompt=system_prompt,
                messages=messages,
                assistant_type=assistant_type,
                case_id=case_id,
                user_id=user_id,
            ):
                yield event
        else:
            # Direct 模式（直接调用 LLM）
            ai_client = self._ai_registry.get_client(assistant_type)
            if not ai_client:
                raise AgentUnavailableError(
                    agent_name="diagnostic-agent",
                    reason=f"未找到助手类型 '{assistant_type}'",
                )

            for chunk in ai_client.chat_completion_stream(
                messages=[{"role": "system", "content": system_prompt}] + messages,
                user_id=user_id or f"case-{case_id}",
            ):
                if chunk:
                    yield AgentTextChunk(content=chunk)

        # 5. 返回知识命中信息（供 conversation 写库）
        if sop_document_id or kbd_candidate_ids:
            yield AgentStageUpdate(
                stage=diagnostic_stage,
                metadata={
                    "sop_document_id": sop_document_id,
                    "kbd_candidate_ids": kbd_candidate_ids,
                    "knowledge_track": knowledge_track,
                }
            )

    def _build_diagnostic_prompt(
        self,
        category_id: str,
        knowledge_content: str,
        knowledge_track: str,
        sop_title: str,
        diagnostic_stage: str,
        case_id: str,
    ) -> str:
        """构建 5段式诊断 Prompt"""
        stage_desc = STAGE_DESC_MAP.get(diagnostic_stage, f"{diagnostic_stage} - 进行中")

        sections: list[str] = [
            SEGMENT_IDENTITY,
            SEGMENT_METHODOLOGY.format(stage_desc=stage_desc),
            SEGMENT_REASONING_MODE,
        ]

        # Segment 4: 动态参考资料（三级 Fallback）
        if knowledge_track == "sop" and knowledge_content:
            sections.append(
                SEGMENT_SOP_REFERENCE.format(
                    sop_source=sop_title,
                    sop_content=knowledge_content[:3000],  # 截断防止过长
                )
            )
        elif knowledge_track == "kb_case" and knowledge_content:
            sections.append(
                SEGMENT_CASE_REFERENCE.format(
                    case_count=3,
                    case_content=knowledge_content[:3000],
                )
            )
        else:
            sections.append(SEGMENT_NO_REFERENCE)

        # Segment 5: 工单上下文
        sections.append(SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))

        return "\n\n".join(sections)
