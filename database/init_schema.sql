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
    metadata JSONB DEFAULT '{}'::jsonb,
    trace_id VARCHAR(64)
);

CREATE INDEX idx_conversation_case_id ON conversation(case_id);
CREATE INDEX idx_conversation_pod_id ON conversation(pod_id);
CREATE INDEX idx_conversation_assistant_type ON conversation(assistant_type);
CREATE INDEX idx_conversation_started_at ON conversation(started_at DESC);
CREATE INDEX idx_conversation_trace_id ON conversation(trace_id);
CREATE INDEX idx_conversation_case_started ON conversation(case_id, started_at DESC);

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

-- 会话表
CREATE TABLE IF NOT EXISTS session (
    session_id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    client_id VARCHAR(255) NOT NULL,
    user_id UUID REFERENCES "user"(user_id) ON DELETE SET NULL,
    case_id VARCHAR(20) REFERENCES "case"(case_id) ON DELETE SET NULL,
    websocket_id VARCHAR(100),
    status VARCHAR(20) NOT NULL DEFAULT 'active',
    ip_address INET,
    user_agent TEXT,
    metadata JSONB DEFAULT '{}'::jsonb,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    last_activity_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    expires_at TIMESTAMP WITH TIME ZONE,
    trace_id VARCHAR(64)
);

CREATE INDEX idx_session_client_id ON session(client_id);
CREATE INDEX idx_session_user_id ON session(user_id);
CREATE INDEX idx_session_case_id ON session(case_id);
CREATE INDEX idx_session_status ON session(status);
CREATE INDEX idx_session_expires_at ON session(expires_at);
CREATE INDEX idx_session_trace_id ON session(trace_id);

COMMENT ON TABLE session IS '会话表，记录WebSocket连接信息';

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
    trace_id VARCHAR(64)
);

CREATE INDEX idx_eval_case_id ON assistant_evaluation(case_id);
CREATE INDEX idx_eval_assistant_type ON assistant_evaluation(assistant_type);
CREATE INDEX idx_eval_score ON assistant_evaluation(score);
CREATE INDEX idx_eval_created_at ON assistant_evaluation(created_at DESC);
CREATE INDEX idx_eval_trace_id ON assistant_evaluation(trace_id);

COMMENT ON TABLE assistant_evaluation IS 'AI助手评估表，记录助手表现评分';

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

-- SOP 决策树节点表（v3.0）
-- 将 SOP 排障手册拆分为"技能"（Skill）节点，支持关键字精确路由
CREATE TABLE IF NOT EXISTS kb_sop_node (
    id              SERIAL PRIMARY KEY,
    skill_id        VARCHAR(100) NOT NULL,             -- 技能 ID（如 vm_boot_failure, storage_iops_low）
    node_name       VARCHAR(200) NOT NULL,             -- 节点名称（如 CPU不足、KVM驱动缺失）
    parent_id       INTEGER REFERENCES kb_sop_node(id),  -- 父节点 ID（NULL 表示根节点）
    keywords        TEXT[] NOT NULL,                   -- 触发关键字列表（如 ['CPU不足','剩余CPU不足']）
    file_path       VARCHAR(500),                      -- 对应的 MD 文件路径（相对 sop_skills/）
    content         TEXT,                              -- 章节全文（检索命中后直接注入上下文）
    level           SMALLINT DEFAULT 1,                -- 层级（1=主章节, 2=子章节）
    sort_order      SMALLINT DEFAULT 0,                -- 同级排序
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_sop_node_skill    ON kb_sop_node(skill_id);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_keywords ON kb_sop_node USING GIN (keywords);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_parent   ON kb_sop_node(parent_id);

COMMENT ON TABLE kb_sop_node IS 'SOP 决策树节点表 — 关键字精确路由（优先于向量语义检索）';
COMMENT ON COLUMN kb_sop_node.skill_id IS '对应 sop_skills/ 目录下的子目录名';
COMMENT ON COLUMN kb_sop_node.keywords IS '触发该节点的关键字数组，来自 keywords_map.json';

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

-- 同义词映射表（v3.0）
-- 将缩写/别名统一化，提升全文检索召回率
CREATE TABLE IF NOT EXISTS kb_synonym (
    id              SERIAL PRIMARY KEY,
    term            VARCHAR(100) NOT NULL,             -- 缩写或别名（如 HCI, VM, vDisk）
    canonical       VARCHAR(100) NOT NULL,             -- 标准名称（如 超融合, 虚拟机, 虚拟磁盘）
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(term, canonical)
);

COMMENT ON TABLE kb_synonym IS '同义词映射表 v3.0 — 统一 HCI 专业术语缩写，提升 BM25 召回率';

-- ============================================================================
-- 完成
-- ============================================================================

SELECT 'Database initialization completed successfully! (v3.0)' as status;
