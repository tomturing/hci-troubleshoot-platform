"""
KB Service SQLAlchemy 模型 — sop_document

对应数据库表：sop_document（SOP 排障手册文档）
生命周期：draft → published → archived
tree_json：审核通过时写入（NULL = 未生成），原 sop_tree 1:1 合并入本表
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class SopDocument(Base):
    """SOP 排障手册文档模型

    状态机：draft → published → archived
    source_id：幂等键，格式如 sop-vm-start-failure（对应 SOP 文档内部标识）
    tree_json：审核通过后由 sop_parser 生成，NULL 表示未生成或解析失败
    """

    __tablename__ = "sop_document"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(100), unique=True, nullable=True)            # 幂等键，如 sop-vm-start-failure
    category_id = Column(String(32), nullable=True)                        # 关联 KB 分类（kb_category.code）
    title = Column(String(500), nullable=False)                            # SOP 标题
    content_md = Column(Text, nullable=False)                              # 完整 SOP Markdown
    docx_hash = Column(String(64), nullable=True)                          # 源文件哈希（幂等去重）
    status = Column(String(20), default="draft", nullable=False)           # draft/published/archived
    reviewer_id = Column(Integer, nullable=True)                           # 审核人 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)           # 审核时间
    published_at = Column(DateTime(timezone=True), nullable=True)          # 发布时间
    # 命中统计（case 级去重，物化列）
    hit_count = Column(Integer, nullable=False, default=0)                 # 有多少个唯一 case 命中此 SOP（S1 命中时 +1）

    # 决策树（原 sop_tree 表 1:1 合并，approve 时写入，NULL = 未生成）
    tree_json = Column(JSONB, nullable=True, default=None)
    tree_schema_version = Column(String(20), nullable=True, default="sop-tree-v1")
    tree_scenario_name = Column(String(500), nullable=True)
    tree_leaf_count = Column(Integer, nullable=False, default=0)
    tree_total_node_count = Column(Integer, nullable=False, default=0)
    tree_validation_status = Column(String(20), nullable=True)
    tree_validation_issues = Column(JSONB, nullable=True)
    tree_generated_at = Column(DateTime(timezone=True), nullable=True)
    tree_generator_version = Column(String(50), nullable=True, default="sop-parser-v1")

    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # 合法状态集合
    VALID_STATUSES = frozenset({"draft", "published", "archived"})

    def __repr__(self) -> str:
        return f"<SopDocument(id={self.id}, title={self.title[:30]}..., status={self.status})>"
