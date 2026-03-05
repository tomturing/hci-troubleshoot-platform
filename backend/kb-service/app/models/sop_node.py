"""
KB Service SQLAlchemy 模型 — kb_sop_node
"""

from datetime import UTC, datetime

from sqlalchemy import ARRAY, Column, DateTime, ForeignKey, Integer, SmallInteger, String, Text

from shared.database.postgres import Base


class KBSopNode(Base):
    """SOP 决策树节点模型

    将 SOP 排障手册视为"技能"，每个技能按章节拆分为多个节点。

    检索优先级：SOP 关键字精确匹配 > 向量/BM25 兜底
    命中节点后，直接将 content 注入到 system message 的 RAG 上下文中。
    """

    __tablename__ = "kb_sop_node"

    id = Column(Integer, primary_key=True, autoincrement=True)
    skill_id = Column(String(100), nullable=False)                        # 技能 ID（如 vm_boot_failure）
    node_name = Column(String(200), nullable=False)                       # 节点名称（如 CPU不足）
    parent_id = Column(Integer, ForeignKey("kb_sop_node.id"), nullable=True)
    keywords = Column(ARRAY(Text), nullable=False)                        # 触发关键字列表
    file_path = Column(String(500), nullable=True)                        # 对应 MD 文件路径
    content = Column(Text, nullable=True)                                 # 章节全文
    level = Column(SmallInteger, default=1)                               # 层级（1=主章节, 2=子章节）
    sort_order = Column(SmallInteger, default=0)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    def __repr__(self) -> str:
        return f"<KBSopNode(id={self.id}, skill_id={self.skill_id}, node={self.node_name})>"
