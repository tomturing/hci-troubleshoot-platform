"""
知识库数据库模型 v3.0 (KB RAG — LearningClaw/ProductionClaw)

v3.0 变更：
- KBDocument/KBChunk 主键由 UUID 改为 SERIAL（整型，pgvector IVFFlat 性能更好）
- KBDocument 扩展完整字段集（状态机、分类树、判断逻辑等）
- KBChunk 新增 tsv tsvector（BM25 全文检索）和 token_count
- 新增：KBSopNode（SOP 决策树节点）、KBCategory（分类树）、KBSynonym（同义词）
"""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    ARRAY,
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    SmallInteger,
    String,
    Text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR

from ..database.postgres import Base


class KBDocument(Base):
    """知识库文档模型 v3.0

    状态机：draft → under_review → approved → published
            draft → rejected
            published → archived
    """

    __tablename__ = "kb_document"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(50), unique=True, nullable=True)            # 原始案例 ID
    title = Column(String(500), nullable=False)
    product = Column(String(100), default="超融合HCI")
    content_md = Column(Text, nullable=False)                             # MD 全文
    content_hash = Column(String(64), nullable=True)                      # SHA256，变更检测
    yaml_meta = Column(JSONB, nullable=True)                              # LLM 增强的结构化元数据
    category_l1 = Column(String(100), nullable=True)                      # 一级分类
    category_l2 = Column(String(100), nullable=True)                      # 二级分类
    tags = Column(ARRAY(Text), nullable=True)                             # 标签数组
    judgment_logic = Column(Text, nullable=True)                          # 排查逻辑（中文）
    summary = Column(Text, nullable=True)                                 # 摘要（中文）
    difficulty = Column(SmallInteger, default=3)                          # 难度 1-5
    status = Column(String(20), default="draft", nullable=False)          # 状态机
    review_note = Column(Text, nullable=True)                             # 审核批注
    reviewer = Column(String(100), nullable=True)                         # 审核人
    reviewed_at = Column(DateTime(timezone=True), nullable=True)
    source_type = Column(String(20), default="kb", nullable=False)        # kb/sop/realtime
    has_images = Column(Boolean, default=False)
    verified_version = Column(String(50), nullable=True)
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    # 合法状态集合
    VALID_STATUSES = frozenset({"draft", "under_review", "approved", "published", "rejected", "archived"})
    # 合法来源类型
    VALID_SOURCE_TYPES = frozenset({"kb", "sop", "realtime"})

    def __repr__(self) -> str:
        return f"<KBDocument(id={self.id}, title={self.title[:30]}..., status={self.status})>"


class KBChunk(Base):
    """知识库分块 + 向量模型 v3.0

    双路检索：
    - embedding：pgvector IVFFlat 余弦相似度（语义检索）
    - tsv：PostgreSQL tsvector GIN 索引（BM25 全文检索）

    tsv 由 KB Service ingestor 在写入时计算（jieba 分词 → to_tsvector('simple', tokens)）。
    """

    __tablename__ = "kb_chunk"

    id = Column(Integer, primary_key=True, autoincrement=True)
    document_id = Column(
        Integer,
        ForeignKey("kb_document.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    chunk_index = Column(SmallInteger, nullable=False)                    # 块序号（0-based）
    content = Column(Text, nullable=False)                                # 块文本（~512 tokens）
    embedding = Column(Vector(384), nullable=True)                        # 384 维向量
    token_count = Column(SmallInteger, nullable=True)                     # token 数
    chunk_metadata = Column("metadata", JSONB, nullable=True)            # 块级元数据（标题层级等）
    tsv = Column(TSVECTOR, nullable=True)                                 # BM25 全文索引
    trace_id = Column(String(64), nullable=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    def __repr__(self) -> str:
        return f"<KBChunk(id={self.id}, document_id={self.document_id}, chunk_index={self.chunk_index})>"


class KBSopNode(Base):
    """SOP 决策树节点模型 v3.0

    将 SOP 排障手册视为"技能"（Skill），每个技能拆分为多个节点。
    检索时优先做关键字精确匹配（keywords 字段），命中后直接注入 content，
    不命中再走向量/BM25 兜底。
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
        return f"<KBSopNode(id={self.id}, skill_id={self.skill_id}, node_name={self.node_name})>"


class KBCategory(Base):
    """知识分类树节点模型 v3.0

    4 层树形结构（L1-L4），首次从 category.json 批量导入。
    L1 共 6 个类别：平台 / 网络 / 存储 / 硬件 / 客户机硬件 / 虚拟机
    """

    __tablename__ = "kb_category"

    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_id = Column(Integer, ForeignKey("kb_category.id"), nullable=True)  # None = L1 根节点
    name = Column(String(100), nullable=False)
    level = Column(SmallInteger, nullable=False)                          # 1=L1, 2=L2, 3=L3, 4=L4
    keywords = Column(ARRAY(Text), nullable=True)                         # 分类触发关键字
    source = Column(String(20), default="manual")                         # manual/auto_generated/auto_suggested
    version = Column(String(20), default="1.0")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    def __repr__(self) -> str:
        return f"<KBCategory(id={self.id}, level={self.level}, name={self.name})>"


class KBSynonym(Base):
    """同义词映射模型 v3.0

    用于 BM25 检索时的词表扩展：HCI → 超融合，VM → 虚拟机 等。
    jieba 自定义词典（hci_dict.txt）与此表保持同步。
    """

    __tablename__ = "kb_synonym"

    id = Column(Integer, primary_key=True, autoincrement=True)
    term = Column(String(100), nullable=False)                            # 缩写/别名
    canonical = Column(String(100), nullable=False)                       # 标准名称
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    def __repr__(self) -> str:
        return f"<KBSynonym(term={self.term} → {self.canonical})>"

