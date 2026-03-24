"""增加诊断状态字段和工具审计表

Revision ID: 0003
Revises: 0002
Create Date: 2026-03-23

变更内容：
  1. conversation 表新增 7 个诊断状态字段：
     - diagnostic_stage : 当前诊断阶段 (S0-S6)
     - category_l1      : 一级分类
     - category_l2      : 二级分类
     - category_id      : 分类 ID（对应 category_baseline.yaml）
     - hypothesis       : 当前假设列表 (JSON)
     - react_state      : ReAct 执行器状态快照 (JSON)
     - pending_confirm  : 待用户确认的工具调用 (JSON)
  2. 新建 tool_audit_log 表（生产安全要求，不可绕过）

降级说明：
  - 删除 tool_audit_log 表
  - 从 conversation 表移除上述 7 个字段
"""

import sqlalchemy as sa
from alembic import op

# revision identifiers
revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # 扩展 conversation 表，新增诊断状态字段
    with op.batch_alter_table("conversation") as batch_op:
        batch_op.add_column(
            sa.Column(
                "diagnostic_stage",
                sa.String(8),
                server_default="S0",
                nullable=False,
                comment="当前诊断阶段：S0意图识别, S1故障定位, S2假设生成, "
                "S3验证执行, S4根因确认, S5方案输出, S6验证闭环",
            )
        )
        batch_op.add_column(
            sa.Column(
                "category_l1",
                sa.String(100),
                nullable=True,
                comment="一级分类，如：虚拟机/存储/网络/硬件/平台",
            )
        )
        batch_op.add_column(
            sa.Column(
                "category_l2",
                sa.String(100),
                nullable=True,
                comment="二级分类，如：虚拟机开机失败",
            )
        )
        batch_op.add_column(
            sa.Column(
                "category_id",
                sa.String(32),
                nullable=True,
                comment="分类 ID，对应 category_baseline.yaml",
            )
        )
        batch_op.add_column(
            sa.Column(
                "hypothesis",
                sa.JSON,
                server_default="[]",
                nullable=True,
                comment="当前假设列表，格式：[{id, description, confidence, status}]",
            )
        )
        batch_op.add_column(
            sa.Column(
                "react_state",
                sa.JSON,
                server_default="{}",
                nullable=True,
                comment="ReAct 执行器状态快照，用于断点续接",
            )
        )
        batch_op.add_column(
            sa.Column(
                "pending_confirm",
                sa.JSON,
                nullable=True,
                comment="待用户确认的工具调用，格式：{tool_call_id, tool_name, args, risk_level}",
            )
        )

    # 工具调用审计表（生产安全要求，所有工具调用都必须记录，不可绕过）
    op.create_table(
        "tool_audit_log",
        sa.Column("id", sa.String(36), primary_key=True, comment="审计记录 UUID"),
        sa.Column("session_id", sa.String(36), nullable=False, comment="关联的会话 ID"),
        sa.Column("trace_id", sa.String(55), nullable=True, comment="W3C traceparent"),
        sa.Column("tool_name", sa.String(100), nullable=False, comment="工具名称，如 get_active_alerts / acli_vm_status"),
        sa.Column("tool_args", sa.JSON, nullable=False, comment="工具调用参数"),
        sa.Column(
            "risk_level",
            sa.Integer,
            nullable=False,
            comment="风险等级：1只读/2写操作需确认/3高危禁止",
        ),
        sa.Column(
            "policy",
            sa.String(20),
            nullable=True,
            comment="执行策略：auto|notify|confirm|block",
        ),
        sa.Column(
            "authorized_by",
            sa.String(100),
            nullable=True,
            comment="人工确认时的用户 ID（risk_level>=2 必填）",
        ),
        sa.Column("result", sa.JSON, nullable=True, comment="工具执行结果（成功时）"),
        sa.Column("error", sa.Text, nullable=True, comment="执行错误信息（失败时）"),
        sa.Column(
            "started_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="开始执行时间",
        ),
        sa.Column(
            "completed_at",
            sa.TIMESTAMP(timezone=True),
            nullable=True,
            comment="执行完成时间",
        ),
        sa.Column("duration_ms", sa.Integer, nullable=True, comment="执行耗时（毫秒）"),
    )

    op.create_index("ix_tool_audit_log_session_id", "tool_audit_log", ["session_id"])
    op.create_index("ix_tool_audit_log_started_at", "tool_audit_log", ["started_at"])


def downgrade() -> None:
    op.drop_index("ix_tool_audit_log_started_at", table_name="tool_audit_log")
    op.drop_index("ix_tool_audit_log_session_id", table_name="tool_audit_log")
    op.drop_table("tool_audit_log")

    with op.batch_alter_table("conversation") as batch_op:
        for col in [
            "diagnostic_stage",
            "category_l1",
            "category_l2",
            "category_id",
            "hypothesis",
            "react_state",
            "pending_confirm",
        ]:
            batch_op.drop_column(col)
