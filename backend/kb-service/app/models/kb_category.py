"""
KB Service SQLAlchemy 模型 — kb_category

分类树结构：
- L1: 一级技术域（平台/网络/存储/硬件/客户机硬件/虚拟机）
- L2~L4: 子分类节点

字段来源：
- 远端字段（init_schema.sql + 20260401001）：id, parent_id, name, level, keywords, source, version, code, domain, path_labels, embedding
- 本地新增（20260402001）：hit_count, is_active

用途：
- S0 意图识别：根据用户 query 的 embedding 与分类节点的 embedding 计算相似度，路由到对应 SOP
- 分类管理：YAML 导入、状态更新、命中统计
"""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from shared.database.postgres import Base
from sqlalchemy import ARRAY, Boolean, Column, DateTime, ForeignKey, Integer, SmallInteger, String, Text
from sqlalchemy.dialects.postgresql import JSONB


class KbCategory(Base):
    """知识分类模型

    支持场景：
    1. S0 意图识别：embedding 字段用于语义匹配
    2. 分类管理：通过 code 作为业务键进行 CRUD
    3. 运营分析：hit_count 统计热门分类，is_active 支持软删除
    """

    __tablename__ = "kb_category"

    # ---- 远端字段（init_schema.sql + 20260401001）----
    id = Column(Integer, primary_key=True, autoincrement=True)
    parent_id = Column(Integer, ForeignKey("kb_category.id"), nullable=True)  # NULL 表示 L1 根节点
    name = Column(String(100), nullable=False)
    level = Column(SmallInteger, nullable=False)  # 1=L1, 2=L2, 3=L3, 4=L4
    keywords = Column(ARRAY(Text), nullable=True)  # 该类别的触发关键字
    source = Column(String(20), default="manual")  # manual/auto_generated/auto_suggested
    version = Column(String(20), default="1.0")
    created_at = Column(DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False)

    # ---- migration 20260401001 新增 ----
    code = Column(String(32), unique=True, nullable=True)  # 业务键，格式：<domain>-<seq>
    domain = Column(String(50), nullable=True)  # 一级技术域（中文）
    path_labels = Column(JSONB, default=[], nullable=True)  # 从顶层到叶节点的完整路径
    embedding = Column(Vector(1536), nullable=True)  # 分类节点语义向量（1536 维）

    # ---- migration 20260402001 新增 ----
    hit_count = Column(Integer, default=0, nullable=False)  # S0 意图识别命中次数
    is_active = Column(Boolean, default=True, nullable=False)  # 软删除标记

    # 合法来源类型
    VALID_SOURCES = frozenset({"manual", "auto_generated", "auto_suggested"})
    # 合法层级
    VALID_LEVELS = frozenset({1, 2, 3, 4})

    def __repr__(self) -> str:
        return f"<KbCategory(id={self.id}, code={self.code}, name={self.name}, level={self.level})>"

    def to_dict(self) -> dict:
        """序列化为字典（用于 API 响应）"""
        return {
            "id": self.id,
            "code": self.code,
            "name": self.name,
            "level": self.level,
            "domain": self.domain,
            "parent_id": self.parent_id,
            "path_labels": self.path_labels or [],
            "keywords": self.keywords or [],
            "source": self.source,
            "version": self.version,
            "hit_count": self.hit_count,
            "is_active": self.is_active,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
