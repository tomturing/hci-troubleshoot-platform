-- HCI Troubleshoot Platform - KB Schema Migration
-- Version: v2.0 → v3.0
-- Date: 2026-03-05
--
-- 作用：将已有生产/开发环境中的旧 kb_document（UUID主键）和 kb_chunk（UUID主键）
--       安全迁移为 v3.0 新 Schema（SERIAL整型主键 + 完整字段集）
--
-- 执行方式：
--   psql -U hci_admin -d hci_troubleshoot -f migrate_kb_v3.sql
--
-- 注意事项：
--   1. 迁移前请确保有数据库备份
--   2. 旧表会被重命名为 kb_document_v2_bak / kb_chunk_v2_bak，不会丢失数据
--   3. 如旧表中有数据需迁移，参考脚本末尾的数据迁移示例
--   4. 脚本可重复执行（幂等），已存在的新表不会被覆盖

BEGIN;

-- ============================================================================
-- 步骤 1：备份旧表（如果存在）
-- ============================================================================

DO $$
BEGIN
    -- 备份旧 kb_chunk（必须先于 kb_document，因为有外键依赖）
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'kb_chunk'
          AND table_schema = 'public'
    ) THEN
        -- 检查是否是旧版本（有 chunk_id UUID 列）
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'kb_chunk'
              AND column_name = 'chunk_id'
        ) THEN
            ALTER TABLE kb_chunk RENAME TO kb_chunk_v2_bak;
            RAISE NOTICE '已将旧 kb_chunk 重命名为 kb_chunk_v2_bak';
        ELSE
            RAISE NOTICE 'kb_chunk 已是 v3.0 格式，跳过备份';
        END IF;
    END IF;

    -- 备份旧 kb_document
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'kb_document'
          AND table_schema = 'public'
    ) THEN
        -- 检查是否是旧版本（有 doc_id UUID 列）
        IF EXISTS (
            SELECT 1 FROM information_schema.columns
            WHERE table_name = 'kb_document'
              AND column_name = 'doc_id'
        ) THEN
            ALTER TABLE kb_document RENAME TO kb_document_v2_bak;
            RAISE NOTICE '已将旧 kb_document 重命名为 kb_document_v2_bak';
        ELSE
            RAISE NOTICE 'kb_document 已是 v3.0 格式，跳过备份';
        END IF;
    END IF;
END $$;

-- ============================================================================
-- 步骤 2：创建 v3.0 新表（与 init_schema.sql §8 保持一致）
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
    reviewed_at     TIMESTAMP WITH TIME ZONE,
    source_type     VARCHAR(20) DEFAULT 'kb',
    has_images      BOOLEAN DEFAULT FALSE,
    verified_version VARCHAR(50),
    trace_id        VARCHAR(64),
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_document_status     ON kb_document(status);
CREATE INDEX IF NOT EXISTS idx_kb_document_category   ON kb_document(category_l1, category_l2);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_id  ON kb_document(source_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_source_type ON kb_document(source_type);
CREATE INDEX IF NOT EXISTS idx_kb_document_trace_id   ON kb_document(trace_id);
CREATE INDEX IF NOT EXISTS idx_kb_document_created_at ON kb_document(created_at DESC);

-- updated_at 自动更新 trigger（需 update_updated_at_column 函数已存在）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'update_kb_document_updated_at'
    ) THEN
        CREATE TRIGGER update_kb_document_updated_at
            BEFORE UPDATE ON kb_document
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END $$;

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
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_chunk_document    ON kb_chunk(document_id);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_position    ON kb_chunk(document_id, chunk_index);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_tsv         ON kb_chunk USING GIN (tsv);
CREATE INDEX IF NOT EXISTS idx_kb_chunk_trace_id    ON kb_chunk(trace_id);

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
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_sop_node_skill    ON kb_sop_node(skill_id);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_keywords ON kb_sop_node USING GIN (keywords);
CREATE INDEX IF NOT EXISTS idx_kb_sop_node_parent   ON kb_sop_node(parent_id);

CREATE TABLE IF NOT EXISTS kb_category (
    id              SERIAL PRIMARY KEY,
    parent_id       INTEGER REFERENCES kb_category(id),
    name            VARCHAR(100) NOT NULL,
    level           SMALLINT NOT NULL,
    keywords        TEXT[],
    source          VARCHAR(20) DEFAULT 'manual',
    version         VARCHAR(20) DEFAULT '1.0',
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
);

CREATE INDEX IF NOT EXISTS idx_kb_category_parent   ON kb_category(parent_id);
CREATE INDEX IF NOT EXISTS idx_kb_category_level    ON kb_category(level);
CREATE INDEX IF NOT EXISTS idx_kb_category_keywords ON kb_category USING GIN (keywords);

CREATE TABLE IF NOT EXISTS kb_synonym (
    id              SERIAL PRIMARY KEY,
    term            VARCHAR(100) NOT NULL,
    canonical       VARCHAR(100) NOT NULL,
    created_at      TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(term, canonical)
);

COMMIT;

-- ============================================================================
-- 步骤 3（可选）：将旧数据迁移到新表
-- ============================================================================
-- 如果旧 kb_document_v2_bak 中有数据需要保留，取消注释以下语句执行：
--
-- INSERT INTO kb_document (title, content_md, source_type, trace_id, created_at, updated_at)
-- SELECT
--     title,
--     content AS content_md,
--     doc_type AS source_type,
--     trace_id,
--     created_at,
--     updated_at
-- FROM kb_document_v2_bak
-- ON CONFLICT DO NOTHING;
--
-- 注意：旧 kb_chunk（UUID外键）无法直接迁移，因为新表使用 SERIAL integer 主键
-- 建议重新对 kb_document 执行分块 + embedding 入库流程

-- ============================================================================
-- 验证结果
-- ============================================================================

SELECT
    table_name,
    (SELECT COUNT(*) FROM information_schema.columns c WHERE c.table_name = t.table_name) AS column_count
FROM information_schema.tables t
WHERE table_schema = 'public'
  AND table_name IN ('kb_document', 'kb_chunk', 'kb_sop_node', 'kb_category', 'kb_synonym')
ORDER BY table_name;
