"""
TriageAgent: S0 意图识别 Agent（继承 BaseAgent）

职责：
  - 获取分类列表（KBClient）
  - 构建 S0 Prompt（注入分类 + 环境上下文）
  - 调用 LLM（流式）进行意图识别
  - 解析 category_id 或候选列表
  - 返回 AgentStageUpdate / AgentInteractiveRequest

设计：
  - think() 调用 LLM，返回识别到的 category_id（str终止）或 ToolCall（保留扩展空间）
  - act() 在此 Agent 中不使用，调用即 raise NotImplementedError
  - process() 是对外接口，流式 yield AgentEvent，供 AgentRouter 调用
  - 对比旧 IntentAgent：复用全部 Prompt 常量，但改用 invoke() 做结构化解析
    以确保在复杂输出中稳定提取 category_id

不做：
  - SOP/KBD 检索（分类未知，分类确认后交由 InvestigationAgent）
  - ReAct 循环（纯意图识别，单次 LLM 调用即可）
"""

from __future__ import annotations

import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from shared.clients import AIAssistantRegistry, KBClient
from shared.observability.logger import get_logger

from app.domain.agent_port import (
    AgentEvent,
    AgentInteractiveRequest,
    AgentTextChunk,
    AgentUnavailableError,
)
from app.domain.base_agent import BaseAgent, Message, Observation, Step, ToolCall

logger = get_logger("triage-agent")


@dataclass
class IntentResult:
    """意图识别结果（内部数据结构，不对外暴露）"""

    category_id: str | None
    category_name: str | None
    candidates: list[dict]   # [{"code": "虚拟机-003", "name": "虚拟机开机失败"}]
    needs_confirmation: bool


# ─── Prompt 常量（与 intent_agent.py 保持一致，从注释来看共享即可）─────────────

SEGMENT_IDENTITY = """你是深信服超融合基础设施（HCI）智能排障专家助手。
你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、
IPMI硬件管理、acli诊断工具集的完整用法。
你的目标是协助现场工程师快速定位和解决 HCI 平台故障。"""

SEGMENT_METHODOLOGY = """【工作方法论】
当前诊断阶段：{stage_desc}

标准诊断流程：
S0 意图识别：从客户描述提取关键实体（虚拟机名/集群/时间点），同时查看告警日志和操作日志，确认客户真实问题
S1 故障定位：向客户提出 1-3 个精准确认问题，定位到最小故障分类
S2 假设生成：列出 2-3 个最可能的根因假设，按概率排序
S3 验证执行：逐一执行诊断命令，收集系统状态证据
S4 根因确认：根据证据确定根因
S5 方案输出：提供明确可执行的修复步骤
S6 验证闭环：确认问题已解决，记录知识"""

SEGMENT_REASONING_MODE = """【知识使用规范】
在意图识别阶段（S0），你的唯一目标是：
  从用户描述中提取故障特征，在分类列表中选出最匹配的 1 个分类。

规则：
  - 不要主动诊断或推理根因（等到分类确认后再诊断）
  - 若特征明确，直接输出确认分类
  - 若特征模糊，提出 1 个澄清问题，并给出最多 4 个候选分类供用户选择
  - 严禁捏造分类编码（只能使用分类列表中的编码）"""

SEGMENT_S0_CONTEXT = """【环境上下文】
## 当前环境信息
{env_info}
## 最新告警
{alert_logs}
## 近期任务日志
{task_logs}"""

SEGMENT_S0_CATEGORIES = """【故障分类列表】
请从以下 {total_count} 个分类中选择最匹配的故障分类：

{categories_text}

输出格式要求：
1. 先用自然语言解释判断依据（1-2 句）
2. 如需澄清，最多提 1 个问题
3. 有足够信息时，**必须**在末尾输出（独立一行）：
   「已确认故障分类：{{code}} {{name}}」
4. 或者输出候选列表供用户选择，并引导用户进行选择（包含最多 4 个推荐选项和 1 个“以上都不是”选项，独立五行）：
   ① {{code1}} {{name1}}
   ② {{code2}} {{name2}}
   ③ {{code3}} {{name3}}
   ④ {{code4}} {{name4}}
   ⑤ 以上都不是（请补充症状描述）
5. 确认分类之前，不做诊断推理，不引用 SOP"""

SEGMENT_CONTEXT_TEMPLATE = "---\n当前工单 ID：{case_id}"


class TriageAgent(BaseAgent):
    """S0 意图识别 Agent（继承 BaseAgent）。

    流式输出 LLM 推理过程，同时在末尾解析分类结果。
    分类确认后 yield AgentStageUpdate(stage="S1")，
    需要用户选择时 yield AgentInteractiveRequest。
    """

    # 分类列表类级缓存（5 分钟 TTL），多会话共享
    _categories_cache: dict[str, list[dict]] | None = None
    _categories_cache_time: float = 0.0
    _CACHE_TTL = 300.0

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        kb_client: KBClient,
    ) -> None:
        super().__init__(name="triage-agent", max_steps=1)
        self._ai_registry = ai_registry
        self._kb_client = kb_client

    # ─── BaseAgent 抽象方法实现 ─────────────────────────────────────────────────

    async def think(self, context: list[Message]) -> Step:
        """调用 LLM invoke() 获取意图识别结果（非流式，用于内部解析）。

        返回 str（category_id 或 "UNKNOWN"），TriageAgent 无需工具调用，
        因此 Step 始终是 str 类型。
        """
        ai_client = self._ai_registry.get_client("htp-agent")
        if not ai_client:
            return "UNKNOWN"

        # 将 Message 列表转换为 OpenAI 格式
        messages = [{"role": m.role, "content": m.content} for m in context]

        result = await ai_client.invoke(messages=messages, user_id="triage")
        if result.content:
            parsed = self._parse_intent_result(result.content)
            if parsed.category_id:
                return parsed.category_id
        return "UNKNOWN"

    async def act(self, tool_call: ToolCall) -> Observation:
        """TriageAgent 不执行工具调用，此方法不应被调用。"""
        raise NotImplementedError("TriageAgent 不执行工具调用")

    # ─── 对外接口（供 AgentRouter 调用）───────────────────────────────────────

    async def process(
        self,
        *,
        session_id: str,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None = None,
        assistant_type: str = "htp-agent",
        case_id: str = "",
        user_id: str = "",
    ) -> AsyncGenerator[AgentEvent, None]:
        """S0 意图识别流程（流式）。

        Args:
            session_id: 会话 ID
            messages: OpenAI 格式消息列表
            env_context: 环境信息
            assistant_type: 助手类型标识
            case_id: 工单 ID
            user_id: 用户 ID

        Yields:
            AgentTextChunk: 流式文本（LLM 推理过程）
            AgentInteractiveRequest: 候选确认（AI 给出 ①② 时）
            AgentStageUpdate(stage="S1"): 分类确认后推进到下一阶段
        """
        # 1. 加载分类列表（带缓存）
        await self._ensure_categories_loaded()

        # 2. 构建 S0 Prompt
        system_prompt = self._build_s0_prompt(
            categories=self._categories_cache or {},
            env_context=env_context,
            case_id=case_id,
        )

        # 3. 获取 LLM 客户端
        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            raise AgentUnavailableError(
                agent_name="triage-agent",
                reason=f"未找到助手类型 '{assistant_type}'",
            )

        # 4. 流式调用 LLM（用户可以看到推理过程）
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        full_reply: list[str] = []

        async for chunk in ai_client.chat_completion_stream(
            messages=full_messages,
            user_id=session_id,
            case_id=case_id,
        ):
            if chunk:
                full_reply.append(chunk)
                yield AgentTextChunk(content=chunk)

        # 5. 解析意图识别结果
        reply_text = "".join(full_reply)

        # 空响应兜底：LLM 未返回任何内容时给出友好提示
        if not reply_text.strip():
            logger.warning(
                event="triage_empty_response",
                message="TriageAgent LLM 返回空响应",
                session_id=session_id,
                assistant_type=assistant_type,
            )
            yield AgentTextChunk(
                content="\n[系统提示] AI 服务暂未返回内容，可能是服务暂时繁忙或配置异常。\n"
                "请稍后重试，或联系管理员检查 AI 服务状态。\n"
            )
            return

        result = self._parse_intent_result(reply_text)

        logger.info(
            event="intent_parsed",
            message=f"意图识别：category_id={result.category_id}，candidates={len(result.candidates)}",
            session_id=session_id,
            category_id=result.category_id,
            candidate_count=len(result.candidates),
        )

        # 6. 输出结果事件
        # 缺陷二修复：废除直接确认路径，所有情况统一走 AgentInteractiveRequest
        if result.category_id:
            # 单一候选：以确认卡形式呈现，让用户明确确认
            yield AgentInteractiveRequest(
                request_id=f"triage-confirm-{session_id}",
                acp_session_id=session_id,
                kind="intent_selection",
                title="请确认故障分类",
                prompt="根据您的描述，AI 判断故障分类如下，请确认：",
                options=[
                    {"optionId": "1", "name": f"{result.category_id} {result.category_name}"},
                    {"optionId": "2", "name": "以上不是，重新描述"},
                ],
                custom_input=False,
                metadata={
                    "category_id": result.category_id,
                    "category_name": result.category_name,
                    "single_candidate": True,
                },
            )
        elif result.candidates:
            # 多候选：让用户从列表选择
            options = [
                {"optionId": str(i + 1), "name": f"{c['code']} {c['name']}"}
                for i, c in enumerate(result.candidates[:4])
            ]
            options.append({"optionId": str(len(options) + 1), "name": "以上都不是（请补充症状描述）"})
            yield AgentInteractiveRequest(
                request_id=f"triage-{session_id}",
                acp_session_id=session_id,
                kind="intent_selection",
                title="请确认故障分类",
                prompt="请选择最匹配当前故障的分类",
                options=options,
                custom_input=False,
                metadata={"candidates": result.candidates},
            )
        else:
            # 缺陷三修复：解析失败兜底，提示用户重新描述
            yield AgentTextChunk(
                content="\n\n抱歉，暂时无法识别您的故障类型。"
                        "请尝试更具体地描述问题现象，例如：'虚拟机无法开机，界面显示XXX错误'。"
            )
            logger.warning(
                event="intent_parse_failed",
                message="S0 意图识别解析失败，无候选也无确认",
                session_id=session_id,
                reply_preview=reply_text[:200],
            )

    async def resolve_candidate_selection(
        self,
        selection: int,
        candidates: list[dict],
    ) -> IntentResult:
        """用户从候选列表选择后，返回最终分类结果。

        Args:
            selection: 用户选择的序号（1/2/3/4/5）
            candidates: 候选列表

        Returns:
            IntentResult，category_id=None 表示"以上都不是"
        """
        candidates_slice = candidates[:4]
        none_selection = len(candidates_slice) + 1
        if selection == none_selection or selection > len(candidates_slice):
            return IntentResult(
                category_id=None,
                category_name=None,
                candidates=[],
                needs_confirmation=False,
            )
        chosen = candidates_slice[selection - 1]
        return IntentResult(
            category_id=chosen["code"],
            category_name=chosen["name"],
            candidates=[],
            needs_confirmation=False,
        )

    # ─── 内部方法 ──────────────────────────────────────────────────────────────

    async def _ensure_categories_loaded(self) -> None:
        """确保分类列表已加载（带 TTL 缓存）。"""
        now = time.time()
        if self._categories_cache is None or (now - self._categories_cache_time) > self._CACHE_TTL:
            try:
                TriageAgent._categories_cache = await self._kb_client.get_categories_grouped()
                TriageAgent._categories_cache_time = now
                total = sum(len(c) for c in (self._categories_cache or {}).values())
                logger.info(
                    event="categories_loaded",
                    message=f"已加载 {total} 个分类",
                    domain_count=len(self._categories_cache or {}),
                )
            except Exception as exc:
                logger.warning(
                    event="categories_load_failed",
                    message=f"分类列表加载失败：{exc}",
                )
                if TriageAgent._categories_cache is None:
                    TriageAgent._categories_cache = {}

    def _build_s0_prompt(
        self,
        categories: dict[str, list[dict]],
        env_context: dict | None,
        case_id: str,
    ) -> str:
        """构建 S0 意图识别 System Prompt（5 段式）。"""
        sections: list[str] = [
            SEGMENT_IDENTITY,
            SEGMENT_METHODOLOGY.format(stage_desc="S0 - 意图识别"),
            SEGMENT_REASONING_MODE,
        ]

        # 环境上下文
        if env_context:
            sections.append(
                SEGMENT_S0_CONTEXT.format(
                    env_info=env_context.get("env_info", ""),
                    alert_logs=env_context.get("alert_logs", ""),
                    task_logs=env_context.get("task_logs", ""),
                )
            )

        # 分类列表
        total_count = sum(len(c) for c in categories.values()) if categories else 0
        categories_text = self._format_categories(categories)
        sections.append(
            SEGMENT_S0_CATEGORIES.format(
                total_count=total_count,
                categories_text=categories_text,
            )
        )

        sections.append(SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))

        return "\n\n".join(sections)

    # 叶子节点 code 格式正则：允许多级前缀（如 虚拟机-L2-001、硬件-003）
    # Unicode 转义避免 encoding 风险
    _LEAF_CODE_RE = re.compile(r'^[一-鿿A-Za-z0-9-]+-\d+$')

    @staticmethod
    def _format_categories(categories: dict[str, list[dict]]) -> str:
        """将分类字典格式化为 Prompt 中的文本块。

        防御性过滤：仅保留符合叶子节点 code 格式的分类（前缀-纯数字）。
        排除中间节点（如 硬件-L2-硬盘），防止 LLM 命中非叶子分类。
        """
        lines: list[str] = []
        for domain, items in categories.items():
            if items:
                # 过滤出叶子节点（code 格式为 前缀-纯数字）
                valid_items = [
                    item for item in items
                    if TriageAgent._LEAF_CODE_RE.match(item.get("code", ""))
                ]
                if valid_items:
                    lines.append(f"### {domain}域（{len(valid_items)}个）")
                    for item in valid_items:  # 免除截断限制，向大模型呈现全部叶子分类
                        code = item.get("code", "")
                        name = item.get("name", "")
                        if code and name:
                            lines.append(f"- {code} {name}")
        return "\n".join(lines)

    @staticmethod
    def _parse_intent_result(reply: str) -> IntentResult:
        """从 LLM 输出中解析意图识别结果。

        匹配优先级：
          1. 「已确认故障分类：{code} {name}」— 直接确认（需用户二次确认）
          2. ①②③④⑤候选列表 — 需要用户选择
          3. 未匹配 — 无法识别

        正则使用 Unicode 转义 [一-鿿] 避免 encoding 乱码风险。
        code 格式允许多级前缀（如 虚拟机-L2-001）。
        """
        # 1. 直接确认模式（Unicode 转义 + 兼容半角冒号 + 多级前缀）
        confirmed_pattern = re.compile(
            r'已确认故障分类[：:]\s*([一-鿿A-Za-z0-9-]+-\d+)\s+([^\n]+)'
        )
        m = confirmed_pattern.search(reply)
        if m:
            return IntentResult(
                category_id=m.group(1).strip(),
                category_name=m.group(2).strip(),
                candidates=[],
                needs_confirmation=True,  # 改为 True，所有情况需用户确认
            )

        # 2. 候选列表模式（① ② ③ ④ ⑤）（Unicode 转义 + 多级前缀）
        candidate_pattern = re.compile(
            r'[①②③④⑤]\s*([一-鿿A-Za-z0-9-]+-\d+)\s+([^\n]+)'
        )
        candidates = [
            {"code": m.group(1).strip(), "name": m.group(2).strip()}
            for m in candidate_pattern.finditer(reply)
        ]
        if candidates:
            return IntentResult(
                category_id=None,
                category_name=None,
                candidates=candidates[:4],
                needs_confirmation=True,
            )

        # 3. 未识别
        return IntentResult(
            category_id=None,
            category_name=None,
            candidates=[],
            needs_confirmation=False,
        )
