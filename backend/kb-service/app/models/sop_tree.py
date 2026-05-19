"""
KB Service SQLAlchemy 模型 — sop_tree

对应数据库表：sop_tree（SOP 多叉决策树结构存储）

职责边界：
  sop_document → 存储完整 SOP Markdown（供 RAG 检索、人工阅读）
  sop_tree     → 存储结构化 JSON 决策树（供 AI Agent 程序遍历）

设计原则：
  ① 1:1 关联 sop_document，通过外键保持同步
  ② tree_json 存根节点（SOPNode）的完整 dict，schema_version 存元数据
  ③ leaf_count / total_node_count 冗余统计列，避免每次 JSON 遍历计算
  ④ validation_issues 存 JSON 格式的 ValidationIssue 列表（可为 None）
"""

from datetime import UTC, datetime

from shared.database.postgres import Base
from sqlalchemy import Column, DateTime, ForeignKey, Integer, String
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import relationship


class SopTree(Base):
    """SOP 多叉决策树模型

    tree_json 格式：
      { "node_id": "n-1", "name": "...", "level": 1, "prerequisites": [],
        "diagnosis": null, "solution": null, "children": [...] }
      即 SOPNode.model_dump() 的结果，根节点就是 SOPNode（无额外包装）
    """

    __tablename__ = "sop_tree"

    id = Column(Integer, primary_key=True, autoincrement=True)

    # 1:1 关联 sop_document，外键级联删除
    document_id = Column(
        Integer,
        ForeignKey("sop_document.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )

    # 版本控制（解析器升级时可通过此字段识别旧格式并重新解析）
    schema_version = Column(String(20), nullable=False, default="sop-tree-v1")

    # 冗余快查：场景名称（同根节点 name），方便 Agent 按名称检索
    scenario_name = Column(String(500), nullable=False)

    # 核心：根节点 JSON（SOPNode.model_dump() 输出）
    tree_json = Column(JSONB, nullable=False)

    # 统计辅助列（写入时计算，避免运行时遍历 JSON）
    leaf_count = Column(Integer, nullable=False, default=0)         # 叶节点（案例）数量
    total_node_count = Column(Integer, nullable=False, default=0)   # 总节点数（含路由节点）

    # 校验状态（valid / warnings / error）
    validation_status = Column(String(20), nullable=False, default="valid")

    # 校验问题列表（[{"level":"warning","location":"...","message":"..."},...]）
    validation_issues = Column(JSONB, nullable=True)

    # 生成元数据
    generated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    generator_version = Column(String(50), nullable=True, default="sop-parser-v1")  # 解析器版本

    created_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
    )
    updated_at = Column(
        DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # 双向关联（SopDocument 侧需要添加 tree back_populates）
    document = relationship(
        "SopDocument",
        back_populates="tree",
        lazy="select",
    )

    def __repr__(self) -> str:
        return (
            f"<SopTree(id={self.id}, document_id={self.document_id}, "
            f"scenario='{self.scenario_name[:30]}', "
            f"leaves={self.leaf_count}, status={self.validation_status})>"
        )
