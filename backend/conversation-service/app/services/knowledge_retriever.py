"""
知识检索器 - 三轨知识检索（SOP → KB → 降级）

完全从 conversation_service 提取，行为不变：
  1. SOP 轨道：程序性知识，按步骤执行
  2. KB 案例轨道：陈述性知识，提取假设
  3. 机制推理兜底：未命中时使用
"""

import asyncio
from dataclasses import dataclass

from shared.utils.logger import get_logger

from app.config import settings

logger = get_logger("knowledge-retriever")


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
SEGMENT_CODES = {
    # A 层：静态核心（每次请求恒定注入）
    0: ("A1", "专家身份定义"),
    1: ("A2", "诊断方法论"),
    2: ("A3", "推理规范"),
    # B 层：动态注入（三级 Fallback，互斥占位）
    3: {
        "sop": ("B1", "SOP参考资料"),
        "kb_case": ("B2", "案例参考资料"),
        "mechanism": ("B3", "机制推理兜底"),
    },
    # D 层：对话层（工单上下文，每次请求追加）
    4: ("D1", "工单上下文"),
}


def _build_context_breakdown(
    sections: list[str],
    fallback_level: str,
) -> list[dict]:
    """构建 Context Window 分段明细（用于可观测性日志与审计落库）"""
    breakdown: list[dict] = []
    for i, section in enumerate(sections):
        if i <= 2:
            code, name = SEGMENT_CODES[i]  # type: ignore[misc]
        elif i == 3 and len(sections) > 4:
            code, name = SEGMENT_CODES[3].get(  # type: ignore[union-attr]
                fallback_level, ("B3", "机制推理兜底")
            )
        elif i == 3 and len(sections) == 4:
            code, name = SEGMENT_CODES[4]  # type: ignore[misc]
        else:
            code, name = SEGMENT_CODES[4]  # type: ignore[misc]
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
        # 获取当前诊断阶段描述
        stage_desc = STAGE_DESC_MAP.get(diagnostic_stage, f"{diagnostic_stage} - 进行中")

        # Segment 1 + 2 + 3 固定段
        base_sections: list[str] = [
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
            }

        # 并发执行 SOP 匹配 + 语义检索
        sop_task = asyncio.create_task(self._kb_client.sop_match(query))
        search_task = asyncio.create_task(
            self._kb_client.search(query, top_n=settings.KB_SEARCH_TOP_N)
        )
        sop_node, kb_chunks = await asyncio.gather(sop_task, search_task, return_exceptions=True)

        # 容错：任一任务异常均不影响主流程
        if isinstance(sop_node, Exception):
            logger.warning(event="kb_sop_error", message=str(sop_node), case_id=case_id)
            sop_node = None
        if isinstance(kb_chunks, Exception):
            logger.warning(event="kb_search_error", message=str(kb_chunks), case_id=case_id)
            kb_chunks = []

        fallback_level: str

        # --- Segment 4: 三级 Fallback ---
        if sop_node:
            # 优先级1: SOP 命中 → 程序性执行路径
            sop_content = sop_node.get("content", "")
            sop_title = sop_node.get("title") or sop_node.get("node_name") or "SOP 排障手册"
            base_sections.append(
                SEGMENT_SOP_REFERENCE.format(
                    sop_source=sop_title,
                    sop_content=sop_content[:settings.KB_CONTEXT_MAX_CHARS],
                )
            )
            fallback_level = "sop"
            logger.info(
                event="kb_sop_hit",
                message=f"SOP 命中：{sop_title}",
                case_id=case_id,
                sop_title=sop_title,
            )

        elif kb_chunks:
            # 优先级2: KB 案例命中 → 假设生成路径
            chunks_text_parts: list[str] = []
            total_chars = 0
            for i, chunk in enumerate(kb_chunks, 1):
                chunk_content = chunk.get("content", "")
                source = chunk.get("source_title") or chunk.get("document_title") or "未知文档"
                case_id_ref = chunk.get("case_id", "")
                version = chunk.get("applicable_version", "")
                meta = f"案例 #{case_id_ref}" if case_id_ref else f"来源：{source}"
                if version:
                    meta += f" | 版本: {version}"
                if total_chars + len(chunk_content) > settings.KB_CONTEXT_MAX_CHARS:
                    break
                chunks_text_parts.append(f"[{i}] {meta}\n{chunk_content}")
                total_chars += len(chunk_content)

            if chunks_text_parts:
                base_sections.append(
                    SEGMENT_CASE_REFERENCE.format(
                        case_count=len(chunks_text_parts),
                        case_content="\n\n---\n".join(chunks_text_parts),
                    )
                )
            fallback_level = "kb_case"
            logger.info(
                event="kb_search_hit",
                message=f"KB 检索命中 {len(kb_chunks)} 条",
                case_id=case_id,
                hit_count=len(kb_chunks),
            )

        else:
            # 优先级3: 双轨均未命中 → 机制推理兜底
            base_sections.append(SEGMENT_NO_REFERENCE)
            fallback_level = "mechanism"
            logger.info(
                event="kb_no_hit",
                message="KB 双轨均未命中，进入机制推理模式",
                case_id=case_id,
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
            "has_sop": sop_node is not None and not isinstance(sop_node, Exception),
            "kb_chunks_count": len(kb_chunks) if isinstance(kb_chunks, list) else 0,
            "kb_top_score": (
                max((c.get("score", 0.0) for c in kb_chunks), default=None)
                if isinstance(kb_chunks, list) and kb_chunks else None
            ),
            "fallback_level": fallback_level,
            "context_breakdown": _ctx_bd,
            "total_chars": _total_chars,
            "total_token_est": _total_token_est,
        }

        return "\n\n".join(base_sections), audit_meta
