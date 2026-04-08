-- migrate:up

-- =============================================================================
-- SOP 排障手册 — sop_document / sop_chunk 建表（版本号修正）
-- 创建日期：2026-04-08
--
-- 背景：
--   20260402001_sop_tables.sql 与 20260402001_kb_category_sop_hit_tracking.sql
--   使用了相同的 version 号（20260402001）。dbmate 以文件名开头纯数字为主键，
--   重复 version 时只执行字典序更早的文件，sop_tables 因此被静默跳过，
--   sop_document / sop_chunk 两张表从未在 K8s 环境中创建。
--   本迁移将该建表 DDL 重新以新 version 纳入执行链。
--
-- 幂等保证：
--   所有 CREATE TABLE / INDEX / TRIGGER 均使用 IF NOT EXISTS 或等价写法，
--   可安全重复执行（若表已存在则无副作用）。
-- =============================================================================

-- ─── 1. sop_document 表 ──────────────────────────────────────────────────────
-- 生命周期：draft → published → archived

CREATE TABLE IF NOT EXISTS sop_document (
    id           SERIAL PRIMARY KEY,
    source_id    VARCHAR(100) UNIQUE,          -- 幂等键，如 sop-vm-start-failure
    category_id  VARCHAR(32) REFERENCES kb_category(code),
    title        VARCHAR(500),                 -- SOP 标题
    content_md   TEXT,                         -- 完整 SOP Markdown
    docx_hash    VARCHAR(64),                  -- 源文件哈希（幂等去重）
    status       VARCHAR(20) DEFAULT 'draft',  -- draft/published/archived
    reviewer_id  INTEGER,
    reviewed_at  TIMESTAMPTZ,
    published_at TIMESTAMPTZ,
    created_at   TIMESTAMPTZ DEFAULT NOW(),
    updated_at   TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sop_document_category
    ON sop_document(category_id) WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_sop_document_status
    ON sop_document(status);

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger
        WHERE tgname = 'trigger_sop_document_updated_at'
          AND tgrelid = 'sop_document'::regclass
    ) THEN
        CREATE TRIGGER trigger_sop_document_updated_at
            BEFORE UPDATE ON sop_document
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END
$$;

COMMENT ON TABLE  sop_document              IS 'SOP 排障手册文档 — 全生命周期（draft→published→archived）';
COMMENT ON COLUMN sop_document.source_id    IS '幂等键，格式如 sop-vm-start-failure';
COMMENT ON COLUMN sop_document.category_id  IS '关联 KB 分类（kb_category.code）';
COMMENT ON COLUMN sop_document.content_md   IS '完整 SOP Markdown 文档';
COMMENT ON COLUMN sop_document.docx_hash    IS '源 .docx 文件 SHA256 哈希，用于幂等去重';
COMMENT ON COLUMN sop_document.status       IS 'draft=待审核, published=已发布可检索, archived=已归档';

-- ─── 2. sop_chunk 表 ─────────────────────────────────────────────────────────
-- 按 SOP 章节拆分，支持向量检索和全文检索

CREATE TABLE IF NOT EXISTS sop_chunk (
    id            SERIAL PRIMARY KEY,
    document_id   INTEGER REFERENCES sop_document(id) ON DELETE CASCADE,
    chunk_index   SMALLINT NOT NULL,            -- 分块序号（0-based）
    chapter_title VARCHAR(200),                 -- 章节标题
    content       TEXT NOT NULL,                -- 分块内容
    embedding     vector(1536),                 -- 语义向量（1536 维）
    tsv           tsvector,                     -- 全文检索向量
    created_at    TIMESTAMPTZ DEFAULT NOW()
);

CREATE INDEX IF NOT EXISTS idx_sop_chunk_document
    ON sop_chunk(document_id);

CREATE INDEX IF NOT EXISTS idx_sop_chunk_tsv
    ON sop_chunk USING GIN(tsv);

COMMENT ON TABLE  sop_chunk               IS 'SOP 分块检索 — 按章节拆分，支持向量检索和全文检索';
COMMENT ON COLUMN sop_chunk.document_id   IS '关联 SOP 文档（级联删除）';
COMMENT ON COLUMN sop_chunk.chunk_index   IS '分块序号（0-based），同一 document_id 内有序';
COMMENT ON COLUMN sop_chunk.chapter_title IS '章节标题，如"问题诊断"、"解决方案"';
COMMENT ON COLUMN sop_chunk.embedding     IS '分块语义向量（1536 维），用于向量检索';
COMMENT ON COLUMN sop_chunk.tsv           IS '全文检索向量（tsvector），用于 BM25 检索';


-- migrate:down
DROP TABLE IF EXISTS sop_chunk;
DROP TABLE IF EXISTS sop_document;
