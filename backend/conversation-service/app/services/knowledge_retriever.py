"""
知识检索器 - 意图识别 + 三轨路由（SOP → KBD → 降级）

改造说明（基于 docs/architecture/21_知识库模块重设计.md 方案B）：
  - 废弃 kb_sop_node 关键字路由（"已知最差的触发机制"）
  - 改为调用 kb-service 的 POST /api/kb/classify/intent 进行 LLM 意图识别
  - 使用返回的 category_id 调用 GET /api/kb/route 进行三轨路由
  - 三轨串行路由：SOP 优先 → KBD 覆盖 → 人工兜底

流程：
  第0轨：意图识别
    用户输入 → classify_intent(query) → category_id（top3 候选）

  第1轨：SOP 优先（根据 category_id 查询 sop_document）
  第2轨：KBD 覆盖（根据 category_id 查询 kbd_entry）
  第3轨：人工兜底（无匹配结果时返回 human_escalation）

S0 阶段特殊处理：
  - 禁止 KB/SOP 检索，避免过早锁定到特定案例
  - 由 PromptBuilder.build_s0_prompt() 构建专用 Prompt（含 198 分类列表）
  - 返回 fallback_level="s0_intent_recognition" 标识
"""

from dataclasses import dataclass

from opentelemetry import trace
from shared.utils.logger import get_logger

from app.config import settings
from app.services.prompt_builder import PromptBuilder, extract_category_from_reply

logger = get_logger("knowledge-retriever")
tracer = trace.get_tracer(__name__)


@dataclass
class KnowledgeResult:
    """知识检索结果"""

    has_sop: bool
    sop_content: str | None
    sop_title: str | None
    kb_chunks: list[dict]
    kb_top_score: float | None
    fallback_level: str  # "sop" | "kb_case" | "mechanism"
    context_breakdown: list[dict]
    total_chars: int
    total_token_est: int


# ─── 5段式 Prompt 常量定义（从 conversation_service 提取）────────────────────
# Segment 1: 专家身份定义
SEGMENT_IDENTITY = """你是深信服超融合基础设施（HCI）智能排障专家助手。
你拥有完整的 HCI 平台工作原理知识：虚拟机生命周期、分布式存储、vxlan网络、
IPMI硬件管理、acli诊断工具集的完整用法。
你的目标是协助现场工程师快速定位和解决 HCI 平台故障。"""

# Segment 2: 诊断方法论（{stage_desc} 在运行时填充当前阶段描述）
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

# Segment 3: 推理规范（说明如何使用各类知识，不硬编码领域结论）
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

# Context Window 分段编码（对应文档 19 五层分类体系）
# A 层：静态核心（每次请求恒定注入）
_SEGMENT_CODE_A: dict[int, tuple[str, str]] = {
    0: ("A1", "专家身份定义"),
    1: ("A2", "诊断方法论"),
    2: ("A3", "推理规范"),
}
# B 层：动态注入（三级 Fallback，互斥占位）独立字典，避免类型混用
_SEGMENT_CODE_B: dict[str, tuple[str, str]] = {
    "sop": ("B1", "SOP参考资料"),
    "kb_case": ("B2", "案例参考资料"),
    "mechanism": ("B3", "机制推理兜底"),
}
# D 层：对话层（工单上下文，每次请求追加）
_SEGMENT_CODE_D1: tuple[str, str] = ("D1", "工单上下文")


def _build_context_breakdown(
    sections: list[str],
    fallback_level: str,
) -> list[dict]:
    """构建 Context Window 分段明细（用于可观测性日志与审计落库）。

    sections 结构固定为 5 段（kb_enabled=True）或 4 段（kb_disabled/机制代替）：
      [0] A1 专家身份定义
      [1] A2 诊断方法论
      [2] A3 推理规范
      [3] B* 动态参考资料（5 段时存在），或直接 D1（4 段时跳过 B 层）
      [4] D1 工单上下文（5 段时在此位置）
    """
    has_b_layer = len(sections) > 4
    breakdown: list[dict] = []
    for i, section in enumerate(sections):
        if i <= 2:
            code, name = _SEGMENT_CODE_A[i]
        elif i == 3 and has_b_layer:
            code, name = _SEGMENT_CODE_B.get(fallback_level, ("B3", "机制推理兜底"))
        else:
            code, name = _SEGMENT_CODE_D1
        chars = len(section)
        breakdown.append({
            "code": code,
            "name": name,
            "chars": chars,
            "token_est": chars // 4,
        })
    return breakdown


class KnowledgeRetriever:
    """三轨知识检索器（SOP → KB → 降级）"""

    def __init__(self, kb_client=None):
        """
        Args:
            kb_client: KBClient 实例，若为 None 则进入机制推理模式
        """
        self._kb_client = kb_client

    async def retrieve(
        self,
        query: str,
        case_id: str,
        diagnostic_stage: str = "S0",
    ) -> tuple[str, dict]:
        """
        构建 5段式 System Prompt（双轨知识架构 + 三级 Fallback）

        返回：
            [0] system_prompt 字符串
            [1] audit_meta 字典
        """
        with tracer.start_as_current_span("knowledge.retrieve") as span:
            span.set_attribute("case_id", case_id)
            span.set_attribute("diagnostic_stage", diagnostic_stage)
            span.set_attribute("query_len", len(query))
            try:
                return await self._retrieve_impl(span, query, case_id, diagnostic_stage)
            except Exception as exc:
                span.record_exception(exc)
                span.set_status(trace.StatusCode.ERROR, str(exc))
                raise

    async def _retrieve_impl(
        self,
        span,
        query: str,
        case_id: str,
        diagnostic_stage: str,
    ) -> tuple[str, dict]:
        """retrieve 的实际实现，由 retrieve() 包裹在 OTel span 中调用。

        新流程（基于 docs/architecture/21_知识库模块重设计.md 方案B）：
        1. 调用 classify_intent(query) 进行意图识别，获取 category_id 列表
        2. 使用 top1 的 category_code 调用 route_by_category 获取知识内容
        3. 根据 route 返回的 track 类型（sop/kbd/human_escalation）注入不同 Segment

        S0 阶段特殊处理：
        - 禁止 KB/SOP 检索，避免过早锁定到特定案例
        - 返回基础 prompt + 分类列表供 LLM 意图识别
        """
        # 获取当前诊断阶段描述
        stage_desc = STAGE_DESC_MAP.get(diagnostic_stage, f"{diagnostic_stage} - 进行中")

        # ─── S0 阶段特殊处理：禁止 KB/SOP 检索 ─────────────────────────────
        if diagnostic_stage == "S0":
            logger.info(
                event="s0_skip_kb_search",
                message="S0 阶段禁止 KB/SOP 检索，使用分类列表进行意图识别",
                case_id=case_id,
            )
            # S0 阶段不进行 KB 检索，返回基础 prompt
            # 实际的分类列表注入由 ConversationService 调用 PromptBuilder.build_s0_prompt 完成
            base_sections: list[str] = [
                SEGMENT_IDENTITY,
                SEGMENT_METHODOLOGY.format(stage_desc=stage_desc),
                SEGMENT_REASONING_MODE,
                SEGMENT_NO_REFERENCE,
                SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id),
            ]
            _ctx_bd = _build_context_breakdown(base_sections, "mechanism")
            span.set_attribute("s0_mode", True)
            span.set_attribute("kb_search_skipped", True)
            return "\n\n".join(base_sections), {
                "has_sop": False,
                "kb_chunks_count": 0,
                "kb_top_score": None,
                "fallback_level": "s0_intent_recognition",  # S0 专用标识
                "context_breakdown": _ctx_bd,
                "total_chars": sum(s["chars"] for s in _ctx_bd),
                "total_token_est": sum(s["token_est"] for s in _ctx_bd),
            }

        # ─── S1+ 阶段：正常 KB 检索流程 ─────────────────────────────────────
        # Segment 1 + 2 + 3 固定段
        base_sections = [
            SEGMENT_IDENTITY,
            SEGMENT_METHODOLOGY.format(stage_desc=stage_desc),
            SEGMENT_REASONING_MODE,
        ]

        # KB 未启用或无客户端时，直接进入机制推理模式
        if not self._kb_client or not settings.KB_ENABLED:
            base_sections.append(SEGMENT_NO_REFERENCE)
            base_sections.append(SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))
            _ctx_bd = _build_context_breakdown(base_sections, "mechanism")
            logger.debug(
                event="context_window_breakdown",
                case_id=case_id,
                stage=diagnostic_stage,
                segments=_ctx_bd,
                total_chars=sum(s["chars"] for s in _ctx_bd),
                total_token_est=sum(s["token_est"] for s in _ctx_bd),
            )
            return "\n\n".join(base_sections), {
                "has_sop": False,
                "kb_chunks_count": 0,
                "kb_top_score": None,
                "fallback_level": "mechanism",
                "context_breakdown": _ctx_bd,
                "total_chars": sum(s["chars"] for s in _ctx_bd),
                "total_token_est": sum(s["token_est"] for s in _ctx_bd),
                "category_id": None,
                "needs_review": True,
            }

        # ─── 第0轨：意图识别 ─────────────────────────────────────────────────────
        intent_result = await self._kb_client.classify_intent(query, top_n=3)
        if not intent_result:
            logger.warning(
                event="intent_classify_failed",
                message="意图识别失败，降级为机制推理",
                case_id=case_id,
            )
            base_sections.append(SEGMENT_NO_REFERENCE)
            base_sections.append(SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))
            _ctx_bd = _build_context_breakdown(base_sections, "mechanism")
            return "\n\n".join(base_sections), {
                "has_sop": False,
                "kb_chunks_count": 0,
                "kb_top_score": None,
                "fallback_level": "mechanism",
                "context_breakdown": _ctx_bd,
                "total_chars": sum(s["chars"] for s in _ctx_bd),
                "total_token_est": sum(s["token_est"] for s in _ctx_bd),
                "category_id": None,
                "needs_review": True,
            }

        categories = intent_result.get("categories", [])
        needs_review_from_intent = intent_result.get("needs_review", False)

        if not categories:
            logger.warning(
                event="intent_classify_empty",
                message="意图识别返回空分类，降级为机制推理",
                case_id=case_id,
            )
            base_sections.append(SEGMENT_NO_REFERENCE)
            base_sections.append(SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))
            _ctx_bd = _build_context_breakdown(base_sections, "mechanism")
            return "\n\n".join(base_sections), {
                "has_sop": False,
                "kb_chunks_count": 0,
                "kb_top_score": None,
                "fallback_level": "mechanism",
                "context_breakdown": _ctx_bd,
                "total_chars": sum(s["chars"] for s in _ctx_bd),
                "total_token_est": sum(s["token_est"] for s in _ctx_bd),
                "category_id": None,
                "needs_review": True,
            }

        # 取 top1 分类作为主要分类
        top_category = categories[0]
        category_code = top_category.get("code")
        category_score = top_category.get("score", 0.0)

        logger.info(
            event="intent_classify_success",
            message=f"意图识别成功：{category_code}（置信度 {category_score:.2f})",
            case_id=case_id,
            category_code=category_code,
            category_score=category_score,
            needs_review=needs_review_from_intent,
        )

        # ─── 三轨路由（SOP → KBD → 人工） ───────────────────────────────────────
        route_result = await self._kb_client.route_by_category(
            category_code=category_code,
            query=query,
            top_k=settings.KB_SEARCH_TOP_N,
        )

        fallback_level: str = "mechanism"
        has_sop = False
        kb_chunks_count = 0
        kb_top_score = None

        if route_result:
            track = route_result.get("track", "human_escalation")
            results = route_result.get("results", [])

            # --- 第1轨：SOP 命中 ---
            if track == "sop" and results:
                sop_content = results[0].get("content_md", "")
                sop_title = results[0].get("title", "SOP 排障手册")
                base_sections.append(
                    SEGMENT_SOP_REFERENCE.format(
                        sop_source=sop_title,
                        sop_content=sop_content[:settings.KB_CONTEXT_MAX_CHARS],
                    )
                )
                fallback_level = "sop"
                has_sop = True
                logger.info(
                    event="route_sop_hit",
                    message=f"三轨路由 SOP 命中：{sop_title}",
                    case_id=case_id,
                    category_code=category_code,
                    sop_title=sop_title,
                )

            # --- 第2轨：KBD 命中 ---
            elif track == "kbd" and results:
                chunks_text_parts: list[str] = []
                chunks_total_chars = 0
                for i, item in enumerate(results, 1):
                    content = item.get("content_md", "")
                    title = item.get("title", "未知文档")
                    support_id = item.get("support_id", "")
                    meta = f"案例 #{support_id}" if support_id else f"来源：{title}"
                    if chunks_total_chars + len(content) > settings.KB_CONTEXT_MAX_CHARS:
                        break
                    chunks_text_parts.append(f"[{i}] {meta}\n{content}")
                    chunks_total_chars += len(content)

                if chunks_text_parts:
                    base_sections.append(
                        SEGMENT_CASE_REFERENCE.format(
                            case_count=len(chunks_text_parts),
                            case_content="\n\n---\n".join(chunks_text_parts),
                        )
                    )
                    fallback_level = "kb_case"
                    kb_chunks_count = len(results)
                    kb_top_score = category_score  # 使用意图识别的置信度
                    logger.info(
                        event="route_kbd_hit",
                        message=f"三轨路由 KBD 命中 {len(results)} 条，已纳入 {len(chunks_text_parts)} 条",
                        case_id=case_id,
                        category_code=category_code,
                        hit_count=len(results),
                        included_count=len(chunks_text_parts),
                    )
                else:
                    base_sections.append(SEGMENT_NO_REFERENCE)
                    fallback_level = "mechanism"
                    logger.warning(
                        event="route_kbd_all_oversized",
                        message=f"KBD 命中 {len(results)} 条但全部超出字符上限，降级为机制推理",
                        case_id=case_id,
                        hit_count=len(results),
                    )

            # --- 第3轨：人工兜底 ---
            else:
                base_sections.append(SEGMENT_NO_REFERENCE)
                fallback_level = "human_escalation"
                logger.info(
                    event="route_human_escalation",
                    message="三轨路由无匹配结果，进入人工兜底",
                    case_id=case_id,
                    category_code=category_code,
                )

        else:
            # route 调用失败，降级为机制推理
            base_sections.append(SEGMENT_NO_REFERENCE)
            fallback_level = "mechanism"
            logger.warning(
                event="route_call_failed",
                message="三轨路由调用失败，降级为机制推理",
                case_id=case_id,
                category_code=category_code,
            )

        # --- Segment 5: 工单上下文 ---
        base_sections.append(SEGMENT_CONTEXT_TEMPLATE.format(case_id=case_id))

        # 构建 context_breakdown
        _ctx_bd = _build_context_breakdown(base_sections, fallback_level)
        _total_chars = sum(s["chars"] for s in _ctx_bd)
        _total_token_est = sum(s["token_est"] for s in _ctx_bd)
        logger.debug(
            event="context_window_breakdown",
            case_id=case_id,
            stage=diagnostic_stage,
            segments=_ctx_bd,
            total_chars=_total_chars,
            total_token_est=_total_token_est,
        )

        # 构建审计元数据
        audit_meta = {
            "has_sop": has_sop,
            "kb_chunks_count": kb_chunks_count,
            "kb_top_score": kb_top_score,
            "fallback_level": fallback_level,
            "context_breakdown": _ctx_bd,
            "total_chars": _total_chars,
            "total_token_est": _total_token_est,
            "category_id": category_code,  # 新增：意图识别返回的分类编码
            "category_score": category_score,  # 新增：意图识别置信度
            "needs_review": needs_review_from_intent,  # 新增：是否需要人工确认
        }

        return "\n\n".join(base_sections), audit_meta