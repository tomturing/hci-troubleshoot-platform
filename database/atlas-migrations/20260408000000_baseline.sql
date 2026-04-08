-- Atlas Baseline 迁移文件
-- 版本: 20260408000000
-- 说明: HCI 智能排障平台数据库完整建表 SQL（基于 schema v6.2）
--
-- 用途:
--   1. 全新 DB（测试环境/本地开发）：执行此文件建立完整 schema
--   2. 已有 DB（dev/staging/prod）：通过 --baseline 20260408000000 跳过此文件
--      已有 DB 的历史通过 dbmate 的 schema_migrations 表跟踪，Atlas 接管后
--      新增迁移文件将追加在此 baseline 之后
--
-- 注意:
--   - 所有 CREATE 语句使用 IF NOT EXISTS，保证幂等性
--   - schema_migrations 表不在此文件中（dbmate 工具表，已废弃）
--   - atlas_schema_revisions 表由 Atlas 自动创建管理

-- ============================================================
-- 扩展
-- ============================================================
CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "vector";

-- ============================================================
-- 自定义 ENUM 类型
-- ============================================================
DO $$ BEGIN
    CREATE TYPE case_status AS ENUM ('created', 'confirmed', 'in_progress', 'resolved', 'closed', 'cancelled');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

DO $$ BEGIN
    CREATE TYPE message_role AS ENUM ('user', 'assistant', 'system', 'command');
EXCEPTION WHEN duplicate_object THEN NULL;
END $$;

-- ============================================================
-- 触发器函数
-- ============================================================
CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION fn_update_conversation_message_count()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE conversation SET message_count = message_count + 1
            WHERE conversation_id = NEW.conversation_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE conversation SET message_count = GREATEST(message_count - 1, 0)
            WHERE conversation_id = OLD.conversation_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION generate_case_id()
RETURNS VARCHAR(20) AS $$
DECLARE
    v_today VARCHAR(8);
    v_seq   INTEGER;
BEGIN
    v_today := TO_CHAR(CURRENT_DATE, 'YYYYMMDD');
    SELECT COUNT(*) + 1 INTO v_seq FROM "case"
        WHERE case_id LIKE 'Q' || v_today || '%';
    RETURN 'Q' || v_today || LPAD(v_seq::TEXT, 5, '0');
END;
$$ LANGUAGE plpgsql;

-- ============================================================
-- 表结构（与 desired_schema.sql 完全一致，用于全新 DB 初始化）
-- ============================================================

CREATE TABLE IF NOT EXISTS "user" (
    user_id uuid NOT NULL DEFAULT gen_random_uuid(),
    client_id varchar(255) NOT NULL UNIQUE,
    username varchar(100),
    email varchar(255),
    user_type varchar(20) NOT NULL DEFAULT 'temporary',
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    last_login_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT user_pkey PRIMARY KEY (user_id)
);
CREATE INDEX IF NOT EXISTS idx_user_client_id ON "user" (client_id);
CREATE INDEX IF NOT EXISTS idx_user_trace_id ON "user" (trace_id);
CREATE INDEX IF NOT EXISTS idx_user_type ON "user" (user_type);
DROP TRIGGER IF EXISTS update_user_updated_at ON "user";
CREATE TRIGGER update_user_updated_at
    BEFORE UPDATE ON "user"
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE IF NOT EXISTS customer (
    customer_id uuid NOT NULL DEFAULT gen_random_uuid(),
    code varchar(64) UNIQUE,
    name varchar(200) NOT NULL,
    short_name varchar(100),
    product_version varchar(50),
    region varchar(100),
    industry varchar(100),
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT customer_pkey PRIMARY KEY (customer_id)
);
CREATE INDEX IF NOT EXISTS idx_customer_name ON customer (name);
CREATE INDEX IF NOT EXISTS idx_customer_code ON customer (code);
CREATE INDEX IF NOT EXISTS idx_customer_product_version ON customer (product_version);
CREATE INDEX IF NOT EXISTS idx_customer_region ON customer (region);
DROP TRIGGER IF EXISTS update_customer_updated_at ON customer;
CREATE TRIGGER update_customer_updated_at
    BEFORE UPDATE ON customer
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE IF NOT EXISTS kb_category (
    id serial NOT NULL,
    parent_id integer,
    name varchar(100) NOT NULL,
    level smallint NOT NULL,
    keywords text[],
    source varchar(50) DEFAULT 'manual',
    version varchar(20) DEFAULT '1.0',
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    code varchar(64) UNIQUE,
    domain varchar(100),
    path_labels jsonb DEFAULT '[]'::jsonb,
    embedding vector(1536),
    hit_count integer DEFAULT 0,
    is_active boolean DEFAULT true,
    CONSTRAINT fk_kb_category_parent_id FOREIGN KEY (parent_id) REFERENCES kb_category (id) ON DELETE NO ACTION,
    CONSTRAINT kb_category_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_kb_category_code ON kb_category (code);
CREATE INDEX IF NOT EXISTS idx_kb_category_parent ON kb_category (parent_id);
CREATE INDEX IF NOT EXISTS idx_kb_category_level ON kb_category (level);
CREATE INDEX IF NOT EXISTS idx_kb_category_keywords ON kb_category USING GIN (keywords);

CREATE TABLE IF NOT EXISTS "case" (
    case_id varchar(20) NOT NULL,
    user_id uuid NOT NULL,
    client_id varchar(255) NOT NULL,
    customer_id uuid,
    title varchar(500) NOT NULL,
    description text,
    status case_status NOT NULL DEFAULT 'created',
    priority varchar(20) DEFAULT 'medium',
    category varchar(100),
    assistant_type varchar(50) NOT NULL DEFAULT 'openclaw',
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    confirmed_at timestamptz,
    resolved_at timestamptz,
    closed_at timestamptz,
    close_reason varchar(20) CHECK (close_reason IN ('user_command','timeout','abandon','admin_close')),
    trace_id varchar(64),
    CONSTRAINT fk_case_user_id FOREIGN KEY (user_id) REFERENCES "user" (user_id) ON DELETE CASCADE,
    CONSTRAINT fk_case_customer_id FOREIGN KEY (customer_id) REFERENCES customer (customer_id) ON DELETE SET NULL,
    CONSTRAINT case_pkey PRIMARY KEY (case_id)
);
CREATE INDEX IF NOT EXISTS idx_case_user_id ON "case" (user_id);
CREATE INDEX IF NOT EXISTS idx_case_client_id ON "case" (client_id);
CREATE INDEX IF NOT EXISTS idx_case_status ON "case" (status);
CREATE INDEX IF NOT EXISTS idx_case_created_at ON "case" (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_case_trace_id ON "case" (trace_id);
CREATE INDEX IF NOT EXISTS idx_case_category ON "case" (category);
CREATE INDEX IF NOT EXISTS idx_case_client_status ON "case" (client_id, status);
CREATE INDEX IF NOT EXISTS idx_case_assistant_type ON "case" (assistant_type);
CREATE INDEX IF NOT EXISTS idx_case_customer_id ON "case" (customer_id) WHERE customer_id IS NOT NULL;
DROP TRIGGER IF EXISTS update_case_updated_at ON "case";
CREATE TRIGGER update_case_updated_at
    BEFORE UPDATE ON "case"
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE IF NOT EXISTS environment (
    environment_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    env_type varchar(50) NOT NULL,
    env_data jsonb NOT NULL,
    collected_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT fk_environment_case_id FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    CONSTRAINT environment_pkey PRIMARY KEY (environment_id)
);
CREATE INDEX IF NOT EXISTS idx_environment_case_id ON environment (case_id);
CREATE INDEX IF NOT EXISTS idx_environment_type ON environment (env_type);
CREATE INDEX IF NOT EXISTS idx_environment_collected_at ON environment (collected_at DESC);
CREATE INDEX IF NOT EXISTS idx_environment_data_gin ON environment USING GIN (env_data);
CREATE INDEX IF NOT EXISTS idx_environment_trace_id ON environment (trace_id);

CREATE TABLE IF NOT EXISTS assistant_evaluation (
    evaluation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    conversation_id uuid,
    assistant_type varchar(50) NOT NULL,
    score smallint,
    feedback text,
    resolution_time_seconds integer,
    message_count integer,
    metadata jsonb DEFAULT '{}'::jsonb,
    close_reason varchar(20),
    session_duration_sec integer,
    repeat_question_count integer,
    composite_score smallint,
    score_breakdown jsonb,
    calculated_at timestamptz,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT assistant_evaluation_pkey PRIMARY KEY (evaluation_id)
);
CREATE INDEX IF NOT EXISTS idx_eval_case_id ON assistant_evaluation (case_id);
CREATE INDEX IF NOT EXISTS idx_eval_assistant_type ON assistant_evaluation (assistant_type);
CREATE INDEX IF NOT EXISTS idx_eval_score ON assistant_evaluation (score);
CREATE INDEX IF NOT EXISTS idx_eval_created_at ON assistant_evaluation (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_eval_trace_id ON assistant_evaluation (trace_id);
CREATE INDEX IF NOT EXISTS idx_eval_composite_score ON assistant_evaluation (composite_score) WHERE composite_score IS NOT NULL;

CREATE TABLE IF NOT EXISTS conversation (
    conversation_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    pod_id varchar(100),
    assistant_type varchar(50) NOT NULL DEFAULT 'openclaw',
    diagnostic_stage varchar(5) NOT NULL DEFAULT 'S0',
    category_id varchar(64),
    category_l1 varchar(100),
    category_l2 varchar(200),
    started_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    ended_at timestamptz,
    message_count integer DEFAULT 0,
    metadata jsonb DEFAULT '{}'::jsonb,
    pending_confirm jsonb,
    repeat_question_count integer NOT NULL DEFAULT 0,
    trace_id varchar(64),
    CONSTRAINT fk_conversation_case_id FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    CONSTRAINT conversation_pkey PRIMARY KEY (conversation_id)
);
CREATE INDEX IF NOT EXISTS idx_conversation_case_id ON conversation (case_id);
CREATE INDEX IF NOT EXISTS idx_conversation_pod_id ON conversation (pod_id);
CREATE INDEX IF NOT EXISTS idx_conversation_assistant_type ON conversation (assistant_type);
CREATE INDEX IF NOT EXISTS idx_conversation_started_at ON conversation (started_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_trace_id ON conversation (trace_id);
CREATE INDEX IF NOT EXISTS idx_conversation_case_started ON conversation (case_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_conversation_diagnostic_stage ON conversation (diagnostic_stage) WHERE diagnostic_stage IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversation_category_id ON conversation (category_id) WHERE category_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_conversation_repeat_question ON conversation (repeat_question_count) WHERE repeat_question_count > 0;

CREATE TABLE IF NOT EXISTS message (
    message_id uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    case_id varchar(20) NOT NULL,
    "role" message_role NOT NULL,
    content text NOT NULL,
    command text,
    command_warning text,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT fk_message_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    CONSTRAINT message_pkey PRIMARY KEY (message_id)
);
CREATE INDEX IF NOT EXISTS idx_message_conversation_id ON message (conversation_id);
CREATE INDEX IF NOT EXISTS idx_message_case_id ON message (case_id);
CREATE INDEX IF NOT EXISTS idx_message_created_at ON message (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_role ON message ("role");
CREATE INDEX IF NOT EXISTS idx_message_trace_id ON message (trace_id);
CREATE INDEX IF NOT EXISTS idx_message_case_created ON message (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_message_content_search ON message USING GIN (to_tsvector('english', content));

DROP TRIGGER IF EXISTS update_conversation_message_count ON message;
CREATE TRIGGER update_conversation_message_count
    AFTER INSERT OR DELETE ON message
    FOR EACH ROW EXECUTE FUNCTION fn_update_conversation_message_count();

CREATE TABLE IF NOT EXISTS diagnostic_item (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    stage varchar(5) NOT NULL,
    "type" varchar(30) NOT NULL,
    seq smallint,
    content jsonb NOT NULL DEFAULT '{}'::jsonb,
    probability real,
    status varchar(20) NOT NULL DEFAULT 'pending',
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trace_id varchar(64),
    CONSTRAINT fk_diagnostic_item_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    CONSTRAINT diagnostic_item_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_conv_type ON diagnostic_item (conversation_id, "type", seq);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_conv_stage ON diagnostic_item (conversation_id, stage);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_status ON diagnostic_item ("type", status);
DROP TRIGGER IF EXISTS update_diagnostic_item_updated_at ON diagnostic_item;
CREATE TRIGGER update_diagnostic_item_updated_at
    BEFORE UPDATE ON diagnostic_item
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

CREATE TABLE IF NOT EXISTS system_prompt (
    id serial NOT NULL,
    stage varchar(5) NOT NULL,
    name varchar(100) NOT NULL,
    description text,
    content_template text NOT NULL,
    version varchar(20) NOT NULL DEFAULT '1.0',
    is_active boolean DEFAULT true,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT system_prompt_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_system_prompt_stage_active ON system_prompt (stage, is_active);
CREATE INDEX IF NOT EXISTS idx_system_prompt_stage_version ON system_prompt (stage, version);

CREATE TABLE IF NOT EXISTS tool_definition (
    id serial NOT NULL,
    tool_name varchar(100) NOT NULL UNIQUE,
    display_name varchar(200),
    tool_type varchar(20) NOT NULL,
    category varchar(50),
    description text NOT NULL,
    usage_template text,
    parameters_schema jsonb,
    examples jsonb,
    risk_level smallint NOT NULL DEFAULT 1,
    is_active boolean DEFAULT true,
    version varchar(20) DEFAULT '1.0',
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT tool_definition_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_tool_definition_type_active ON tool_definition (tool_type, is_active);
CREATE INDEX IF NOT EXISTS idx_tool_definition_category_risk ON tool_definition (category, risk_level);
CREATE INDEX IF NOT EXISTS idx_tool_definition_risk_level ON tool_definition (risk_level);

CREATE TABLE IF NOT EXISTS tool_result (
    id varchar(36) NOT NULL,
    conversation_id uuid NOT NULL,
    tool_name varchar(100) NOT NULL,
    tool_type varchar(20),
    step_no smallint,
    risk_level smallint,
    policy varchar(20),
    authorized_by varchar(100),
    input_json jsonb DEFAULT '{}'::jsonb,
    output_json jsonb,
    error text,
    duration_ms integer,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT fk_tool_result_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    CONSTRAINT tool_result_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_tool_result_conversation ON tool_result (conversation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_result_tool_name ON tool_result (tool_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_result_risk_level ON tool_result (risk_level) WHERE risk_level >= 2;
CREATE INDEX IF NOT EXISTS idx_tool_result_trace_id ON tool_result (trace_id) WHERE trace_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS audit_log (
    id varchar(36) NOT NULL,
    audit_type varchar(20) NOT NULL,
    conversation_id uuid NOT NULL,
    turn_index smallint,
    system_prompt_id integer,
    payload jsonb NOT NULL DEFAULT '{}'::jsonb,
    error text,
    duration_ms integer,
    started_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT fk_audit_log_conversation_id FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    CONSTRAINT fk_audit_log_system_prompt_id FOREIGN KEY (system_prompt_id) REFERENCES system_prompt (id) ON DELETE SET NULL,
    CONSTRAINT audit_log_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_audit_log_conversation ON audit_log (conversation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_type ON audit_log (audit_type, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_trace_id ON audit_log (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_log_system_prompt_id ON audit_log (system_prompt_id) WHERE system_prompt_id IS NOT NULL;

CREATE TABLE IF NOT EXISTS session (
    session_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    user_id uuid NOT NULL,
    metadata jsonb DEFAULT '{}'::jsonb,
    created_at timestamptz DEFAULT CURRENT_TIMESTAMP,
    expires_at timestamptz,
    trace_id varchar(64),
    CONSTRAINT fk_session_case_id FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    CONSTRAINT session_pkey PRIMARY KEY (session_id)
);
CREATE INDEX IF NOT EXISTS idx_session_case_id ON session (case_id);
CREATE INDEX IF NOT EXISTS idx_session_user_id ON session (user_id);

CREATE TABLE IF NOT EXISTS kbd_entry (
    id bigserial NOT NULL,
    support_id varchar(20) NOT NULL UNIQUE,
    support_url text,
    title text NOT NULL,
    content_md text,
    metadata jsonb NOT NULL DEFAULT '{}'::jsonb,
    category_id varchar(32),
    ai_category_id varchar(32),
    ai_category_conf double precision,
    ai_category_reason text,
    embedding vector(1536),
    tsv tsvector,
    status varchar(20) NOT NULL DEFAULT 'draft',
    reviewer_id integer,
    reviewed_at timestamptz,
    review_note text,
    published_at timestamptz,
    archived_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_kbd_entry_category_id FOREIGN KEY (category_id) REFERENCES kb_category (code) ON DELETE NO ACTION,
    CONSTRAINT kbd_entry_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_kbd_entry_status ON kbd_entry (status);
CREATE INDEX IF NOT EXISTS idx_kbd_entry_category ON kbd_entry (category_id) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_kbd_entry_ai_category ON kbd_entry (ai_category_id);
CREATE INDEX IF NOT EXISTS idx_kbd_entry_published ON kbd_entry (published_at DESC) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_kbd_entry_tsv ON kbd_entry USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kbd_entry_metadata ON kbd_entry USING GIN (metadata);

CREATE TABLE IF NOT EXISTS sop_document (
    id serial NOT NULL,
    source_id varchar(100) UNIQUE,
    category_id varchar(32),
    title varchar(500),
    content_md text,
    docx_hash varchar(64),
    status varchar(20) DEFAULT 'draft',
    reviewer_id integer,
    reviewed_at timestamptz,
    review_note text,
    published_at timestamptz,
    created_at timestamptz DEFAULT now(),
    updated_at timestamptz DEFAULT now(),
    CONSTRAINT fk_sop_document_category_id FOREIGN KEY (category_id) REFERENCES kb_category (code) ON DELETE NO ACTION,
    CONSTRAINT sop_document_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_sop_document_category ON sop_document (category_id) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_sop_document_status ON sop_document (status);

CREATE TABLE IF NOT EXISTS sop_chunk (
    id serial NOT NULL,
    document_id integer NOT NULL,
    chunk_index smallint NOT NULL,
    chapter_title varchar(200),
    content text,
    embedding vector(1536),
    tsv tsvector,
    created_at timestamptz DEFAULT now(),
    CONSTRAINT fk_sop_chunk_document_id FOREIGN KEY (document_id) REFERENCES sop_document (id) ON DELETE CASCADE,
    CONSTRAINT sop_chunk_pkey PRIMARY KEY (id)
);
CREATE INDEX IF NOT EXISTS idx_sop_chunk_document ON sop_chunk (document_id);
CREATE INDEX IF NOT EXISTS idx_sop_chunk_tsv ON sop_chunk USING GIN (tsv);

-- ============================================================
-- 延迟添加的外键约束（打破循环依赖）
-- ============================================================
ALTER TABLE assistant_evaluation
    ADD CONSTRAINT IF NOT EXISTS fk_eval_case_id
    FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE;

ALTER TABLE assistant_evaluation
    ADD CONSTRAINT IF NOT EXISTS fk_eval_conversation_id
    FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE SET NULL;
