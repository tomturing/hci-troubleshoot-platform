"""
Prompt 审计落库服务（下沉至 agent-service）

提供统一、健壮且异步非阻塞的方法，将 HTP/OPS/PAI Agent 吞下的 100% 原始全量 Prompt 写入数据库。
"""

from __future__ import annotations

import uuid
from typing import Any

from shared.models.audit import AuditLog
from shared.observability.logger import get_logger

logger = get_logger("prompt-audit-service")


class PromptAuditService:
    """统一审计日志服务类"""

    _session_factory: Any = None

    @classmethod
    def initialize(cls, session_factory: Any) -> None:
        """初始化注入数据库异步 session_factory"""
        cls._session_factory = session_factory
        logger.info("PromptAuditService 初始化成功")

    @classmethod
    async def write_prompt_audit(
        cls,
        conversation_id: str | uuid.UUID,
        assistant_type: str,
        messages: list[dict[str, Any]],
        case_id: str = "",
        trace_id: str = "",
    ) -> None:
        """异步写入 audit_log 记录（后台任务，自行捕获异常）

        Args:
            conversation_id: 会话 ID
            assistant_type: 助手类型（htp-agent / ops-agent / pydantic-ai）
            messages: 大模型吞入的 100% 原始消息列表
            case_id: 工单 ID
            trace_id: 链路追踪 ID
        """
        if not trace_id:
            from shared.observability.otel import get_current_trace_id

            trace_id = get_current_trace_id()

        if not cls._session_factory:
            logger.warning("PromptAuditService 未初始化，跳过审计写入")
            return

        try:
            # 转换与校验 UUID
            conv_uuid = (
                uuid.UUID(str(conversation_id)) if not isinstance(conversation_id, uuid.UUID) else conversation_id
            )
        except ValueError:
            # 强壮性兜底：对非标准格式（如'user'或'Qxxxx'）进行 UUID 命名空间派生，确保 100% 成功落库
            conv_uuid = uuid.uuid5(uuid.NAMESPACE_DNS, str(conversation_id))
            logger.info(
                event="prompt_audit_uuid_derived",
                message=f"已对非标准 UUID 格式会话ID派生定位: {conversation_id} -> {conv_uuid}",
            )

        # 统计总字符数并检测是否含有 SOP 引用以自动推断 has_sop 标志
        total_chars = 0
        for msg in messages:
            content = msg.get("content", "") or ""
            if not isinstance(content, str):
                content = str(content)
            total_chars += len(content)

        # 数据库精准检测：根据是否已存在对应的 active/interrupted/completed 的 sop_execution 记录来判定 has_sop
        has_sop = False
        try:
            from sqlalchemy import text

            async with cls._session_factory() as session:
                res = await session.execute(
                    text("SELECT 1 FROM sop_execution WHERE conversation_id = :conv_id LIMIT 1"), {"conv_id": conv_uuid}
                )
                has_sop = res.fetchone() is not None
        except Exception as e:
            # 强壮性兜底：若数据库查询异常，回退到字符串搜索
            logger.warning(f"SOP 状态查询失败（采用正则兜底）: {e}")
            for msg in messages:
                content = msg.get("content", "") or ""
                if not isinstance(content, str):
                    content = str(content)
                if "SOP" in content or "sop" in content:
                    has_sop = True
                    break

        try:
            async with cls._session_factory() as session:
                audit_log = AuditLog(
                    conversation_id=conv_uuid,
                    audit_type="prompt",
                    payload={
                        "case_id": case_id or f"case-{conversation_id}",
                        "assistant_type": assistant_type,
                        "message_count": len(messages),
                        "has_sop": has_sop,
                        "kb_chunks_count": 0,
                        "kb_top_score": None,
                        "messages": messages,  # 👈 记录 100% 原始全量 Prompt
                        "total_chars": total_chars,
                        "total_token_est": int(total_chars / 3.0),
                    },
                    trace_id=trace_id,
                )
                session.add(audit_log)
                await session.commit()
            logger.info(
                event="agent_prompt_audit_success",
                message=f"已成功捕获并记录大模型原始审计: {assistant_type}, messages={len(messages)}, chars={total_chars}",
                conversation_id=str(conv_uuid),
            )
        except Exception as e:
            # 审计失败绝对不能影响推理引擎主流转，仅记录 warning 级别日志
            logger.warning(
                event="agent_prompt_audit_error",
                message=f"大模型审计写入失败（已自行隔离）: {e}",
                conversation_id=str(conv_uuid),
            )
