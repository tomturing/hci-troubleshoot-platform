"""Context Window 可观测性：prompt_audit 追加 context_breakdown 字段

Revision ID: 0004
Revises: 0003
Create Date: 2026-03-24

变更内容：
  prompt_audit 表新增两列：
    - context_breakdown  JSONB  — 每个 Segment 的编码、名称、字符数、token 估算
      格式：[{"code":"A1","name":"专家身份定义","chars":300,"token_est":75}, ...]
    - total_token_est    INT    — 本次请求全部 Segment 的 token 估算总量（4 chars/token）

  注：system_prompt_chars 字段（旧字段）保留，其含义与 total_chars 相同，
  由 insert_prompt_audit 写入。

背景：
  解决 Context Window 黑盒问题。每次 AI 请求都会通过 StructuredLogger 的
  debug 日志（event=context_window_breakdown）输出分段明细，同时 100% 落库
  到 prompt_audit.context_breakdown，可通过 Loki 或 SQL 分析各 Segment
  的 token 占比趋势。

降级：
  直接 DROP COLUMN，无数据迁移风险。
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "0004"
down_revision = "0003"
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table("prompt_audit") as batch_op:
        batch_op.add_column(
            sa.Column(
                "context_breakdown",
                sa.dialects.postgresql.JSONB(astext_type=sa.Text()),
                nullable=True,
                comment=(
                    "Context Window 分段明细（100% 覆盖），格式："
                    '[{"code":"A1","name":"专家身份定义","chars":300,"token_est":75},...]'
                ),
            )
        )
        batch_op.add_column(
            sa.Column(
                "total_token_est",
                sa.Integer(),
                nullable=True,
                comment="本次请求全部 Segment token 估算总量（4 chars/token），用于趋势监控",
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("prompt_audit") as batch_op:
        batch_op.drop_column("total_token_est")
        batch_op.drop_column("context_breakdown")
