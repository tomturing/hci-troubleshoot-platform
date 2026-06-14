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
from shared.models.information import EvidenceBundle
from shared.observability.logger import get_logger

from app.domain.agent_port import (
    AgentEvent,
    AgentInteractiveRequest,
    AgentTextChunk,
    AgentUnavailableError,
)
from app.domain.base_agent import BaseAgent, Message, Observation, Step, ToolCall
from app.services.evidence_builder import EvidenceBuilder

logger = get_logger("triage-agent")


@dataclass
class IntentResult:
    """意图识别结果（内部数据结构，不对外暴露）"""

    category_id: str | None
    category_name: str | None
    candidates: list[dict]  # [{"code": "虚拟机-003", "name": "虚拟机开机失败"}]
    needs_confirmation: bool


# ─── Prompt 数据库化收敛已就绪 ──────────────────────────────────────────────


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
        db_session_factory: Any = None,
        fact_store: Any = None,  # FactStore 实例（可选，不注入则降级为无状态模式）
    ) -> None:
        super().__init__(name="triage-agent", max_steps=1)
        self._ai_registry = ai_registry
        self._kb_client = kb_client
        if db_session_factory is None:
            from shared.utils.prompt_loader import create_mock_session_factory

            self._db_session_factory = create_mock_session_factory()
        else:
            self._db_session_factory = db_session_factory
        # EvidenceBuilder：有 FactStore 时从 Redis 加载事实，否则仅基于 env_context 构建
        self._evidence_builder = EvidenceBuilder(fact_store=fact_store)

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
        system_prompt = await self._build_s0_prompt(
            categories=self._categories_cache or {},
            env_context=env_context,
            case_id=case_id,
            session_id=session_id,
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

    async def _build_s0_prompt(
        self,
        categories: dict[str, list[dict]],
        env_context: dict | None,
        case_id: str,
        session_id: str = "",
    ) -> str:
        """构建 S0 意图识别 System Prompt（EvidenceBundle 结构化注入版）。

        替代原有的 json.dumps 原始字典拼接，改为通过 EvidenceBuilder 构建
        带来源标注、新鲜度标注和冲突标注的结构化事实章节，减少 Prompt 噪声。
        """
        from shared.utils.prompt_loader import StrictPromptLoader

        async with self._db_session_factory() as session:
            base_identity = await StrictPromptLoader.load_and_validate(session, "base_identity_v1", [])
            base_methodology = await StrictPromptLoader.load_and_validate(
                session, "base_methodology_v1", ["stage_desc"]
            )
            s0_rules = await StrictPromptLoader.load_and_validate(
                session,
                "s0_intent_recognition_v1",
                ["env_info", "alert_logs", "task_logs", "total_count", "categories_text"],
            )
            base_context = await StrictPromptLoader.load_and_validate(session, "base_case_context_v1", ["case_id"])

        formatted_methodology = base_methodology.format(stage_desc="S0 - 意图识别")

        total_count = sum(len(c) for c in categories.values()) if categories else 0
        categories_text = self._format_categories(categories)

        # ── 阶段二改造：使用 EvidenceBundle 替代原始 json.dumps 拼接 ──────────
        bundle = await self._evidence_builder.build_for_intent_classification(
            session_id=session_id,
            env_context=env_context,
        )
        evidence_section = bundle.to_prompt_section()

        # T2-3: 完全依赖 EvidenceBundle，移除 env_context fallback
        # 从 Bundle 中提取 S0 模板所需的三个占位符变量
        env_info_val = self._extract_fact_value(bundle, "env_info")
        alert_logs_val = self._extract_fact_value(bundle, "alert_logs")
        task_logs_val = self._extract_fact_value(bundle, "task_logs")

        # 如果 EvidenceBundle 中缺少必要字段，记录警告但不 fallback 到 env_context
        if not env_info_val:
            logger.warning("S0 EvidenceBundle 缺少 env_info 字段")
        if not alert_logs_val:
            logger.warning("S0 EvidenceBundle 缺少 alert_logs 字段")
        if not task_logs_val:
            logger.warning("S0 EvidenceBundle 缺少 task_logs 字段")

        formatted_s0_rules = s0_rules.format(
            env_info=env_info_val,
            alert_logs=alert_logs_val,
            task_logs=task_logs_val,
            total_count=total_count,
            categories_text=categories_text,
        )

        formatted_context = base_context.format(case_id=case_id)

        # 将 EvidenceBundle 章节追加在规则段之后，供 LLM 参考
        return "\n\n".join(
            [base_identity, formatted_methodology, formatted_s0_rules, evidence_section, formatted_context]
        )

    @staticmethod
    def _extract_fact_value(bundle: EvidenceBundle, key: str) -> str:
        """从 EvidenceBundle 的 facts 列表中提取指定 key 的字符串化展示值。

        用于将 EvidenceBundle 中的字段回填到 Prompt 模板占位符（向后兼容）。
        """
        for fact in bundle.facts:
            if fact.get("key") == key:
                return str(fact.get("value", ""))
        return ""

    # 叶子节点 code 格式正则：允许多级前缀（如 虚拟机-L2-001、硬件-003）
    # Unicode 转义避免 encoding 风险
    _LEAF_CODE_RE = re.compile(r"^[一-鿿A-Za-z0-9-]+-\d+$")

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
                valid_items = [item for item in items if TriageAgent._LEAF_CODE_RE.match(item.get("code", ""))]
                if valid_items:
                    lines.append(f"### {domain}域（{len(valid_items)}个）")
                    for item in valid_items:  # 免除截断限制，向大模型呈现全部叶子分类
                        code = item.get("code", "")
                        name = item.get("name", "")
                        if code and name:
                            lines.append(f"- {code} {name}")
        return "\n".join(lines)

    @classmethod
    def _get_active_categories_map(cls) -> dict[str, str]:
        """从缓存中获取所有激活分类的 code -> name 映射。"""
        if not cls._categories_cache:
            return {}
        cat_map = {}
        for items in cls._categories_cache.values():
            for item in items:
                code = item.get("code")
                name = item.get("name")
                if code and name:
                    cat_map[code] = name
        return cat_map

    @staticmethod
    def _parse_intent_result(reply: str) -> IntentResult:
        """从 LLM 输出中解析意图识别结果（宽松正则 + 字典验证混合模式）。

        支持任何自定义 Prompt，只要回复中包含合法的叶子节点编码即可进行场景确认或候选提供。
        """
        # 1. 提取所有形如 [中文/字母/数字]-[数字] 的场景编码及其后随的名称描述
        # 匹配规则：匹配分类编码，并且捕获该行中编码之后的所有非换行、非编号字符
        pattern = re.compile(r"([一-鿿A-Za-z0-9-]+-\d+)(?:\s+([^\n①②③④⑤]+))?")

        matches = pattern.findall(reply)
        if not matches:
            return IntentResult(
                category_id=None,
                category_name=None,
                candidates=[],
                needs_confirmation=False,
            )

        # 获取缓存的 active categories 映射（若有）
        cat_map = TriageAgent._get_active_categories_map()

        # 提取并过滤有效匹配
        parsed_items = []
        seen_codes = set()

        for code, raw_name in matches:
            code = code.strip()
            if code in seen_codes:
                continue

            # 校验是否是系统支持的合法叶子节点编码
            if not TriageAgent._LEAF_CODE_RE.match(code):
                continue

            # 优先从分类字典获取准确名称，避免 LLM 输出中的噪音干扰；如果不存在，则从文本中解析并清洗
            if cat_map and code in cat_map:
                name = cat_map[code]
            else:
                # 清洗提取出来的名称
                name = raw_name.strip() if raw_name else ""
                # 去除类似于 "95，高置信度" / "，高置信度" / " 95" 等后缀噪音
                name = re.sub(r"\s*\d+\s*(，|,)\s*高置信度.*$", "", name)
                name = re.sub(r"\s*(，|,)?\s*高置信度.*$", "", name)
                # 剔除可能存在的逗号及之后的内容
                name = re.sub(r"\s*，\s*.*$", "", name)
                name = name.strip()

            seen_codes.add(code)
            parsed_items.append({"code": code, "name": name})

        if not parsed_items:
            return IntentResult(
                category_id=None,
                category_name=None,
                candidates=[],
                needs_confirmation=False,
            )

        # 如果提取到单一编码，直接判定为确认分类（以确认卡呈现）
        if len(parsed_items) == 1:
            item = parsed_items[0]
            return IntentResult(
                category_id=item["code"],
                category_name=item["name"],
                candidates=[],
                needs_confirmation=True,
            )

        # 如果提取到多个编码，判定为多候选（以选项列表呈现）
        return IntentResult(
            category_id=None,
            category_name=None,
            candidates=parsed_items[:4],
            needs_confirmation=True,
        )
