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

import hashlib
import json
import re
import time
from collections.abc import AsyncGenerator
from dataclasses import dataclass
from typing import Any

from shared.clients import AIAssistantRegistry, KBClient
from shared.models.information import EvidenceBundle
from shared.observability.logger import get_logger

from app.config import settings
from app.domain.agent_port import (
    AgentEvent,
    AgentInteractiveRequest,
    AgentTextChunk,
    AgentUnavailableError,
)
from app.domain.base_agent import BaseAgent, Message, Observation, Step, ToolCall
from app.services.evidence_builder import EvidenceBuilder

logger = get_logger("triage-agent")

S0_NONE_OPTION_ID = "__none__"


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
    # S0 只做故障分类。显式命令执行请求必须在调用 LLM 前被确定性拦截，
    # 防止模型把预期输出伪装成真实结果，再被展示层误当成控制指令。
    _DIRECT_COMMAND_REQUEST_PATTERNS = (
        re.compile(
            r"(?:^|[\n。！？])\s*(?:请|请你|请帮我|麻烦|帮我|现在|立即|直接)"
            r"[^\n。！？]{0,60}?(?:执行|运行)[^\n。！？]{0,30}?(?:命令|脚本)",
            re.IGNORECASE,
        ),
        re.compile(
            r"(?:^|[\n。！？])\s*(?:执行|运行)(?:以下|下列|这个|该|一条)[^\n。！？]{0,20}?(?:命令|脚本)", re.IGNORECASE
        ),
        re.compile(r"\b(?:please\s+)?(?:run|execute)\b[^\n.!?]{0,50}\b(?:command|script|ssh|shell)\b", re.IGNORECASE),
    )
    _UNGROUNDED_EXECUTION_EVIDENCE_PATTERNS = (
        re.compile(r"```\s*(?:bash|sh|shell|console)\b", re.IGNORECASE),
        re.compile(r"(?:命令)?执行结果\s*[:：]", re.IGNORECASE),
        re.compile(r"退出码\s*[:：=]?\s*-?\d+", re.IGNORECASE),
        re.compile(r"\bexit(?:\s+code)?\s*[:=]\s*-?\d+\b", re.IGNORECASE),
        re.compile(r"(?:已执行|执行成功)[^\n。！？]{0,30}(?:命令|SSH|Shell)", re.IGNORECASE),
    )

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

        result = await ai_client.invoke(messages=messages, user_id="triage", temperature=settings.LLM_TEMPERATURE_S0)
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
        # 0. S0 命令执行硬门禁：不依赖 Prompt，命中后不调用 LLM，也不产生可执行代码块。
        latest_user_text = self._latest_user_text(messages)
        if self._is_direct_command_execution_request(latest_user_text):
            logger.warning(
                event="triage_command_execution_blocked",
                message="S0 阶段拦截显式命令执行请求",
                session_id=session_id,
                case_id=case_id,
                request_sha256=hashlib.sha256(latest_user_text.encode("utf-8")).hexdigest(),
            )
            yield AgentTextChunk(
                content="当前会话仍处于故障分类阶段，本次未执行任何命令。"
                "请先确认故障分类；进入诊断阶段后，Agent 会通过可审计的结构化工具通道执行必要的只读检查。"
            )
            return

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

        # 4. 调用 LLM。先缓冲完整结果并执行证据门禁，再向用户输出；
        # 否则流式分块一旦发出，后置校验无法撤回伪造的执行证据。
        full_messages = [{"role": "system", "content": system_prompt}, *messages]
        full_reply: list[str] = []

        deterministic_result: IntentResult | None = None
        try:
            async for chunk in ai_client.chat_completion_stream(
                messages=full_messages,
                user_id=session_id,
                case_id=case_id,
                temperature=settings.LLM_TEMPERATURE_S0,
            ):
                if chunk:
                    full_reply.append(chunk)
        except Exception as exc:
            logger.warning(
                event="triage_llm_fallback",
                message="S0 模型不可用，使用分类树确定性候选并要求人工确认",
                session_id=session_id,
                error=str(exc),
            )
            deterministic_result = self._deterministic_candidates(messages, env_context)

        # 5. 解析意图识别结果
        reply_text = "".join(full_reply)
        reply_sha256 = hashlib.sha256(reply_text.encode("utf-8")).hexdigest()
        ungrounded_evidence_blocked = self._contains_ungrounded_execution_evidence(reply_text)

        # 空响应兜底：LLM 未返回任何内容时给出友好提示
        if not reply_text.strip() and deterministic_result is None:
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

        if deterministic_result is not None:
            yield AgentTextChunk(content="分类模型暂时不可用，已根据故障描述和现场事实生成候选，请人工确认。")
        elif ungrounded_evidence_blocked:
            logger.error(
                event="triage_ungrounded_execution_evidence_blocked",
                message="S0 模型输出包含无工具证据的命令执行内容，已阻止透传",
                session_id=session_id,
                case_id=case_id,
                reply_sha256=reply_sha256,
            )
            yield AgentTextChunk(
                content="当前仍处于故障分类阶段，系统没有执行任何命令。"
                "检测到回复中包含未经工具结果证明的执行内容，已安全阻止；请根据下方提示继续确认故障分类。"
            )
        else:
            for chunk in full_reply:
                yield AgentTextChunk(content=chunk)

        result = deterministic_result or self._parse_intent_result(reply_text)

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
                    {
                        "optionId": result.category_id,
                        "code": result.category_id,
                        "categoryName": result.category_name,
                        "name": f"{result.category_id} {result.category_name}",
                    },
                    {"optionId": S0_NONE_OPTION_ID, "name": "以上不是，重新描述"},
                ],
                custom_input=False,
                metadata={
                    "category_id": result.category_id,
                    "category_name": result.category_name,
                    "single_candidate": True,
                },
            )
        elif result.candidates:
            # 多候选：category code 是稳定业务身份；序号只由 UI 按展示位置生成。
            options = [
                {
                    "optionId": c["code"],
                    "code": c["code"],
                    "categoryName": c["name"],
                    "name": f"{c['code']} {c['name']}",
                }
                for c in result.candidates[:4]
            ]
            options.append({"optionId": S0_NONE_OPTION_ID, "name": "以上都不是（请补充症状描述）"})
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
            parse_failure_context = (
                {"reply_sha256": reply_sha256, "reply_blocked": True}
                if ungrounded_evidence_blocked
                else {"reply_preview": reply_text[:200]}
            )
            logger.warning(
                event="intent_parse_failed",
                message="S0 意图识别解析失败，无候选也无确认",
                session_id=session_id,
                **parse_failure_context,
            )

    @staticmethod
    def _latest_user_text(messages: list[dict[str, Any]]) -> str:
        """返回最近一条用户消息，避免历史描述触发当前轮次门禁。"""
        for message in reversed(messages):
            if message.get("role") == "user":
                return str(message.get("content") or "").strip()
        return ""

    @classmethod
    def _is_direct_command_execution_request(cls, text: str) -> bool:
        """识别明确的命令执行祈使句；故障描述中的“执行失败”不应误判。"""
        if not text:
            return False
        return any(pattern.search(text) for pattern in cls._DIRECT_COMMAND_REQUEST_PATTERNS)

    @classmethod
    def _contains_ungrounded_execution_evidence(cls, text: str) -> bool:
        """S0 没有工具能力，因此任何命令结果形态都属于无依据执行证据。"""
        if not text:
            return False
        return any(pattern.search(text) for pattern in cls._UNGROUNDED_EXECUTION_EVIDENCE_PATTERNS)

    @classmethod
    def _deterministic_candidates(
        cls,
        messages: list[dict[str, Any]],
        env_context: dict[str, Any] | None,
    ) -> IntentResult:
        """模型不可用时按分类树和现场事实生成候选，不自动确认分类。"""
        user_text = " ".join(str(message.get("content") or "") for message in messages if message.get("role") == "user")
        context_text = json.dumps(env_context or {}, ensure_ascii=False)
        combined = f"{user_text} {context_text}".lower()
        action_aliases = {
            "开机": ("开机", "启动虚拟机", "无法启动", "不能启动"),
            "关机": ("关机", "关闭虚拟机", "无法关闭"),
            "重启": ("重启", "重新启动"),
            "创建": ("创建", "新建"),
            "删除": ("删除",),
            "迁移": ("迁移",),
            "快照": ("快照",),
            "备份": ("备份",),
            "网络": ("网络", "丢包", "不通", "延时"),
        }
        scored: list[tuple[int, str, str]] = []
        for domain, items in (cls._categories_cache or {}).items():
            domain_score = 2 if domain and domain.lower() in combined else 0
            for item in items:
                code = str(item.get("code") or "")
                name = str(item.get("name") or "")
                if not code or not name or not cls._LEAF_CODE_RE.match(code):
                    continue
                score = domain_score
                if name.lower() in combined:
                    score += 20
                for action, aliases in action_aliases.items():
                    if action in name and any(alias.lower() in combined for alias in aliases):
                        score += 10
                distinctive = re.sub(r"虚拟机|失败|异常|告警|或|不", " ", name.lower())
                for token in re.findall(r"[a-z0-9]+|[一-鿿]{2,}", distinctive):
                    if token in combined:
                        score += min(len(token), 6)
                if score > 0:
                    scored.append((score, code, name))

        scored.sort(key=lambda row: (-row[0], row[1]))
        candidates = [{"code": code, "name": name} for _score, code, name in scored[:4]]
        return IntentResult(
            category_id=None,
            category_name=None,
            candidates=candidates,
            needs_confirmation=bool(candidates),
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

    @classmethod
    def _parse_intent_result(cls, reply: str) -> IntentResult:
        """把模型提出的分类收敛到权威分类字典。

        模型回复是不可信文本：只解析明确的“确认分类”标记或独立候选行，禁止
        扫描判断依据全文。任何不在当前启用分类字典中的编码一律拒绝，避免把
        ``ubu-sus-25.2`` 一类资源名称误识别为故障分类。
        """
        cat_map = cls._get_active_categories_map()
        if not cat_map:
            logger.warning(event="triage_category_registry_empty", message="权威分类字典为空，拒绝解析模型分类")
            return IntentResult(
                category_id=None,
                category_name=None,
                candidates=[],
                needs_confirmation=False,
            )

        candidate_pattern = re.compile(
            r"^[\t ]*[①②③④][\t ]*([一-鿿A-Za-z0-9-]+-\d+)(?:[\t ]+[^\r\n]+)?[\t ]*$",
            re.MULTILINE,
        )
        confirmed_pattern = re.compile(
            r"(?:已确认故障分类|故障分类)[：:][\t ]*([一-鿿A-Za-z0-9-]+-\d+)(?:[\t ]+[^\r\n]+)?"
        )

        candidate_codes = candidate_pattern.findall(reply)
        confirmed_match = confirmed_pattern.search(reply)
        proposed_codes = candidate_codes or ([confirmed_match.group(1)] if confirmed_match else [])

        parsed_items: list[dict[str, str]] = []
        seen_codes: set[str] = set()
        for raw_code in proposed_codes:
            code = raw_code.strip()
            if code in seen_codes:
                continue
            if code not in cat_map:
                logger.warning(
                    event="triage_unknown_category_rejected",
                    message=f"模型提出未注册分类 {code}，已拒绝",
                    category_id=code,
                )
                continue
            seen_codes.add(code)
            parsed_items.append({"code": code, "name": cat_map[code]})

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
