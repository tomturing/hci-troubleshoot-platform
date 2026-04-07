-- migrate:up

-- =============================================================================
-- Schema 修复迁移 — 补齐 ORM 模型与实际 DB 之间的所有漂移
-- 日期：2026-04-07
-- 根因：schema_migrations 记录了 8 次迁移已执行，但实际 DDL 未生效
--       （DB 可能从早期备份恢复，migration 记录被带入但 DDL 未重放）
--
-- 修复内容：
--   §1  新建 customer 表（case.customer_id FK 目标）
--   §2  case 表补列（close_reason, customer_id）
--   §3  conversation 表补列（diagnostic 系列 + pending_resolution）
--   §4  assistant_evaluation 表补列（评分评价体系）
--   §5  新建 kb_document / kb_chunk（v3.0 格式）
--   §6  新建 kb_sop_node / kb_synonym
--   §7  新建 raw_cases / knowledge_atoms（P4 管道）
--   §8  新建 prompt_audit（评分审计）
--   §9  新建 tool_result（v6.2 工具执行记录）
--   §10 新建 diagnostic_item（v6.2 诊断子表）
--
-- 幂等保证：
--   - 所有 CREATE TABLE 使用 IF NOT EXISTS
--   - 所有 ALTER TABLE ADD COLUMN 使用 IF NOT EXISTS
--   - FK / CHECK 约束通过 DO $$ 块检测后添加
--   - 全量索引使用 IF NOT EXISTS
--   新环境 / 已修复环境可安全重复执行
-- =============================================================================

-- ============================================================================
-- §1. 新建 customer 表
-- ============================================================================

CREATE TABLE IF NOT EXISTS customer (
    customer_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code          VARCHAR(64)  UNIQUE,                          -- 外部系统客户 ID（幂等键）
    name          VARCHAR(200) NOT NULL,                        -- 客户全称
    short_name    VARCHAR(100),                                 -- 客户简称
    product_version VARCHAR(50),                                -- HCI 产品版本
    region        VARCHAR(100),                                 -- 所在区域
    industry      VARCHAR(100),                                 -- 所属行业
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,           -- 扩展元数据
    created_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    trace_id      VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_customer_code ON customer(code);
CREATE INDEX IF NOT EXISTS idx_customer_name ON customer(name);
CREATE INDEX IF NOT EXISTS idx_customer_product_version ON customer(product_version);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'update_customer_updated_at'
    ) THEN
        CREATE TRIGGER update_customer_updated_at
            BEFORE UPDATE ON customer
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END $$;

COMMENT ON TABLE customer IS '客户档案表 — HCI 产品采购方（公司/单位级别），与 user 独立';

-- ============================================================================
-- §2. case 表补列
-- ============================================================================

-- 2.1 close_reason：工单关闭原因（被动信号轨道核心数据）
ALTER TABLE "case"
    ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20);

-- 添加 CHECK 约束（包含 v6.3 新增的 escalated / s0_classification_failed）
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = '"case"'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%close_reason%'
    ) THEN
        ALTER TABLE "case" ADD CONSTRAINT case_close_reason_check
            CHECK (close_reason IN (
                'user_command', 'timeout', 'abandon', 'admin_close',
                'escalated', 's0_classification_failed'
            ));
    END IF;
END $$;

COMMENT ON COLUMN "case".close_reason IS '工单关闭原因：user_command/timeout/abandon/admin_close/escalated/s0_classification_failed';

-- 2.2 customer_id：关联客户档案（可选，ON DELETE SET NULL）
ALTER TABLE "case"
    ADD COLUMN IF NOT EXISTS customer_id UUID;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'case_customer_id_fkey'
    ) THEN
        ALTER TABLE "case"
            ADD CONSTRAINT case_customer_id_fkey
            FOREIGN KEY (customer_id) REFERENCES customer(customer_id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_case_customer_id ON "case"(customer_id);

COMMENT ON COLUMN "case".customer_id IS '关联客户档案（可选），用于按客户维度聚合工单';

-- ============================================================================
-- §3. conversation 表补列
-- ============================================================================

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS diagnostic_stage VARCHAR(8) NOT NULL DEFAULT 'S0';

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS category_l1 VARCHAR(100);

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS category_l2 VARCHAR(100);

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS category_id VARCHAR(32);

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS pending_confirm JSONB;

-- v6.3 新增：S6 验证闭环后等待用户选择的快照（A=已解决/B=未解决/C=升级人工）
ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS pending_resolution JSONB;

CREATE INDEX IF NOT EXISTS idx_conversation_diagnostic_stage ON conversation(diagnostic_stage);

COMMENT ON COLUMN conversation.diagnostic_stage IS 'P4 诊断阶段: S0→S1→S2→S3→S4→S5→S6';
COMMENT ON COLUMN conversation.pending_confirm IS '待用户确认的工具调用（S3/S5 高危工具），断线重连恢复锚点';
COMMENT ON COLUMN conversation.pending_resolution IS 'S6 验证闭环后等待用户选择 A/B/C 的快照';

-- ============================================================================
-- §4. assistant_evaluation 表补列
-- ============================================================================

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20);

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS session_duration_sec INTEGER;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS composite_score SMALLINT;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS score_breakdown JSONB;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_eval_composite_score
    ON assistant_evaluation(composite_score) WHERE composite_score IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eval_close_reason
    ON assistant_evaluation(close_reason) WHERE close_reason IS NOT NULL;

COMMENT ON COLUMN assistant_evaluation.composite_score IS '综合质量分 0-100，QualityScoreService 计算';
COMMENT ON COLUMN assistant_evaluation.score_breakdown IS '各维度分解：{"close_intent":90,"efficiency":70,"user_rating":80,"ai_quality":65}';

-- ============================================================================
-- §5. 新建 kb_document / kb_chunk（v3.0 格式）
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb_document (
    id              SERIAL PRIMARY KEY,
    source_id       VARCHAR(50) UNIQUE,
    title           VARCHAR(500) NOT NULL,
    product         VARCHAR(100) DEFAULT '超融合HCI',
    content_md      TEXT NOT NULL,
    content_hash    VARCHAR(64),
    yaml_meta       JSONB,
    category_l1     VARCHAR(100),
    category_l2     VARCHAR(100),
    tags            TEXT[],
    judgment_logic  TEXT,
    summary         TEXT,
    difficulty      SMALLINT DEFAULT 3,
    status          VARCHAR(20) DEFAULT 'draft',
    review_note     TEXT,
    reviewer        VARCHAR(100),
    reviewed_at     TIMESTAMPTZ,
    source_type     VARCHAR(20) DEFAULT 'kb',
    has_images      BOOLEAN DEFAULT FALSE,
    verified_version VARCHAR(50),
    trace_id        VARCHAR(64),
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_document_status     ON kb_document(status);
CREATE INDEX IF NOT EXISTS idx_kb_document_category   ON kb_document(category_l1, category_l2);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_id  ON kb_document(source_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_type ON kb_document(source_type);
CREATE INDEX IF NOT EXISTS idx_kb_document_trace_id   ON kb_document(trace_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_created_at ON kb_document(created_at DESC);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'update_kb_document_updated_at'
    ) THEN
        CREATE TRIGGER update_kb_document_updated_at
            BEFORE UPDATE ON kb_document
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END $$;

COMMENT ON TABLE kb_document IS '知识库文档表 v3.0 — 状态机驱动的文档生命周期管理';

CREATE TABLE IF NOT EXISTS kb_chunk (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    chunk_index     SMALLINT NOT NULL,
    content         TEXT NOT NULL,
    embedding       vector(384),
    token_count     SMALLINT,
    metadata        JSONB,
    tsv             tsvector,
    trace_id        VARCHAR(64),
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_document    ON kb_chunk(document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_position    ON kb_chunk(document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_tsv         ON kb_chunk USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_trace_id    ON kb_chunk(trace_id);

COMMENT ON TABLE kb_chunk IS '知识库分块+向量表 v3.0 — BM25 + pgvector 双路检索';

-- ============================================================================
-- §6. 新建 kb_sop_node / kb_synonym
-- ============================================================================

CREATE TABLE IF NOT EXISTS kb_sop_node (
    id              SERIAL PRIMARY KEY,
    skill_id        VARCHAR(100) NOT NULL,
    node_name       VARCHAR(200) NOT NULL,
    parent_id       INTEGER REFERENCES kb_sop_node(id),
    keywords        TEXT[] NOT NULL,
    file_path       VARCHAR(500),
    content         TEXT,
    level           SMALLINT DEFAULT 1,
    sort_order      SMALLINT DEFAULT 0,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_sop_node_skill    ON kb_sop_node(skill_id);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_keywords ON kb_sop_node USING GIN (keywords);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_parent   ON kb_sop_node(parent_id);

CREATE TABLE IF NOT EXISTS kb_synonym (
    id              SERIAL PRIMARY KEY,
    term            VARCHAR(100) NOT NULL,
    canonical       VARCHAR(100) NOT NULL,
    created_at      TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(term, canonical)
);

-- ============================================================================
-- §7. 新建 raw_cases / knowledge_atoms（P4 数据管道）
-- ============================================================================

CREATE TABLE IF NOT EXISTS raw_cases (
    id             BIGSERIAL    PRIMARY KEY,
    case_id        VARCHAR(64)  NOT NULL UNIQUE,
    source_url     TEXT         NOT NULL DEFAULT '',
    content_text   TEXT         NOT NULL DEFAULT '',
    images         JSONB        NOT NULL DEFAULT '[]',
    classification VARCHAR(128) NOT NULL DEFAULT '',
    quality_score  SMALLINT     NOT NULL DEFAULT 0,
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_raw_cases_classification ON raw_cases(classification);
CREATE INDEX IF NOT EXISTS idx_raw_cases_quality        ON raw_cases(quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_raw_cases_created_at     ON raw_cases(created_at DESC);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_raw_cases_updated_at'
    ) THEN
        CREATE OR REPLACE FUNCTION update_raw_cases_updated_at()
        RETURNS TRIGGER AS $fn$
        BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
        $fn$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_raw_cases_updated_at
            BEFORE UPDATE ON raw_cases
            FOR EACH ROW EXECUTE FUNCTION update_raw_cases_updated_at();
    END IF;
END $$;

COMMENT ON TABLE raw_cases IS 'HCI 历史工单原始数据（已脱敏），供知识库提炼使用';

CREATE TABLE IF NOT EXISTS knowledge_atoms (
    id              VARCHAR(32)  PRIMARY KEY,
    atom_type       VARCHAR(32)  NOT NULL,
    category_id     VARCHAR(64)  NOT NULL DEFAULT '',
    trigger_json    JSONB        NOT NULL DEFAULT '{}',
    content_json    JSONB        NOT NULL DEFAULT '{}',
    source_type     VARCHAR(16)  NOT NULL DEFAULT 'session',
    source_ref      VARCHAR(64)  NOT NULL DEFAULT '',
    verified        BOOLEAN      NOT NULL DEFAULT FALSE,
    confidence      NUMERIC(3,2) NOT NULL DEFAULT 0.70,
    created_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    verified_at     TIMESTAMPTZ,
    verified_by     VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_verified  ON knowledge_atoms(verified, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_category  ON knowledge_atoms(category_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_source    ON knowledge_atoms(source_ref);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_atom_type ON knowledge_atoms(atom_type);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'trg_knowledge_atoms_updated_at'
    ) THEN
        CREATE OR REPLACE FUNCTION update_knowledge_atoms_updated_at()
        RETURNS TRIGGER AS $fn$
        BEGIN NEW.updated_at = NOW(); RETURN NEW; END;
        $fn$ LANGUAGE plpgsql;

        CREATE TRIGGER trg_knowledge_atoms_updated_at
            BEFORE UPDATE ON knowledge_atoms
            FOR EACH ROW EXECUTE FUNCTION update_knowledge_atoms_updated_at();
    END IF;
END $$;

COMMENT ON TABLE knowledge_atoms IS 'AI 自动提炼的知识原子候选，verified=false 时待人工审核';

-- ============================================================================
-- §8. 新建 prompt_audit 表
-- ============================================================================

CREATE TABLE IF NOT EXISTS prompt_audit (
    audit_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id     UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    case_id             VARCHAR(20),
    assistant_type      VARCHAR(50),
    model               VARCHAR(100),
    message_count       INTEGER,
    has_sop             BOOLEAN DEFAULT FALSE,
    kb_chunks_count     INTEGER DEFAULT 0,
    kb_top_score        FLOAT DEFAULT 0.0,
    system_prompt_chars INTEGER,
    messages            JSONB,
    payload_ref         VARCHAR(200),
    user_rating         SMALLINT CHECK (user_rating >= 1 AND user_rating <= 5),
    context_breakdown   JSONB,
    total_token_est     INTEGER,
    captured_at         TIMESTAMPTZ DEFAULT NOW(),
    trace_id            VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_prompt_audit_case        ON prompt_audit(case_id);
CREATE INDEX IF NOT EXISTS idx_prompt_audit_has_sop     ON prompt_audit(has_sop) WHERE has_sop = FALSE;
CREATE INDEX IF NOT EXISTS idx_prompt_audit_rating      ON prompt_audit(user_rating) WHERE user_rating IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_prompt_audit_captured_at ON prompt_audit(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_prompt_audit_trace_id    ON prompt_audit(trace_id);

COMMENT ON TABLE prompt_audit IS 'AI 层 Prompt 审计镜像：元数据 100% 采集，payload 按比例采样';

-- ============================================================================
-- §9. 新建 tool_result 表（v6.2 — 替代已废弃的 tool_audit_log）
-- ============================================================================

CREATE TABLE IF NOT EXISTS tool_result (
    id                VARCHAR(36)   PRIMARY KEY,
    conversation_id   UUID          NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    tool_name         VARCHAR(100)  NOT NULL,
    tool_type         VARCHAR(20)   NOT NULL,
    step_no           SMALLINT,                                    -- BUG-03 修复：关联 diagnostic_item.seq
    risk_level        SMALLINT      NOT NULL DEFAULT 1,
    policy            VARCHAR(20)   NOT NULL,
    authorized_by     VARCHAR(100),
    input_json        JSONB         NOT NULL DEFAULT '{}',
    output_json       JSONB,
    error             TEXT,
    started_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    duration_ms       INTEGER,
    trace_id          VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_tool_result_conversation ON tool_result(conversation_id);
CREATE INDEX IF NOT EXISTS idx_tool_result_tool_name    ON tool_result(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_result_trace_id     ON tool_result(trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_result_started_at   ON tool_result(started_at DESC);

COMMENT ON TABLE tool_result IS 'ReAct 工具执行记录（v6.2，替代 tool_audit_log），含 step_no 追踪';

-- ============================================================================
-- §10. 新建 diagnostic_item 表（v6.2 — 解 BUG-06 hypothesis blob 问题）
-- ============================================================================

CREATE TABLE IF NOT EXISTS diagnostic_item (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   UUID          NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    stage             VARCHAR(5)    NOT NULL,                      -- S2/S3/S4/S5
    type              VARCHAR(30)   NOT NULL,                      -- hypothesis/verification_step/root_cause/solution
    seq               SMALLINT      NOT NULL DEFAULT 1,
    content           JSONB         NOT NULL DEFAULT '{}',
    probability       FLOAT,                                       -- 仅 type=hypothesis
    status            VARCHAR(20)   NOT NULL DEFAULT 'pending',   -- pending/in_progress/confirmed/rejected/skipped/archived
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    trace_id          VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_diagnostic_item_conversation ON diagnostic_item(conversation_id);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_type         ON diagnostic_item(type);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_status       ON diagnostic_item(status);

COMMENT ON TABLE diagnostic_item IS '诊断结论子表（v6.2）— 替代 conversation.hypothesis JSONB blob';

-- ============================================================================
-- §11. 清理孤立的 schema_migrations 记录
-- ============================================================================
-- 20260401002 在 schema_migrations 中存在但 ConfigMap 中无对应文件
-- 此记录无害但会造成混淆，清理之
DELETE FROM schema_migrations WHERE version = '20260401002';

-- ============================================================================
-- 完成
-- ============================================================================

SELECT 'Schema repair migration completed — '
    || (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE')
    || ' tables now in database'
    AS status;


-- migrate:down
-- 修复迁移不提供自动降级，请参照 database/rollback_20260407001.md 手动操作
