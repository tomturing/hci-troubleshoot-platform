-- migrate:up

-- HCI Troubleshoot Platform - Database Initialization Script
-- Version: 3.0 (KB RAG + LearningClaw/ProductionClaw)
-- Date: 2026-03-05
-- Database: PostgreSQL 15

-- ============================================================================
-- 1. 创建扩展
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";  -- 用于文本相似度搜索
CREATE EXTENSION IF NOT EXISTS "vector";   -- pgvector 向量存储扩展

SET timezone = 'UTC';

-- ============================================================================
-- 2. 创建枚举类型
-- ============================================================================

CREATE TYPE case_status AS ENUM (
    'created',      -- 已创建
    'confirmed',    -- 已确认
    'in_progress',  -- 进行中
    'resolved',     -- 已解决
    'closed',       -- 已关闭
    'cancelled'     -- 已取消
);

CREATE TYPE message_role AS ENUM (
    'user',        -- 用户消息
    'assistant',   -- AI助手消息
    'system',      -- 系统消息
    'command'      -- 命令建议
);

-- ============================================================================
-- 3. 创建触发器函数
-- ============================================================================

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION generate_case_id()
RETURNS VARCHAR AS $$
DECLARE
    date_part VARCHAR(8);
    seq_part VARCHAR(5);
    next_seq INT;
BEGIN
    date_part := TO_CHAR(CURRENT_DATE, 'YYYYMMDD');
    
    SELECT COALESCE(MAX(SUBSTRING(case_id FROM 10 FOR 5)::INT), 0) + 1
    INTO next_seq
    FROM "case"
    WHERE case_id LIKE 'Q' || date_part || '%';
    
    seq_part := LPAD(next_seq::TEXT, 5, '0');
    
    RETURN 'Q' || date_part || seq_part;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- 4. 创建表
-- ============================================================================

-- 用户表
CREATE TABLE IF NOT EXISTS "user" (
    user_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id VARCHAR(255) UNIQUE NOT NULL,
    username VARCHAR(100),
    email VARCHAR(255),
    user_type VARCHAR(20) NOT NULL DEFAULT 'temporary',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_login_at TIMESTAMP WITH TIME ZONE,
    trace_id VARCHAR(64)
);

CREATE INDEX idx_user_client_id ON "user"(client_id);
CREATE INDEX idx_user_trace_id ON "user"(trace_id);
CREATE INDEX idx_user_type ON "user"(user_type);

CREATE TRIGGER update_user_updated_at BEFORE UPDATE ON "user"
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE "user" IS '用户表，存储临时用户和认证用户信息';
COMMENT ON COLUMN "user".client_id IS '客户端生成的唯一标识';

-- 工单表
CREATE TABLE IF NOT EXISTS "case" (
    case_id VARCHAR(20) PRIMARY KEY,
    user_id UUID NOT NULL REFERENCES "user"(user_id) ON DELETE CASCADE,
    client_id VARCHAR(255) NOT NULL,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    status case_status NOT NULL DEFAULT 'created',
    priority VARCHAR(20) DEFAULT 'medium',
    category VARCHAR(100),
    assistant_type VARCHAR(50) NOT NULL DEFAULT 'openclaw',
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    confirmed_at TIMESTAMP WITH TIME ZONE,
    resolved_at TIMESTAMP WITH TIME ZONE,
    closed_at TIMESTAMP WITH TIME ZONE,
    close_reason VARCHAR(20) CHECK (close_reason IN ('user_command', 'timeout', 'abandon', 'admin_close')),  -- 工单关闭原因（被动信号轨道）
    trace_id VARCHAR(64)
);

CREATE INDEX idx_case_user_id ON "case"(user_id);
CREATE INDEX idx_case_client_id ON "case"(client_id);
CREATE INDEX idx_case_status ON "case"(status);
CREATE INDEX idx_case_created_at ON "case"(created_at DESC);
CREATE INDEX idx_case_trace_id ON "case"(trace_id);
CREATE INDEX idx_case_category ON "case"(category);
CREATE INDEX idx_case_client_status ON "case"(client_id, status);
CREATE INDEX idx_case_assistant_type ON "case"(assistant_type);

CREATE TRIGGER update_case_updated_at BEFORE UPDATE ON "case"
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE "case" IS '工单表，记录排障请求';

-- 对话会话表
CREATE TABLE IF NOT EXISTS conversation (
    conversation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(20) NOT NULL REFERENCES "case"(case_id) ON DELETE CASCADE,
    pod_id VARCHAR(100),
    assistant_type VARCHAR(50) NOT NULL DEFAULT 'openclaw',
    started_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    ended_at TIMESTAMP WITH TIME ZONE,
    message_count INT DEFAULT 0,
    repeat_question_count INT DEFAULT 0 NOT NULL,  -- 重复提问计数（质量指标）
    metadata JSONB DEFAULT '{}'::jsonb,
    -- 诊断状态字段（0003 迁移）
    diagnostic_stage VARCHAR(8) NOT NULL DEFAULT 'S0',  -- 诊断阶段：S0意图识别→S6验证闭环
    category_l1 VARCHAR(100),                          -- 一级分类（如：虚拟机/存储/网络）
    category_l2 VARCHAR(100),                          -- 二级分类（如：虚拟机开机失败）
    category_id VARCHAR(32),                           -- 分类 ID，对应 category_baseline.yaml
    hypothesis JSONB DEFAULT '[]'::jsonb,              -- 当前假设列表 [{id,description,confidence,status}]
    react_state JSONB DEFAULT '{}'::jsonb,             -- ReAct 执行器状态快照（断点续接）
    pending_confirm JSONB,                             -- 待用户确认的工具调用 {tool_call_id,tool_name,args,risk_level}
    trace_id VARCHAR(64)
);

CREATE INDEX idx_conversation_case_id ON conversation(case_id);
CREATE INDEX idx_conversation_pod_id ON conversation(pod_id);
CREATE INDEX idx_conversation_assistant_type ON conversation(assistant_type);
CREATE INDEX idx_conversation_started_at ON conversation(started_at DESC);
CREATE INDEX idx_conversation_trace_id ON conversation(trace_id);
CREATE INDEX idx_conversation_case_started ON conversation(case_id, started_at DESC);
CREATE INDEX idx_conversation_diagnostic_stage ON conversation(diagnostic_stage);

COMMENT ON TABLE conversation IS '对话会话表，一个Case可以有多个会话';

-- 消息表
CREATE TABLE IF NOT EXISTS message (
    message_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    case_id VARCHAR(20) NOT NULL,
    role message_role NOT NULL,
    content TEXT NOT NULL,
    command TEXT,
    command_warning TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    trace_id VARCHAR(64),
    
    CONSTRAINT check_command_content CHECK (
        (role = 'command' AND command IS NOT NULL) OR
        (role != 'command')
    )
);

CREATE INDEX idx_message_conversation_id ON message(conversation_id);
CREATE INDEX idx_message_case_id ON message(case_id);
CREATE INDEX idx_message_role ON message(role);
CREATE INDEX idx_message_created_at ON message(created_at DESC);
CREATE INDEX idx_message_trace_id ON message(trace_id);
CREATE INDEX idx_message_case_created ON message(case_id, created_at DESC);
CREATE INDEX idx_message_content_search ON message USING gin(to_tsvector('english', content));

-- 消息计数触发器
CREATE OR REPLACE FUNCTION update_conversation_message_count()
RETURNS TRIGGER AS $$
BEGIN
    IF TG_OP = 'INSERT' THEN
        UPDATE conversation 
        SET message_count = message_count + 1
        WHERE conversation_id = NEW.conversation_id;
    ELSIF TG_OP = 'DELETE' THEN
        UPDATE conversation 
        SET message_count = message_count - 1
        WHERE conversation_id = OLD.conversation_id;
    END IF;
    RETURN NULL;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_message_count_on_insert
    AFTER INSERT ON message
    FOR EACH ROW EXECUTE FUNCTION update_conversation_message_count();

CREATE TRIGGER update_message_count_on_delete
    AFTER DELETE ON message
    FOR EACH ROW EXECUTE FUNCTION update_conversation_message_count();

COMMENT ON TABLE message IS '消息表，存储所有对话消息';

-- 环境信息表
CREATE TABLE IF NOT EXISTS environment (
    environment_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(20) NOT NULL REFERENCES "case"(case_id) ON DELETE CASCADE,
    env_type VARCHAR(50) NOT NULL,
    env_data JSONB NOT NULL,
    collected_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    trace_id VARCHAR(64)
);

CREATE INDEX idx_environment_case_id ON environment(case_id);
CREATE INDEX idx_environment_type ON environment(env_type);
CREATE INDEX idx_environment_collected_at ON environment(collected_at DESC);
CREATE INDEX idx_environment_trace_id ON environment(trace_id);
CREATE INDEX idx_environment_data_gin ON environment USING gin(env_data);

COMMENT ON TABLE environment IS '环境信息表，存储客户现场环境数据';

-- AI助手评估表 (v2.0)
CREATE TABLE IF NOT EXISTS assistant_evaluation (
    evaluation_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    case_id VARCHAR(20) NOT NULL REFERENCES "case"(case_id) ON DELETE CASCADE,
    conversation_id UUID REFERENCES conversation(conversation_id) ON DELETE SET NULL,
    assistant_type VARCHAR(50) NOT NULL,
    score SMALLINT CHECK (score >= 1 AND score <= 5),
    feedback TEXT,
    resolution_time_seconds INT,
    message_count INT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    trace_id VARCHAR(64),
    -- 评分评价体系（v1 迁移）
    close_reason         VARCHAR(20),                  -- 工单关闭原因（冗余存储，避免跨表 JOIN）
    session_duration_sec INTEGER,                      -- 会话时长（秒），用于解决效率维度计算
    repeat_question_count INTEGER,                     -- 用户重复提问次数
    composite_score      SMALLINT,                     -- 综合质量分 0-100，由 QualityScoreService 计算
    score_breakdown      JSONB,                        -- 各维度分解：{close_intent, efficiency, user_rating, ai_quality}
    calculated_at        TIMESTAMPTZ                   -- 综合质量分计算时间
);

CREATE INDEX idx_eval_case_id ON assistant_evaluation(case_id);
CREATE INDEX idx_eval_assistant_type ON assistant_evaluation(assistant_type);
CREATE INDEX idx_eval_score ON assistant_evaluation(score);
CREATE INDEX idx_eval_created_at ON assistant_evaluation(created_at DESC);
CREATE INDEX idx_eval_trace_id ON assistant_evaluation(trace_id);
CREATE INDEX IF NOT EXISTS idx_eval_composite_score ON assistant_evaluation(composite_score) WHERE composite_score IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_eval_close_reason    ON assistant_evaluation(close_reason) WHERE close_reason IS NOT NULL;

COMMENT ON TABLE assistant_evaluation IS 'AI助手评估表，记录助手表现评分';
COMMENT ON COLUMN assistant_evaluation.composite_score IS '综合质量分 0-100，由 QualityScoreService 计算（双轨制：无用户评分时自动降级为三维模型）';
COMMENT ON COLUMN assistant_evaluation.score_breakdown IS '各维度分解：{"close_intent": 90, "efficiency": 70, "user_rating": 80, "ai_quality": 65}';

-- ============================================================================
-- 5. 创建视图
-- ============================================================================

CREATE OR REPLACE VIEW active_cases AS
SELECT 
    c.case_id,
    c.client_id,
    c.title,
    c.status,
    c.priority,
    c.category,
    c.created_at,
    u.username,
    u.email,
    COUNT(DISTINCT conv.conversation_id) as conversation_count,
    COUNT(m.message_id) as total_messages,
    MAX(m.created_at) as last_message_at
FROM "case" c
LEFT JOIN "user" u ON c.user_id = u.user_id
LEFT JOIN conversation conv ON c.case_id = conv.case_id
LEFT JOIN message m ON conv.conversation_id = m.conversation_id
WHERE c.status IN ('created', 'confirmed', 'in_progress')
GROUP BY c.case_id, c.client_id, c.title, c.status, c.priority, c.category, c.created_at, u.username, u.email
ORDER BY c.created_at DESC;

CREATE OR REPLACE VIEW case_statistics AS
SELECT 
    c.case_id,
    c.title,
    c.status,
    c.created_at,
    c.closed_at,
    EXTRACT(EPOCH FROM (COALESCE(c.closed_at, CURRENT_TIMESTAMP) - c.created_at)) / 3600 as duration_hours,
    COUNT(DISTINCT conv.conversation_id) as conversation_count,
    COUNT(m.message_id) as message_count,
    COUNT(CASE WHEN m.role = 'command' THEN 1 END) as command_count,
    JSONB_BUILD_OBJECT(
        'user_messages', COUNT(CASE WHEN m.role = 'user' THEN 1 END),
        'assistant_messages', COUNT(CASE WHEN m.role = 'assistant' THEN 1 END),
        'system_messages', COUNT(CASE WHEN m.role = 'system' THEN 1 END)
    ) as message_breakdown
FROM "case" c
LEFT JOIN conversation conv ON c.case_id = conv.case_id
LEFT JOIN message m ON conv.conversation_id = m.conversation_id
GROUP BY c.case_id, c.title, c.status, c.created_at, c.closed_at;

-- ============================================================================
-- 6. 插入种子数据 (可选)
-- ============================================================================

-- 插入测试用户
INSERT INTO "user" (client_id, username, user_type, trace_id)
VALUES ('client-test-001', 'test_user', 'temporary', 'hci-init-seed-001')
ON CONFLICT (client_id) DO NOTHING;

-- ============================================================================
-- 7. 授权
-- ============================================================================

-- 如果有特定的应用用户，在这里授权
-- GRANT ALL PRIVILEGES ON ALL TABLES IN SCHEMA public TO hci_app_user;
-- GRANT ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA public TO hci_app_user;

-- ============================================================================
-- 8. 知识库表 (v3.0 RAG — LearningClaw/ProductionClaw)
-- ============================================================================
-- 注意：v2.0 → v3.0 Breaking Change
--   旧 kb_document 主键为 doc_id UUID，新版改为 id SERIAL（整型，性能更优）
--   旧 kb_chunk 主键为 chunk_id UUID，新版改为 id SERIAL
--   如已存在旧表，执行 database/migrate_kb_v3.sql 进行迁移

-- 知识库文档表（v3.0）
CREATE TABLE IF NOT EXISTS kb_document (
    id              SERIAL PRIMARY KEY,
    source_id       VARCHAR(50) UNIQUE,                -- 原始案例ID（采集自 support system）
    title           VARCHAR(500) NOT NULL,
    product         VARCHAR(100) DEFAULT '超融合HCI',
    content_md      TEXT NOT NULL,                     -- MD 全文（Source of Truth）
    content_hash    VARCHAR(64),                       -- SHA256，用于变更检测，避免重复入库
    yaml_meta       JSONB,                             -- 结构化元数据（LLM 增强后的字段集合）
    category_l1     VARCHAR(100),                      -- 一级分类（如：虚拟机 / 存储 / 网络）
    category_l2     VARCHAR(100),                      -- 二级分类（如：开关机 / 迁移 / 快照）
    tags            TEXT[],                            -- 标签数组（如：['CPU', 'KVM', 'BIOS']）
    judgment_logic  TEXT,                              -- 排查逻辑（中文，LLM 生成）
    summary         TEXT,                              -- 摘要（中文，LLM 生成）
    difficulty      SMALLINT DEFAULT 3,                -- 难度 1-5，3 为默认
    status          VARCHAR(20) DEFAULT 'draft',       -- 状态机：draft/under_review/approved/published/rejected/archived
    review_note     TEXT,                              -- 审核批注
    reviewer        VARCHAR(100),                      -- 审核人
    reviewed_at     TIMESTAMP WITH TIME ZONE,
    source_type     VARCHAR(20) DEFAULT 'kb',          -- 来源：kb（历史案例）/ sop / realtime（在网工单）
    has_images      BOOLEAN DEFAULT FALSE,             -- 是否含图片（需 OCR 处理）
    verified_version VARCHAR(50),                     -- 已验证的产品版本
    trace_id        VARCHAR(64),                       -- W3C traceparent 调用链 ID
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_document_status     ON kb_document(status);
CREATE INDEX IF NOT EXISTS idx_kb_document_category   ON kb_document(category_l1, category_l2);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_id  ON kb_document(source_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_type ON kb_document(source_type);
CREATE INDEX IF NOT EXISTS idx_kb_document_trace_id   ON kb_document(trace_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_created_at ON kb_document(created_at DESC);

CREATE TRIGGER update_kb_document_updated_at BEFORE UPDATE ON kb_document
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

COMMENT ON TABLE kb_document IS '知识库文档表 v3.0 — 状态机驱动的文档生命周期管理';
COMMENT ON COLUMN kb_document.status IS '状态机: draft→under_review→approved→published，也可→rejected/archived';
COMMENT ON COLUMN kb_document.source_type IS 'kb=历史案例批量导入, sop=SOP手册, realtime=在网工单实时生成';
COMMENT ON COLUMN kb_document.content_hash IS 'SHA256(content_md)，增量更新时用于跳过未变更文档';

-- 知识库分块 + 向量表（v3.0）
-- 注意：IVFFlat 索引需在数据量 ≥ 1000 条后手动创建（空表无法创建 IVFFlat）
CREATE TABLE IF NOT EXISTS kb_chunk (
    id              SERIAL PRIMARY KEY,
    document_id     INTEGER NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    chunk_index     SMALLINT NOT NULL,                 -- 块在文档中的顺序位置（0-based）
    content         TEXT NOT NULL,                     -- 块文本（~512 tokens）
    embedding       vector(384),                       -- 向量（主力 z.ai API / 降级 bge-small-zh，384维）
    token_count     SMALLINT,                          -- 块的 token 数量
    metadata        JSONB,                             -- 块级元数据（标题层级、页码等）
    tsv             tsvector,                          -- BM25 全文索引（jieba 分词后的 tsvector）
    trace_id        VARCHAR(64),                       -- 入库时的调用链 ID
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_document    ON kb_chunk(document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_position    ON kb_chunk(document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_tsv         ON kb_chunk USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_trace_id    ON kb_chunk(trace_id);

-- 向量检索索引（在有一定数据量后手动执行，空表无法创建 IVFFlat）：
-- CREATE INDEX idx_kb_chunk_embedding ON kb_chunk USING ivfflat (embedding vector_cosine_ops) WITH (lists = 100);

COMMENT ON TABLE kb_chunk IS '知识库分块 + 向量表 v3.0 — 双路检索（BM25 tsvector + pgvector IVFFlat）';
COMMENT ON COLUMN kb_chunk.tsv IS 'BM25 全文检索索引，由 jieba 分词后写入（用 simple 配置，避免中文 parser 依赖）';
COMMENT ON COLUMN kb_chunk.embedding IS '384 维向量，主力使用 z.ai embedding API，降级使用本地 bge-small-zh-v1.5';

-- 知识分类树（v3.0）
-- 4 层树形结构（L1-L4），L1 有 6 个主类别
CREATE TABLE IF NOT EXISTS kb_category (
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES kb_category(id),  -- NULL 表示 L1 根节点
    name            VARCHAR(100) NOT NULL,
    level           SMALLINT NOT NULL,                 -- 1=L1, 2=L2, 3=L3, 4=L4
    keywords        TEXT[],                            -- 该类别的触发关键字
    source          VARCHAR(20) DEFAULT 'manual',      -- manual（人工）/ auto_generated / auto_suggested
    version         VARCHAR(20) DEFAULT '1.0',
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_category_parent   ON kb_category(parent_id);
CREATE INDEX IF NOT EXISTS idx_kb_category_level    ON kb_category(level);
CREATE INDEX IF NOT EXISTS idx_kb_category_keywords ON kb_category USING GIN (keywords);

COMMENT ON TABLE kb_category IS '知识分类树 v3.0 — 4 层树形结构（L1:平台/网络/存储/硬件/客户机硬件/虚拟机）';
COMMENT ON COLUMN kb_category.source IS 'manual=人工维护, auto_generated=系统生成, auto_suggested=LLM建议待审核';

-- ============================================================================
-- 9. 原始工单数据表（P4 数据管道迁移）
-- ============================================================================
CREATE TABLE IF NOT EXISTS raw_cases (
    id             BIGSERIAL    PRIMARY KEY,
    case_id        VARCHAR(64)  NOT NULL UNIQUE,        -- 来源系统的工单 ID（幂等键）
    source_url     TEXT         NOT NULL DEFAULT '',    -- 工单在来源系统的 URL
    content_text   TEXT         NOT NULL DEFAULT '',    -- 已脱敏的工单正文（Markdown 格式）
    images         JSONB        NOT NULL DEFAULT '[]',  -- 图片/附件 URL 列表，格式：[{"url":"...","type":"..."}]
    classification VARCHAR(128) NOT NULL DEFAULT '',    -- 故障分类（如 vm_power_failure）
    quality_score  SMALLINT     NOT NULL DEFAULT 0,     -- 质量评分 0-100，低于 20 分的不入库
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  raw_cases               IS 'HCI 历史工单原始数据（已脱敏），供知识库提炼使用';
COMMENT ON COLUMN raw_cases.quality_score IS '工单质量评分 0-100，低于 20 分的不入库';
COMMENT ON COLUMN raw_cases.images        IS 'JSON 数组，每项 {"url": "...", "type": "error_screenshot|command_output|..."}';

CREATE INDEX IF NOT EXISTS idx_raw_cases_classification ON raw_cases(classification);
CREATE INDEX IF NOT EXISTS idx_raw_cases_quality        ON raw_cases(quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_raw_cases_created_at     ON raw_cases(created_at DESC);

CREATE OR REPLACE FUNCTION update_raw_cases_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_raw_cases_updated_at ON raw_cases;
CREATE TRIGGER trg_raw_cases_updated_at
BEFORE UPDATE ON raw_cases
FOR EACH ROW EXECUTE FUNCTION update_raw_cases_updated_at();

-- ============================================================================
-- 11. 知识原子候选表（P4 知识反馈闭环迁移）
-- ============================================================================
CREATE TABLE IF NOT EXISTS knowledge_atoms (
    id             VARCHAR(32)  PRIMARY KEY,            -- 格式：ka-{12位hex}
    atom_type      VARCHAR(32)  NOT NULL,               -- diagnostic_step|fix_action|decision_gate|threshold|error_code_ref|background
    category_id    VARCHAR(64)  NOT NULL DEFAULT '',    -- 关联的故障分类 ID
    trigger_json   JSONB        NOT NULL DEFAULT '{}',  -- 触发条件 {stage, conditions, error_code_patterns, ...}
    content_json   JSONB        NOT NULL DEFAULT '{}',  -- 内容 {full_text, commands, ...}
    source_type    VARCHAR(16)  NOT NULL DEFAULT 'session',  -- session|manual
    source_ref     VARCHAR(64)  NOT NULL DEFAULT '',    -- 来源 session_id 或操作者
    verified       BOOLEAN      NOT NULL DEFAULT FALSE, -- 人工审核状态
    confidence     NUMERIC(3,2) NOT NULL DEFAULT 0.70,  -- 置信度 0.00-1.00
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    verified_at    TIMESTAMPTZ,                         -- 审核通过时间
    verified_by    VARCHAR(64)                          -- 审核者 ID
);

COMMENT ON TABLE  knowledge_atoms           IS 'AI 自动提炼的知识原子候选，verified=false 时待人工审核';
COMMENT ON COLUMN knowledge_atoms.atom_type IS 'diagnostic_step/fix_action/decision_gate/threshold/error_code_ref/background';
COMMENT ON COLUMN knowledge_atoms.verified  IS '人工审核通过后设为 true，被知识检索系统使用';
COMMENT ON COLUMN knowledge_atoms.confidence IS '机器生成默认 0.70，人工修正后可调高';

CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_verified  ON knowledge_atoms(verified, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_category  ON knowledge_atoms(category_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_source    ON knowledge_atoms(source_ref);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_atom_type ON knowledge_atoms(atom_type);

CREATE OR REPLACE FUNCTION update_knowledge_atoms_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_knowledge_atoms_updated_at ON knowledge_atoms;
CREATE TRIGGER trg_knowledge_atoms_updated_at
BEFORE UPDATE ON knowledge_atoms
FOR EACH ROW EXECUTE FUNCTION update_knowledge_atoms_updated_at();

-- ============================================================================
-- 12. AI 行为统一审计日志表（合并自 prompt_audit + tool_audit_log）
-- ============================================================================
-- audit_type='prompt'    → 记录 LLM 调用上下文（原 prompt_audit）
-- audit_type='tool_call' → 记录 ReAct 工具执行（原 tool_audit_log）
-- audit_type='system'    → 系统级事件

CREATE TABLE IF NOT EXISTS audit_log (
    id              VARCHAR(36)  PRIMARY KEY,                          -- UUID
    audit_type      VARCHAR(20)  NOT NULL,                             -- 'prompt' | 'tool_call' | 'system'
    conversation_id UUID         NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    turn_index      SMALLINT,                                          -- prompt 类型：第几轮对话
    tool_name       VARCHAR(100),                                      -- tool_call 类型：工具名称
    risk_level      SMALLINT,                                          -- tool_call 类型：1=只读 2=写 3=高危
    policy          VARCHAR(20),                                       -- tool_call 类型：auto|notify|confirm|block
    authorized_by   VARCHAR(100),                                      -- tool_call 类型：高危操作确认用户
    payload         JSONB        NOT NULL DEFAULT '{}',                -- 类型专属字段（prompt 上下文 / 工具结果）
    error           TEXT,                                              -- 错误信息（失败时）
    duration_ms     INTEGER,                                           -- 执行耗时（毫秒）
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    trace_id        VARCHAR(64)                                        -- W3C traceparent
);

CREATE INDEX IF NOT EXISTS idx_audit_log_conversation ON audit_log(conversation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_type         ON audit_log(audit_type, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_tool_name    ON audit_log(tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_log_risk_level   ON audit_log(risk_level) WHERE risk_level >= 2;
CREATE INDEX IF NOT EXISTS idx_audit_log_trace_id     ON audit_log(trace_id) WHERE trace_id IS NOT NULL;

COMMENT ON TABLE audit_log IS 'AI 行为统一审计日志（合并自 prompt_audit + tool_audit_log）';
COMMENT ON COLUMN audit_log.audit_type IS 'prompt=LLM 调用审计, tool_call=工具执行审计, system=系统事件';
COMMENT ON COLUMN audit_log.risk_level IS '1=只读/低风险, 2=写操作需人工确认, 3=高危禁止自动执行';
COMMENT ON COLUMN audit_log.payload IS 'prompt 类型：{has_sop,kb_chunks_count,kb_top_score,context_breakdown,payload_ref,...}；tool_call 类型：{tool_args,result_summary,result_code}';

-- ============================================================================
-- 完成
-- ============================================================================

SELECT 'Database initialization completed successfully! (v3.1)' as status;


-- migrate:down
-- 不提供自动降级，手动回滚
