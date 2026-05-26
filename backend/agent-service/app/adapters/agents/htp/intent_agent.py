"""
IntentAgent: S0 意图识别 Agent

@deprecated
此类已被 TriageAgent 替换（继承 BaseAgent）。
保留此文件仅用于向后兼容，新代码请使用 TriageAgent。
迁移指南：
  - IntentAgent.process() → TriageAgent.process()（接口兼容）
  - IntentAgent(ai_registry, kb_client) → TriageAgent(ai_registry, kb_client)
参见：docs/task/agent/events/2026-05-26-P1重要修复与P2重构任务.md#T-AGT-10

职责：
  - 获取分类列表（KBClient）
  - 构建 S0 Prompt（注入分类 + 环境上下文）
  - 调用 LLM 进行意图识别
  - 返回 category_id 或候选列表

不做：
  - SOP/KBD 检索（分类未知）
  - ReAct 循环（纯意图识别）
"""

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
    AgentStageUpdate,
    AgentTextChunk,
    AgentUnavailableError,
)

logger = get_logger("intent-agent")


@dataclass
class IntentResult:
    """意图识别结果"""
    category_id: str | None
    category_name: str | None
    candidates: list[dict]  # [{"code": "...", "name": "..."}]
    needs_confirmation: bool


# ─── 5段式 Prompt 常量定义（从 knowledge_retriever 提取）────────────────────

# Segment 1: 专家身份定义
SEGMENT_IDENTITY = """你是深信服超融合基础设施（HCI）智能排障专家助手。
你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、
IPMI硬件管理、acli诊断工具集的完整用法。
你的目标是协助现场工程师快速定位和解决 HCI 平台故障。"""

# Segment 2: 诊断方法论（{stage_desc} 在运行时填充）
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

# Segment 4-S0: 环境上下文注入
SEGMENT_S0_CONTEXT = """【环境上下文】
## 当前环境信息
{env_info}
## 最新告警
{alert_logs}
## 近期任务日志
{task_logs}"""

# Segment 5-S0: 分类列表注入
SEGMENT_S0_CATEGORIES = """【故障分类列表】
请从以下 {total_count} 个分类中选择最匹配的故障分类：

{categories_text}

输出格式要求：
1. 先用自然语言解释判断依据（1-2 句）
2. 如需澄清，最多提 1 个问题
3. 有足够信息时，**必须**在末尾输出：
   「已确认故障分类：{code} {name}」
4. 或者输出候选列表供用户选择：
   ① {code1} {name1}
   ② {code2} {name2}
   ③ 以上都不是
5. 确认分类之前，不做诊断推理，不引用 SOP"""

# Segment 6: 工单上下文
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


class IntentAgent:
    """S0 意图识别 Agent"""

    # 分类列表缓存（5 分钟 TTL）
    _categories_cache: dict[str, list[dict]] | None = None
    _categories_cache_time: float = 0.0
    _CACHE_TTL = 300.0

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        kb_client: KBClient,
    ) -> None:
        self._ai_registry = ai_registry
        self._kb_client = kb_client

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
        """S0 意图识别流程

        Args:
            session_id: 会话 ID
            messages: OpenAI 格式消息列表
            env_context: 环境信息 {"env_info": "...", "alert_logs": "...", "task_logs": "..."}
            assistant_type: 助手类型标识
            case_id: 工单 ID
            user_id: 用户 ID

        Yields:
            AgentTextChunk: 流式文本
            AgentInteractiveRequest: 候选确认（当 AI 输出 ①②③ 时）
            AgentStageUpdate: 阶段推进（当分类确认时）
        """
        # 1. 获取分类列表（缓存）
        if self._categories_cache is None or \
           (time.time() - self._categories_cache_time) > self._CACHE_TTL:
            try:
                self._categories_cache = await self._kb_client.get_categories_grouped()
                self._categories_cache_time = time.time()
                total = sum(len(c) for c in self._categories_cache.values()) if self._categories_cache else 0
                logger.info(
                    event="categories_loaded",
                    message=f"已加载 {total} 个分类",
                    domain_count=len(self._categories_cache) if self._categories_cache else 0,
                )
            except Exception as e:
                logger.warning(
                    event="categories_load_failed",
                    message=f"分类列表加载失败: {e}",
                )
                self._categories_cache = {}

        # 2. 构建 S0 Prompt
        system_prompt = self._build_s0_prompt(
            categories=self._categories_cache or {},
            env_context=env_context,
            case_id=case_id,
        )

        # 3. 调用 LLM
        ai_client = self._ai_registry.get_client(assistant_type)
        if not ai_client:
            raise AgentUnavailableError(
                agent_name="intent-agent",
                reason=f"未找到助手类型 '{assistant_type}'",
            )

        full_reply: list[str] = []
        for chunk in ai_client.chat_completion_stream(
            messages=[{"role": "system", "content": system_prompt}] + messages,
            user_id=user_id or f"case-{case_id}",
        ):
            if chunk:
                full_reply.append(chunk)
                yield AgentTextChunk(content=chunk)

        # 4. 解析意图识别结果
        reply_text = "".join(full_reply)
        result = self._parse_intent_result(reply_text)

        logger.info(
            event="intent_parsed",
            message=f"意图识别结果: category_id={result.category_id}, candidates={len(result.candidates)}",
            session_id=session_id,
        )

        # 5. 输出结果事件
        if result.category_id and not result.needs_confirmation:
            # 直接确认分类
            yield AgentStageUpdate(
                stage="S1",
                metadata={
                    "category_id": result.category_id,
                    "category_name": result.category_name,
                    "confirmed": True,
                }
            )
        elif result.candidates:
            # 输出候选列表，等待用户选择
            yield AgentInteractiveRequest(
                request_id=f"intent-{session_id}",
                acp_session_id=session_id,
                kind="intent_selection",
                title="请确认故障分类",
                prompt="请选择最匹配的故障分类",
                options=[
                    {"optionId": "1", "name": f"{result.candidates[0]['code']} {result.candidates[0]['name']}"},
                    {"optionId": "2", "name": f"{result.candidates[1]['code']} {result.candidates[1]['name']}" if len(result.candidates) > 1 else "其他"},
                    {"optionId": "3", "name": "以上都不是"},
                ],
                custom_input=False,
                metadata={"candidates": result.candidates},
            )

    def _build_s0_prompt(
        self,
        categories: dict[str, list[dict]],
        env_context: dict | None,
        case_id: str,
    ) -> str:
        """构建 S0 意图识别 Prompt（4 段式）"""
        stage_desc = STAGE_DESC_MAP.get("S0", "S0 - 意图识别")

        sections: list[str] = [
            SEGMENT_IDENTITY,
            SEGMENT_METHODOLOGY.format(stage_desc=stage_desc),
            SEGMENT_REASONING_MODE,
        ]

        # 环境上下文（Segment 4）
        if env_context:
            env_info = env_context.get("env_info", "")
            alert_logs = env_context.get("alert_logs", "")
            task_logs = env_context.get("task_logs", "")
            sections.append(
                SEGMENT_S0_CONTEXT.format(
                    env_info=env_info,
                    alert_logs=alert_logs,
                    task_logs=task_logs,
                )
            )

        # 分类列表（Segment 5）
        total_count = sum(len(c) for c in categories.values()) if categories else 0
        categories_text = self._format_categories(categories)
        sections.append(
            SEGMENT_S0_CATEGORIES.format(
                total_count=total_count,
                categories_text=categories_text,
            )
        )

        # 工单上下文（Segment 6）
        sections.append(SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))

        return "\n\n".join(sections)

    def _format_categories(self, categories: dict[str, list[dict]]) -> str:
        """格式化分类列表（按域分组）"""
        lines: list[str] = []
        for domain, items in categories.items():
            if items:
                lines.append(f"### {domain}域（{len(items)}个）")
                for item in items[:20]:  # 每域最多显示 20 个，避免 prompt 过长
                    code = item.get("code", "")
                    name = item.get("name", "")
                    if code and name:
                        lines.append(f"- {code} {name}")
        return "\n".join(lines)

    def _parse_intent_result(self, reply: str) -> IntentResult:
        """解析 AI 输出，提取分类信息"""
        # 匹配「已确认故障分类：虚拟机-003 虚拟机开机失败」
        confirmed_pattern = re.compile(r'已确认故障分类：([一-龥A-Za-z]+-\d+)\s+([^\n]+)')

        # 匹配候选 ①②③
        candidate_pattern = re.compile(r'[①②]\s*([一-龥A-Za-z]+-\d+)\s+([^\n]+)')

        # 先尝试匹配直接确认
        confirmed_match = confirmed_pattern.search(reply)
        if confirmed_match:
            return IntentResult(
                category_id=confirmed_match.group(1).strip(),
                category_name=confirmed_match.group(2).strip(),
                candidates=[],
                needs_confirmation=False,
            )

        # 再尝试匹配候选列表
        candidates: list[dict] = []
        for match in candidate_pattern.finditer(reply):
            candidates.append({
                "code": match.group(1).strip(),
                "name": match.group(2).strip(),
            })

        if candidates:
            return IntentResult(
                category_id=None,
                category_name=None,
                candidates=candidates[:3],  # 最多保留 3 个候选
                needs_confirmation=True,
            )

        # 未匹配到任何分类信息
        return IntentResult(
            category_id=None,
            category_name=None,
            candidates=[],
            needs_confirmation=False,
        )

    async def resolve_candidate_selection(
        self,
        selection: int,
        candidates: list[dict],
    ) -> IntentResult:
        """用户选择候选后，确认分类

        Args:
            selection: 用户选择（1/2/3）
            candidates: 候选列表

        Returns:
            IntentResult: 确认的分类或"以上都不是"
        """
        if selection == 3:
            return IntentResult(
                category_id=None,
                category_name=None,
                candidates=[],
                needs_confirmation=False,  # 以上都不是，触发 S0 失败流程
            )
        else:
            chosen = candidates[selection - 1] if selection <= len(candidates) else None
            return IntentResult(
                category_id=chosen["code"] if chosen else None,
                category_name=chosen["name"] if chosen else None,
                candidates=[],
                needs_confirmation=False,
            )
