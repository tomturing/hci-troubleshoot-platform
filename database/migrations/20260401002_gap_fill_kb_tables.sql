-- migrate:up

-- =============================================================================
-- Gap-fill migration — 补建历史遗漏的知识库表
-- 创建日期：2026-04-01
--
-- 背景：
--   dev 环境原用 alembic 初始化数据库，00_baseline.sql 将 dbmate migrations
--   全部标记为"已执行"，但以下表从未实际创建：
--     - kb_document / kb_chunk / kb_category / audit_log (来自 20260305001)
--     - kb_sop_node / kb_synonym              (来自 20260312001)
--     - kbd_entry                              (来自 20260401001)
--
-- 本 migration 幂等地补建上述所有缺失的表（全部使用 IF NOT EXISTS）。
-- 对已存在的列/索引使用 IF NOT EXISTS，不影响已有数据。
--
-- 前提：
--   - update_updated_at_column() 函数已存在（20260305001 的 init_schema 已创建，
--     若不存在则 20260305001 中已定义）
--   - pgvector 扩展已安装（ALTER TABLE kb_category ADD embedding vector(1536)）
-- =============================================================================

-- ─── 1. kb_document — 知识库文档（Source of Truth）─────────────────────────

CREATE TABLE IF NOT EXISTS kb_document (
    id              SERIAL PRIMARY KEY,
    source_id       VARCHAR(50) UNIQUE,                -- 原始案例ID（采集自 support system）
    title           VARCHAR(500) NOT NULL,
    product         VARCHAR(100) DEFAULT '超融合HCI',
    content_md      TEXT NOT NULL,                     -- MD 全文（Source of Truth）
    content_hash    VARCHAR(64),                       -- SHA256，用于变更检测
    yaml_meta       JSONB,                             -- 结构化元数据（LLM 增强后）
    category_l1     VARCHAR(100),                      -- 一级分类
    category_l2     VARCHAR(100),                      -- 二级分类
    tags            TEXT[],                            -- 标签数组
    judgment_logic  TEXT,                              -- 排查逻辑（中文，LLM 生成）
    summary         TEXT,                              -- 摘要（中文，LLM 生成）
    difficulty      SMALLINT DEFAULT 3,                -- 难度 1-5
    status          VARCHAR(20) DEFAULT 'draft',       -- draft/under_review/approved/published/rejected/archived
    review_note     TEXT,
    reviewer        VARCHAR(100),
    reviewed_at     TIMESTAMP WITH TIME ZONE,
    source_type     VARCHAR(20) DEFAULT 'kb',          -- kb/sop/realtime
    has_images      BOOLEAN DEFAULT FALSE,
    verified_version VARCHAR(50),
    trace_id        VARCHAR(64),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_document_status      ON kb_document(status);
CREATE INDEX IF NOT EXISTS idx_kb_document_category    ON kb_document(category_l1, category_l2);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_id   ON kb_document(source_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_type ON kb_document(source_type);
CREATE INDEX IF NOT EXISTS idx_kb_document_trace_id    ON kb_document(trace_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_created_at  ON kb_document(created_at DESC);

-- 触发器（函数来自 init_schema，已存在）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'update_kb_document_updated_at'
          AND tgrelid = 'kb_document'::regclass
    ) THEN
        CREATE TRIGGER update_kb_document_updated_at
            BEFORE UPDATE ON kb_document
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END
$$;

COMMENT ON TABLE kb_document IS '知识库文档 v3.0 — 状态机驱动的文档生命周期管理';

-- ─── 2. kb_chunk — 文档分块 + 向量（双路检索）──────────────────────────────

CREATE TABLE IF NOT EXISTS kb_chunk (
    id          SERIAL PRIMARY KEY,
    document_id INTEGER NOT NULL REFERENCES kb_document(id) ON DELETE CASCADE,
    chunk_index SMALLINT NOT NULL,                     -- 块在文档中的顺序（0-based）
    content     TEXT NOT NULL,                         -- 块文本（~512 tokens）
    embedding   vector(384),                           -- 语义向量（384 维，bge-small-zh）
    token_count SMALLINT,
    metadata    JSONB,                                 -- 块级元数据（标题层级等）
    tsv         tsvector,                              -- BM25 全文检索
    trace_id    VARCHAR(64),
    created_at  TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_document ON kb_chunk(document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_position ON kb_chunk(document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_tsv      ON kb_chunk USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_trace_id ON kb_chunk(trace_id);

-- IVFFlat 向量索引：数据量 ≥ 1000 后手动创建
-- CREATE INDEX idx_kb_chunk_embedding ON kb_chunk USING ivfflat (embedding vector_cosine_ops) WITH (lists=100);

COMMENT ON TABLE kb_chunk IS '知识库分块 + 向量 v3.0 — 双路检索（BM25 tsvector + pgvector）';

-- ─── 3. kb_category — 知识分类树（全局分类枢纽）────────────────────────────

CREATE TABLE IF NOT EXISTS kb_category (
    id         SERIAL PRIMARY KEY,
    parent_id  INTEGER REFERENCES kb_category(id),    -- NULL = L1 根节点
    name       VARCHAR(100) NOT NULL,
    level      SMALLINT NOT NULL,                      -- 1=L1, 2=L2, 3=L3, 4=L4
    keywords   TEXT[],
    source     VARCHAR(20) DEFAULT 'manual',           -- manual/auto_generated/auto_suggested
    version    VARCHAR(20) DEFAULT '1.0',
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_category_parent   ON kb_category(parent_id);
CREATE INDEX IF NOT EXISTS idx_kb_category_level    ON kb_category(level);
CREATE INDEX IF NOT EXISTS idx_kb_category_keywords ON kb_category USING GIN (keywords);

-- 补充 20260401001 的 ALTER（code/domain/path_labels/embedding 字段）
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS code         VARCHAR(32) UNIQUE;
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS domain       VARCHAR(50);
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS path_labels  JSONB DEFAULT '[]'::jsonb;
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS embedding    vector(1536);

CREATE INDEX IF NOT EXISTS idx_kb_category_code ON kb_category(code);

-- HNSW 向量索引：198 条分类数据完成后手动创建
-- CREATE INDEX idx_kb_category_embed ON kb_category USING hnsw (embedding vector_cosine_ops);

COMMENT ON TABLE kb_category IS '知识分类树 v3.0 — 198个叶节点，4层树形结构，全局分类枢纽';
COMMENT ON COLUMN kb_category.code IS 'category_baseline.yaml 的 id 字段，如 "虚拟机-001"';
COMMENT ON COLUMN kb_category.embedding IS '分类节点语义向量（1536维），由 seed_categories.py 批量生成';

-- ─── 4. audit_log — AI 行为统一审计（合并自 prompt_audit + tool_audit_log）─

CREATE TABLE IF NOT EXISTS audit_log (
    id              VARCHAR(36)  PRIMARY KEY,           -- UUID
    audit_type      VARCHAR(20)  NOT NULL,              -- 'prompt' | 'tool_call' | 'system'
    conversation_id UUID         NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    turn_index      SMALLINT,                           -- prompt 类型：第几轮对话
    tool_name       VARCHAR(100),                       -- tool_call 类型：工具名称
    risk_level      SMALLINT,                           -- 1=只读 2=写 3=高危
    policy          VARCHAR(20),                        -- auto|notify|confirm|block
    authorized_by   VARCHAR(100),                       -- 高危操作确认用户
    payload         JSONB        NOT NULL DEFAULT '{}', -- 类型专属字段
    error           TEXT,
    duration_ms     INTEGER,
    started_at      TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    trace_id        VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_audit_log_conversation ON audit_log(conversation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_type         ON audit_log(audit_type, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_log_tool_name    ON audit_log(tool_name) WHERE tool_name IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_audit_log_risk_level   ON audit_log(risk_level) WHERE risk_level >= 2;
CREATE INDEX IF NOT EXISTS idx_audit_log_trace_id     ON audit_log(trace_id) WHERE trace_id IS NOT NULL;

COMMENT ON TABLE audit_log IS 'AI 行为统一审计日志（合并自 prompt_audit + tool_audit_log）';

-- ─── 5. kb_sop_node — SOP 决策树节点（待迁移至 knowledge_atoms，暂保持）──

-- 注意：kb_sop_node 架构上将被废弃（迁移方向：knowledge_atoms）
-- 当前 kb-service 代码仍引用此表，创建以维持服务正常运行
CREATE TABLE IF NOT EXISTS kb_sop_node (
    id         SERIAL PRIMARY KEY,
    skill_id   VARCHAR(100) NOT NULL,
    node_name  VARCHAR(200) NOT NULL,
    parent_id  INTEGER REFERENCES kb_sop_node(id),
    keywords   TEXT[] NOT NULL,
    file_path  VARCHAR(500),
    content    TEXT,
    level      SMALLINT DEFAULT 1,
    sort_order SMALLINT DEFAULT 0,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_sop_node_skill    ON kb_sop_node(skill_id);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_keywords ON kb_sop_node USING GIN (keywords);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_parent   ON kb_sop_node(parent_id);

COMMENT ON TABLE kb_sop_node IS 'SOP 决策树节点（待废弃，迁移方向：knowledge_atoms），当前 kb-service 仍引用';

-- ─── 6. kb_synonym — 同义词映射（可选合并入 kb_category.metadata）──────────

CREATE TABLE IF NOT EXISTS kb_synonym (
    id        SERIAL PRIMARY KEY,
    term      VARCHAR(100) NOT NULL,
    canonical VARCHAR(100) NOT NULL,
    created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE (term, canonical)
);

COMMENT ON TABLE kb_synonym IS '术语缩写到标准名映射，提升 BM25 召回率（< 500 条时可合并入 kb_category.metadata）';

-- ─── 7. kbd_entry — KBD 知识条目（依赖 kb_category，必须最后创建）──────────

CREATE TABLE IF NOT EXISTS kbd_entry (
    id                 BIGSERIAL    PRIMARY KEY,
    support_id         VARCHAR(20)  UNIQUE NOT NULL,
    support_url        TEXT,
    title              TEXT         NOT NULL,
    content_md         TEXT,
    metadata           JSONB        NOT NULL DEFAULT '{}',
    -- 分类（双轨）
    category_id        VARCHAR(32)  REFERENCES kb_category(code),
    ai_category_id     VARCHAR(32),
    ai_category_conf   FLOAT,
    ai_category_reason TEXT,
    -- 检索字段（published 时生成）
    embedding          vector(1536),
    tsv                tsvector,
    -- 状态机：draft → published → archived / rejected
    status             VARCHAR(20)  NOT NULL DEFAULT 'draft',
    reviewer_id        INTEGER,
    reviewed_at        TIMESTAMPTZ,
    review_note        TEXT,
    published_at       TIMESTAMPTZ,
    archived_at        TIMESTAMPTZ,
    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_kbd_entry_status      ON kbd_entry(status);
CREATE INDEX IF NOT EXISTS idx_kbd_entry_category    ON kbd_entry(category_id) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_kbd_entry_ai_category ON kbd_entry(ai_category_id);
CREATE INDEX IF NOT EXISTS idx_kbd_entry_published   ON kbd_entry(published_at DESC) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS idx_kbd_entry_tsv         ON kbd_entry USING GIN(tsv);
CREATE INDEX IF NOT EXISTS idx_kbd_entry_metadata    ON kbd_entry USING GIN(metadata);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trigger_kbd_entry_updated_at'
          AND tgrelid = 'kbd_entry'::regclass
    ) THEN
        CREATE TRIGGER trigger_kbd_entry_updated_at
            BEFORE UPDATE ON kbd_entry
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END
$$;

COMMENT ON TABLE kbd_entry IS 'KBD 知识条目 — 全生命周期（draft→published→archived/rejected）；属于知识库生产管道';
COMMENT ON COLUMN kbd_entry.support_id IS '深信服案例ID（API rows.id），幂等键';
COMMENT ON COLUMN kbd_entry.category_id IS '人工确认分类（引用 kb_category.code，如"虚拟机-001"）';

-- migrate:down
-- 不提供自动降级（逐表手动删除，注意外键依赖顺序：kbd_entry → kb_category → kb_sop_node/kb_synonym/kb_chunk → kb_document → audit_log）
