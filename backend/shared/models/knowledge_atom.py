"""
知识原子（KnowledgeAtom）数据模型 v1.0

知识原子是最小可检索的知识单元，每个原子对应一个具体的
诊断步骤、修复动作、决策门或背景知识。

知识类型（type）枚举：
  - diagnostic_step   : 诊断步骤（判断/检查）
  - fix_action        : 修复动作（操作命令）
  - decision_gate     : 决策分支（条件判断）
  - background        : 背景知识（概念解释）
  - escalation        : 上报流程（升级处理）

知识领域（knowledge_domain）枚举：
  - sop      : 来自 SOP 排障手册（高置信度）
  - case     : 来自历史案例库
  - inferred : AI 归纳生成（需人工审核）
"""

from datetime import UTC, datetime

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, Column, DateTime, Float, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB

from ..database.postgres import Base


class KnowledgeAtom(Base):
    """知识原子模型 v1.0

    知识原子存储结构化的排障知识片段，支持双路检索：
    - embedding  : pgvector IVFFlat 余弦相似度（语义检索）
    - trigger.task_error_keywords : JSONB 路径精确匹配（关键词触发）
    """

    __tablename__ = "knowledge_atoms"

    # ─── 主键 ──────────────────────────────────────────────────────────────────
    id = Column(String(64), primary_key=True)  # 格式：ka-{uuid12}

    # ─── 分类标识 ─────────────────────────────────────────────────────────────
    type = Column(String(32), nullable=False, index=True)
    # diagnostic_step | fix_action | decision_gate | background | escalation

    category_id = Column(String(32), nullable=True, index=True)
    # 对应 category_baseline.yaml，如 "虚拟机-003"

    knowledge_domain = Column(String(16), nullable=False, default="sop")
    # sop | case | inferred

    # ─── 触发条件（JSONB 精确检索关键路径）──────────────────────────────────
    trigger = Column(JSONB, nullable=True)
    # 结构：{
    #   "stage": "S2",                              # 诊断阶段
    #   "task_error_keywords": ["CPU不足", "0x..."], # 关键字列表
    #   "error_codes": ["0x010032F5"]               # 错误码列表（可选）
    # }

    # ─── 内容正文 ─────────────────────────────────────────────────────────────
    content = Column(JSONB, nullable=False)
    # 结构：{
    #   "description": "...",       # 单句摘要
    #   "full_text": "...",         # 完整内容（MD 格式）
    #   "commands": ["acli vm.on"], # 相关命令（去重保序）
    #   "expected_result": "..."    # 预期结果（可选）
    # }

    # ─── HCI 版本范围 ─────────────────────────────────────────────────────────
    applicable_version_min = Column(String(20), nullable=True)
    applicable_version_max = Column(String(20), nullable=True)

    # ─── 来源追踪 ─────────────────────────────────────────────────────────────
    source_type = Column(String(20), nullable=True)
    # docx | md | case | manual
    source_ref = Column(Text, nullable=True)
    # 原始文件路径或 URL

    # ─── 质量指标 ─────────────────────────────────────────────────────────────
    confidence = Column(Float, default=0.8, nullable=False)
    verified = Column(Boolean, default=False, nullable=False)

    # ─── 向量嵌入（1536 维，OpenAI text-embedding-3-small/large）───────────
    embedding = Column(Vector(1536), nullable=True)

    # ─── 使用反馈统计 ─────────────────────────────────────────────────────────
    usage_count = Column(Integer, default=0, nullable=False)
    feedback_positive = Column(Integer, default=0, nullable=False)
    feedback_negative = Column(Integer, default=0, nullable=False)

    # ─── 可观测性字段 ─────────────────────────────────────────────────────────
    trace_id = Column(String(55), nullable=True, index=True)
    # W3C traceparent 格式，55 字符

    # ─── 时间戳 ───────────────────────────────────────────────────────────────
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return (
            f"<KnowledgeAtom(id={self.id}, type={self.type}, "
            f"category_id={self.category_id}, verified={self.verified})>"
        )


class ErrorCodeIndex(Base):
    """错误码索引模型 v1.0

    建立错误码 → 知识原子的快速映射关系，支持精确错误码检索。
    错误码格式：0x + 8位十六进制（如 0x010032F5）
    """

    __tablename__ = "error_code_index"

    # ─── 主键 ──────────────────────────────────────────────────────────────────
    error_code = Column(String(32), primary_key=True)
    # 格式：0x010032F5（大小写不敏感，存储时统一大写）

    # ─── 描述信息 ─────────────────────────────────────────────────────────────
    description = Column(Text, nullable=True)
    # 错误码含义说明

    # ─── 关联数据（JSON 数组）────────────────────────────────────────────────
    category_ids = Column(JSONB, nullable=True)
    # 关联的分类 ID 列表，如 ["虚拟机-003"]

    knowledge_atom_ids = Column(JSONB, nullable=True)
    # 关联的知识原子 ID 列表，如 ["ka-abc123xyz456"]

    # ─── 来源标识 ─────────────────────────────────────────────────────────────
    source = Column(String(16), default="manual", nullable=False)
    # manual | auto_extracted | vendor_doc

    # ─── 时间戳 ───────────────────────────────────────────────────────────────
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )
    updated_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<ErrorCodeIndex(error_code={self.error_code}, source={self.source})>"


class RawCase(Base):
    """原始案例模型 v1.0

    存储从外部来源（support.sangfor.com.cn 等）抓取的原始案例。
    图片内容由 VisionProcessor 后续异步填充 content_text。
    """

    __tablename__ = "raw_cases"

    # ─── 主键 ──────────────────────────────────────────────────────────────────
    case_id = Column(String(64), primary_key=True)
    # 来自数据源的原始 ID

    # ─── 内容字段 ─────────────────────────────────────────────────────────────
    source_url = Column(Text, nullable=True)
    title = Column(Text, nullable=True)
    content_text = Column(Text, nullable=True)
    # markdown 正文，图片文字由 VisionProcessor 填充

    images = Column(JSONB, nullable=True)
    # 图片元数据列表：[{"url": "...", "caption": "...", "ocr_text": "..."}]

    # ─── 分类结果 ─────────────────────────────────────────────────────────────
    classification = Column(JSONB, nullable=True)
    # 结构：{
    #   "category_id": "虚拟机-003",
    #   "confidence": 0.92,
    #   "top3": [{"category_id": "...", "score": 0.92}, ...]
    # }

    # ─── 质量评分 ─────────────────────────────────────────────────────────────
    quality_score = Column(Float, nullable=True)
    # 0.0-1.0，由 enricher.py 计算

    # ─── 处理状态 ─────────────────────────────────────────────────────────────
    processed_at = Column(DateTime(timezone=True), nullable=True)
    # 最后处理时间，None 表示待处理

    # ─── 可观测性字段 ─────────────────────────────────────────────────────────
    trace_id = Column(String(55), nullable=True, index=True)

    # ─── 时间戳 ───────────────────────────────────────────────────────────────
    created_at = Column(
        DateTime(timezone=True),
        default=lambda: datetime.now(UTC),
        nullable=False,
    )

    def __repr__(self) -> str:
        return f"<RawCase(case_id={self.case_id}, quality_score={self.quality_score})>"
