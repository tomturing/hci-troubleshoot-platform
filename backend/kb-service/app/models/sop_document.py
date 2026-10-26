"""
KB Service SQLAlchemy 模型 — sop_document

对应数据库表：sop_document（SOP 排障手册文档）
生命周期：draft → published → archived
tree_json：审核通过时写入（NULL = 未生成），原 sop_tree 1:1 合并入本表
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Boolean, Column, DateTime, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class SopDocument(Base):
    """SOP 排障手册文档模型

    状态机：draft → published → archived
    source_id：幂等键，格式如 sop-vm-start-failure（对应 SOP 文档内部标识）
    tree_json：审核通过后由 sop_parser 生成，NULL 表示未生成或解析失败
    """

    __tablename__ = "sop_document"

    id = Column(Integer, primary_key=True, autoincrement=True)
    source_id = Column(String(100), unique=True, nullable=True)  # 幂等键，如 sop-vm-start-failure
    category_id = Column(String(32), nullable=True)  # 关联 KB 分类（kb_category.code）
    title = Column(String(500), nullable=False)  # SOP 标题
    content_md = Column(Text, nullable=False)  # 完整 SOP Markdown
    docx_hash = Column(String(64), nullable=True)  # 源文件哈希（幂等去重）
    status = Column(String(20), default="draft", nullable=False)  # draft/published/archived
    reviewer_id = Column(Integer, nullable=True)  # 审核人 ID
    reviewed_at = Column(DateTime(timezone=True), nullable=True)  # 审核时间
    published_at = Column(DateTime(timezone=True), nullable=True)  # 发布时间
    # 命中统计（case 级去重，物化列）
    hit_count = Column(Integer, nullable=False, default=0)  # 有多少个唯一 case 命中此 SOP（S1 命中时 +1）

    # 决策树（原 sop_tree 表 1:1 合并，approve 时写入，NULL = 未生成）
    tree_json = Column(JSONB, nullable=True, default=None)
    tree_schema_version = Column(String(20), nullable=True, default="sop-tree-v1")
    tree_leaf_count = Column(Integer, nullable=False, default=0)
    tree_validation_status = Column(String(20), nullable=True)
    tree_validation_issues = Column(JSONB, nullable=True)
    tree_generator_version = Column(String(50), nullable=True, default="sop-parser-v1")

    # 变量定义（T-AGT-24：approve 时自动解析生成）
    variable_schema = Column(JSONB, nullable=True, default=list)  # 变量定义列表 JSONB 数组
    # 已应用的变量包引用，形如 [{"pack": "hci_common_vt_diag", "version": 1}]
    # 仅用于漂移检测与批量同步；运行时只读 variable_schema（展开后的扁平数组）
    applied_packs = Column(JSONB, nullable=False, default=list, server_default="[]")

    # 关键信号集合（跨文档通用，与 kbd_entry.signals_json 同构；抽取阶段填充）
    signals_json = Column(JSONB, nullable=False, default=list)  # 关键信号集合

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


class SopVariablePack(Base):
    """SOP 变量包：可被多个 SOP 复用的命名变量集合。

    设计意图：消灭"同一份通用变量被复制粘贴到多个 SOP"的漂移缺陷。
    包内容结构与社会 sop_document.variable_schema 完全一致，展开后仍写回
    sop_document.variable_schema —— 运行时只读扁平数组，不感知包的存在。
    """

    __tablename__ = "sop_variable_pack"

    id = Column(Integer, primary_key=True, autoincrement=True)
    pack_name = Column(String(64), nullable=False, unique=True)  # 包标识，如 hci_common_vt_diag
    version = Column(Integer, nullable=False, default=1)  # 包版本，升级用于漂移检测
    description = Column(Text, nullable=False, default="")
    variables = Column(JSONB, nullable=False, default=list)  # 变量定义数组
    is_active = Column(Boolean, nullable=False, default=True)
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )
