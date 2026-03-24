"""知识原子数据库迁移 v2：新增 knowledge_atoms / error_code_index / raw_cases 三张表

Revision ID: 0002
Revises: 0001
Create Date: 2026-06-26

变更内容：
  1. knowledge_atoms     — 知识原子主表，含 pgvector(1536) embedding
  2. error_code_index    — 错误码 → 知识原子快速映射
  3. raw_cases           — 原始案例暂存表（待 VisionProcessor 解析图片）

索引策略：
  - knowledge_atoms.embedding  : ivfflat（余弦距离），列表数量 100
  - knowledge_atoms.type       : btree
  - knowledge_atoms.category_id: btree
  - knowledge_atoms.trigger    : GIN（JSONB 路径检索）
  - knowledge_atoms.trace_id   : btree

降级说明：
  downgrade 直接 DROP TABLE（CASCADE），不提供数据迁移回滚。
  如需保留数据，请在执行 downgrade 前手动备份。
"""

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects.postgresql import JSONB

# revision identifiers
revision = "0002"
down_revision = "0001"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # ─────────────────────────────────────────────────────────────────────────
    # 确保 pgvector 扩展已启用（幂等操作）
    # ─────────────────────────────────────────────────────────────────────────
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # ─────────────────────────────────────────────────────────────────────────
    # 1. knowledge_atoms — 知识原子主表
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "knowledge_atoms",
        sa.Column("id", sa.String(64), primary_key=True, comment="主键，格式 ka-{uuid12}"),
        sa.Column("type", sa.String(32), nullable=False,
                  comment="知识类型: diagnostic_step|fix_action|decision_gate|background|escalation"),
        sa.Column("category_id", sa.String(32), nullable=True,
                  comment="分类 ID，对应 category_baseline.yaml，如 虚拟机-003"),
        sa.Column("knowledge_domain", sa.String(16), nullable=False, server_default="sop",
                  comment="知识领域: sop|case|inferred"),
        sa.Column("trigger", JSONB(astext_type=sa.Text()), nullable=True,
                  comment='触发条件: {"stage": "S2", "task_error_keywords": [...]}'),
        sa.Column("content", JSONB(astext_type=sa.Text()), nullable=False,
                  comment='内容正文: {"description": "...", "full_text": "...", "commands": [...]}'),
        sa.Column("applicable_version_min", sa.String(20), nullable=True,
                  comment="适用 HCI 版本下限，如 6.0.0"),
        sa.Column("applicable_version_max", sa.String(20), nullable=True,
                  comment="适用 HCI 版本上限，None 表示不限"),
        sa.Column("source_type", sa.String(20), nullable=True,
                  comment="来源类型: docx|md|case|manual"),
        sa.Column("source_ref", sa.Text(), nullable=True,
                  comment="原始文件路径或 URL"),
        sa.Column("confidence", sa.Float(), nullable=False, server_default="0.8",
                  comment="置信度 0.0-1.0"),
        sa.Column("verified", sa.Boolean(), nullable=False, server_default="false",
                  comment="是否经人工审核"),
        sa.Column("embedding", Vector(1536), nullable=True,
                  comment="1536 维向量（text-embedding-3-small/large）"),
        sa.Column("usage_count", sa.Integer(), nullable=False, server_default="0",
                  comment="被召回次数"),
        sa.Column("feedback_positive", sa.Integer(), nullable=False, server_default="0",
                  comment="正向反馈数"),
        sa.Column("feedback_negative", sa.Integer(), nullable=False, server_default="0",
                  comment="负向反馈数"),
        sa.Column("trace_id", sa.String(55), nullable=True,
                  comment="W3C traceparent 格式调用链 ID"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()"), comment="更新时间"),
    )

    # 普通 btree 索引
    op.create_index("ix_ka_type", "knowledge_atoms", ["type"])
    op.create_index("ix_ka_category_id", "knowledge_atoms", ["category_id"])
    op.create_index("ix_ka_trace_id", "knowledge_atoms", ["trace_id"])

    # GIN 索引：JSONB trigger 字段（支持 @> 包含查询）
    op.execute(
        "CREATE INDEX ix_ka_trigger_gin ON knowledge_atoms USING GIN (trigger)"
    )

    # IVFFlat 向量索引（余弦距离，lists=100 适合约 1-10 万向量）
    # 注意：IVFFlat 需要表中有数据才能生效，初始为空表时创建仍合法
    op.execute(
        "CREATE INDEX ix_ka_embedding_ivfflat ON knowledge_atoms "
        "USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100)"
    )

    # ─────────────────────────────────────────────────────────────────────────
    # 2. error_code_index — 错误码 → 知识原子快速映射
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "error_code_index",
        sa.Column("error_code", sa.String(32), primary_key=True,
                  comment="错误码，格式 0xHHHHHHHH（大写）"),
        sa.Column("description", sa.Text(), nullable=True,
                  comment="错误码含义说明"),
        sa.Column("category_ids", JSONB(astext_type=sa.Text()), nullable=True,
                  comment='关联分类 ID 列表，如 ["虚拟机-003"]'),
        sa.Column("knowledge_atom_ids", JSONB(astext_type=sa.Text()), nullable=True,
                  comment='关联知识原子 ID 列表'),
        sa.Column("source", sa.String(16), nullable=False, server_default="'manual'",
                  comment="来源: manual|auto_extracted|vendor_doc"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()"), comment="创建时间"),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()"), comment="更新时间"),
    )

    # ─────────────────────────────────────────────────────────────────────────
    # 3. raw_cases — 原始案例暂存表
    # ─────────────────────────────────────────────────────────────────────────
    op.create_table(
        "raw_cases",
        sa.Column("case_id", sa.String(64), primary_key=True,
                  comment="来自数据源的原始 ID"),
        sa.Column("source_url", sa.Text(), nullable=True,
                  comment="案例来源 URL"),
        sa.Column("title", sa.Text(), nullable=True,
                  comment="案例标题"),
        sa.Column("content_text", sa.Text(), nullable=True,
                  comment="Markdown 正文，图片文字由 VisionProcessor 异步填充"),
        sa.Column("images", JSONB(astext_type=sa.Text()), nullable=True,
                  comment='图片元数据: [{"url": "...", "ocr_text": "..."}]'),
        sa.Column("classification", JSONB(astext_type=sa.Text()), nullable=True,
                  comment='分类结果: {"category_id": "...", "confidence": 0.92, "top3": [...]}'),
        sa.Column("quality_score", sa.Float(), nullable=True,
                  comment="质量评分 0.0-1.0，由 enricher.py 计算"),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True,
                  comment="最后处理时间，None 表示待处理"),
        sa.Column("trace_id", sa.String(55), nullable=True,
                  comment="W3C traceparent 格式调用链 ID"),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False,
                  server_default=sa.text("NOW()"), comment="创建时间"),
    )

    op.create_index("ix_rc_trace_id", "raw_cases", ["trace_id"])


def downgrade() -> None:
    # 注意：DROP TABLE CASCADE 会级联删除所有依赖对象（外键等）
    # 执行 downgrade 前请确认数据已备份
    op.drop_table("raw_cases")
    op.drop_table("error_code_index")

    # 先删除索引再删除表（IVFFlat 索引不支持 CASCADE）
    op.execute("DROP INDEX IF EXISTS ix_ka_embedding_ivfflat")
    op.execute("DROP INDEX IF EXISTS ix_ka_trigger_gin")
    op.drop_index("ix_ka_trace_id", table_name="knowledge_atoms")
    op.drop_index("ix_ka_category_id", table_name="knowledge_atoms")
    op.drop_index("ix_ka_type", table_name="knowledge_atoms")
    op.drop_table("knowledge_atoms")
