"""
Conversation Model - 对话会话表

完整定义，供 case-service 和 conversation-service 共用。
"""

import uuid
from datetime import UTC, datetime

from shared.database.postgres import Base
from shared.models.base import TraceableMixin
from sqlalchemy import BigInteger, Column, DateTime, Integer, String
from sqlalchemy.dialects.postgresql import JSONB, UUID


class Conversation(Base, TraceableMixin):
    """对话会话表"""

    __tablename__ = "conversation"

    conversation_id = Column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    case_id = Column(String(20), nullable=False, index=True)
    pod_id = Column(String(100), nullable=True)
    assistant_type = Column(String(50), nullable=False, default="openclaw")
    started_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    ended_at = Column(DateTime(timezone=True), nullable=True)
    # [DB-TRIGGER] 由触发器 update_message_count_on_insert / update_message_count_on_delete
    # 调用函数 update_conversation_message_count() 自动维护。
    # 禁止在代码层手动递增，否则会造成双重计数。
    # 只读：通过 session.refresh(conversation) 获取最新值。
    message_count = Column(Integer, default=0)
    repeat_question_count = Column(Integer, default=0, nullable=False)
    metadata_ = Column("metadata", JSONB, default=dict)

    # 诊断状态字段（Phase 2 新增，迁移 0003；v6.2 移除 hypothesis/react_state，见 BUG-06）
    diagnostic_stage = Column(String(8), default="S0", nullable=False, comment="诊断阶段 S0-S6")
    category_l1 = Column(String(100), nullable=True, comment="一级分类")
    category_l2 = Column(String(100), nullable=True, comment="二级分类")
    category_id = Column(String(32), nullable=True, comment="分类 ID，关联 kb_category.code")
    # [v6.2 已移除] hypothesis: 原 JSONB blob，BUG-06 根因，改为 diagnostic_item 子实体表
    # [v6.2 已移除] react_state: ReAct 推理草稿，正确设计是内存存活（AgentState），无需持久化
    pending_confirm = Column(JSONB, nullable=True, comment="待确认工具调用快照（S3/S5 高危工具等待授权），断线重连恢复锚点")
    # v6.3 新增：S6 完成后等待用户意图选择的快照
    # 格式：{"stage":"S6","sent_at":"...","options":["A","B","C"]}
    # 选 A(已解决) → case.status=resolved；选 B(未解决) → 回退S1；选 C(升级人工) → in_progress
    # 与 pending_confirm 独立：两者不会同时出现（pending_confirm在S3/S5，此字段在S6）
    pending_resolution = Column(JSONB, nullable=True, comment="S6 验证闭环后等待用户选择的快照，A/B/C 选择后清空")

    # 知识资产命中引用（case 级去重，hit_count 物化列的数据源）
    # S1 阶段按 category_id 命中 SOP 后写入；NULL 表示无 SOP 或未到 S1
    sop_document_id = Column(Integer, nullable=True, comment="S1 命中的 SOP 文档 ID，FK → sop_document.id")
    # S4 根因确认后 AI 推断的 KBD 叶节点；NULL 表示新问题未收录或未到 S4
    # admin-UI 可手动修正（修正时重新计算 hit_count）
    resolved_kbd_entry_id = Column(BigInteger, nullable=True, comment="S4 根因确认的 KBD 条目 ID，FK → kbd_entry.id")

    def __repr__(self):
        return f"<Conversation(conversation_id={self.conversation_id}, case_id={self.case_id}, stage={self.diagnostic_stage})>"