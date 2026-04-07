"""Schema 修复迁移 — 补齐 ORM 模型与实际 DB 之间的所有漂移

Revision ID: 0005
Revises: 0004
Create Date: 2026-04-07

与 dbmate 的 20260407001_schema_repair.sql 等价。
所有操作使用 IF NOT EXISTS，可安全重复执行（幂等）。

变更内容：
  §1. 新建 customer 表
  §2. case 表补列：close_reason, customer_id
  §3. conversation 表补列：repeat_question_count, diagnostic 系列, pending_resolution
  §4. assistant_evaluation 表补列：评分评价体系 6 字段
  §5. 新建 kb_document / kb_chunk（v3.0）
  §6. 新建 kb_sop_node / kb_synonym
  §7. 新建 raw_cases / knowledge_atoms
  §8. 新建 prompt_audit
  §9. 新建 tool_result（v6.2）
  §10. 新建 diagnostic_item（v6.2）
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

    # §5. kb_document / kb_chunk（v3.0）
    op.execute("""
        CREATE TABLE IF NOT EXISTS kb_document (
            id SERIAL PRIMARY KEY, source_id VARCHAR(50) UNIQUE,
            title VARCHAR(500) NOT NULL, product VARCHAR(100) DEFAULT '超融合HCI',
            content_md TEXT NOT NULL, content_hash VARCHAR(64), yaml_meta JSONB,
            category_l1 VARCHAR(100), category_l2 VARCHAR(100), tags TEXT[],
            judgment_logic TEXT, summary TEXT, difficulty SMALLINT DEFAULT 3,
            status VARCHAR(20) DEFAULT 'draft', review_note TEXT, reviewer VARCHAR(100),
            reviewed_at TIMESTAMPTZ, source_type VARCHAR(20) DEFAULT 'kb',
            has_images BOOLEAN DEFAULT FALSE, verified_version VARCHAR(50),
            trace_id VARCHAR(64),
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS kb_chunk (
            id SERIAL PRIMARY KEY,
            document_id INTEGER NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
            chunk_index SMALLINT NOT NULL, content TEXT NOT NULL,
            embedding vector(384), token_count SMALLINT, metadata JSONB,
            tsv tsvector, trace_id VARCHAR(64),
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("CREATE INDEX IF NOT EXISTS idx_kb_chunk_document ON kb_chunk(document_id)")

    # §6. kb_sop_node / kb_synonym
    op.execute("""
        CREATE TABLE IF NOT EXISTS kb_sop_node (
            id SERIAL PRIMARY KEY, skill_id VARCHAR(100) NOT NULL,
            node_name VARCHAR(200) NOT NULL, parent_id INTEGER REFERENCES kb_sop_node(id),
            keywords TEXT[] NOT NULL, file_path VARCHAR(500), content TEXT,
            level SMALLINT DEFAULT 1, sort_order SMALLINT DEFAULT 0,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS kb_synonym (
            id SERIAL PRIMARY KEY, term VARCHAR(100) NOT NULL,
            canonical VARCHAR(100) NOT NULL,
            created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
            UNIQUE(term, canonical)
        )
    """)

    # §7. raw_cases / knowledge_atoms
    op.execute("""
        CREATE TABLE IF NOT EXISTS raw_cases (
            id BIGSERIAL PRIMARY KEY, case_id VARCHAR(64) NOT NULL UNIQUE,
            source_url TEXT NOT NULL DEFAULT '', content_text TEXT NOT NULL DEFAULT '',
            images JSONB NOT NULL DEFAULT '[]', classification VARCHAR(128) NOT NULL DEFAULT '',
            quality_score SMALLINT NOT NULL DEFAULT 0,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
        )
    """)
    op.execute("""
        CREATE TABLE IF NOT EXISTS knowledge_atoms (
            id VARCHAR(32) PRIMARY KEY, atom_type VARCHAR(32) NOT NULL,
            category_id VARCHAR(64) NOT NULL DEFAULT '', trigger_json JSONB NOT NULL DEFAULT '{}',
            content_json JSONB NOT NULL DEFAULT '{}', source_type VARCHAR(16) NOT NULL DEFAULT 'session',
            source_ref VARCHAR(64) NOT NULL DEFAULT '', verified BOOLEAN NOT NULL DEFAULT FALSE,
            confidence NUMERIC(3,2) NOT NULL DEFAULT 0.70,
            created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(), updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
            verified_at TIMESTAMPTZ, verified_by VARCHAR(64)
        )
    """)

    # §8. prompt_audit
    op.execute("""
        CREATE TABLE IF NOT EXISTS prompt_audit (
            audit_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
            conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
            case_id VARCHAR(20), assistant_type VARCHAR(50), model VARCHAR(100),
            message_count INTEGER, has_sop BOOLEAN DEFAULT FALSE,
            kb_chunks_count INTEGER DEFAULT 0, kb_top_score FLOAT DEFAULT 0.0,
            system_prompt_chars INTEGER, messages JSONB, payload_ref VARCHAR(200),
            user_rating SMALLINT CHECK (user_rating >= 1 AND user_rating <= 5),
            context_breakdown JSONB, total_token_est INTEGER,
            captured_at TIMESTAMPTZ DEFAULT NOW(), trace_id VARCHAR(64)
        )
    """)

    # §9. tool_result（v6.2）
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

    # §10. diagnostic_item（v6.2）
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
