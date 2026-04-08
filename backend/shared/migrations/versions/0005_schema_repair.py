"""Schema 修复迁移 — 补齐 ORM 模型与实际 DB 之间的结构漂移

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-07
Updated: 2026-04-08

重要说明（2026-04-08 更新）：
- Alembic 迁移链已废弃，dbmate 是唯一权威迁移工具。
- 本文件保留作历史记录，不再被 K8s Job 执行（hci-platform-data db-migrate Job 已停用）。
- 原 §5-§8（创建废弃表 kb_document/kb_chunk/kb_sop_node/kb_synonym/raw_cases/
  knowledge_atoms/prompt_audit）已删除，这些表已由 dbmate 路径负责清理。
- 如需了解当前权威 schema，请查阅 database/migrations/ 目录。

保留变更（对 ORM 运行时有效的结构补列）：
  §1. 新建 customer 表
  §2. case 表补列：close_reason, customer_id
  §3. conversation 表补列：repeat_question_count, diagnostic 系列, pending_resolution
  §4. assistant_evaluation 表补列：评分评价体系 6 字段
  §5. 新建 tool_result（v6.2）
  §6. 新建 diagnostic_item（v6.2）
"""

from alembic import op

revision = "0005"
down_revision = "0004"
branch_labels = None
depends_on = None


def upgrade() -> None:
    # §1. customer 表
    op.execute("""
        CREATE TABLE IF NOT EXISTS customer (
            customer_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            code VARCHAR(64) UNIQUE,
            name VARCHAR(200) NOT NULL,
            short_name VARCHAR(100),
            product_version VARCHAR(50),
            region VARCHAR(100),
            industry VARCHAR(100),
            metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            trace_id VARCHAR(64)
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_customer_code ON customer(code)")
    op.execute("CREATE INDEX IF NOT EXISTS idx_customer_name ON customer(name)")
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_trigger WHERE tgname = 'update_customer_updated_at') THEN
                CREATE TRIGGER update_customer_updated_at BEFORE UPDATE ON customer
                    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
            END IF;
        END $$
    """)

    # §2. case 表补列
    op.execute('ALTER TABLE "case" ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20)')
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conrelid = '"case"'::regclass AND contype = 'c'
                  AND pg_get_constraintdef(oid) LIKE '%%close_reason%%'
            ) THEN
                ALTER TABLE "case" ADD CONSTRAINT case_close_reason_check
                    CHECK (close_reason IN ('user_command','timeout','abandon','admin_close','escalated','s0_classification_failed'));
            END IF;
        END $$
    """)
    op.execute('ALTER TABLE "case" ADD COLUMN IF NOT EXISTS customer_id UUID')
    op.execute("""
        DO $$ BEGIN
            IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'case_customer_id_fkey') THEN
                ALTER TABLE "case" ADD CONSTRAINT case_customer_id_fkey
                    FOREIGN KEY (customer_id) REFERENCES customer(customer_id) ON DELETE SET NULL;
            END IF;
        END $$
    """)
    op.execute('CREATE INDEX IF NOT EXISTS idx_case_customer_id ON "case"(customer_id)')

    # §3. conversation 表补列
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER NOT NULL DEFAULT 0")
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS diagnostic_stage VARCHAR(8) NOT NULL DEFAULT 'S0'")
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS category_l1 VARCHAR(100)")
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS category_l2 VARCHAR(100)")
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS category_id VARCHAR(32)")
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS pending_confirm JSONB")
    op.execute("ALTER TABLE conversation ADD COLUMN IF NOT EXISTS pending_resolution JSONB")
    op.execute("CREATE INDEX IF NOT EXISTS idx_conversation_diagnostic_stage ON conversation(diagnostic_stage)")

    # §4. assistant_evaluation 表补列
    op.execute("ALTER TABLE assistant_evaluation ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20)")
    op.execute("ALTER TABLE assistant_evaluation ADD COLUMN IF NOT EXISTS session_duration_sec INTEGER")
    op.execute("ALTER TABLE assistant_evaluation ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER")
    op.execute("ALTER TABLE assistant_evaluation ADD COLUMN IF NOT EXISTS composite_score SMALLINT")
    op.execute("ALTER TABLE assistant_evaluation ADD COLUMN IF NOT EXISTS score_breakdown JSONB")
    op.execute("ALTER TABLE assistant_evaluation ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMPTZ")
    op.execute("CREATE INDEX IF NOT EXISTS idx_eval_composite_score ON assistant_evaluation(composite_score) WHERE composite_score IS NOT NULL")
    op.execute("CREATE INDEX IF NOT EXISTS idx_eval_close_reason ON assistant_evaluation(close_reason) WHERE close_reason IS NOT NULL")

    # §5. tool_result（v6.2）— 原 §9
    op.execute("""
        CREATE TABLE IF NOT EXISTS tool_result (
            id VARCHAR(36) PRIMARY KEY,
            conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
            tool_name VARCHAR(100) NOT NULL, tool_type VARCHAR(20) NOT NULL,
            step_no SMALLINT, risk_level SMALLINT NOT NULL DEFAULT 1,
            policy VARCHAR(20) NOT NULL, authorized_by VARCHAR(100),
            input_json JSONB NOT NULL DEFAULT '{}', output_json JSONB, error TEXT,
            started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), completed_at TIMESTAMPTZ,
            duration_ms INTEGER, trace_id VARCHAR(64)
        )
    """)

    # §6. diagnostic_item（v6.2）— 原 §10
    op.execute("""
        CREATE TABLE IF NOT EXISTS diagnostic_item (
            id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
            stage VARCHAR(5) NOT NULL, type VARCHAR(30) NOT NULL,
            seq SMALLINT NOT NULL DEFAULT 1, content JSONB NOT NULL DEFAULT '{}',
            probability FLOAT, status VARCHAR(20) NOT NULL DEFAULT 'pending',
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), trace_id VARCHAR(64)
        )
    """)


def downgrade() -> None:
    # 修复迁移不提供自动降级
    pass
