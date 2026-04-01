-- migrate:up

-- =============================================================================
-- SOP 排障手册 — 数据库迁移
-- 创建日期：2026-04-02
-- 变更内容：
--   1. sop_document  SOP 文档表（全生命周期管理）
--   2. sop_chunk     SOP 分块检索表（按章节拆分，支持向量检索）
-- 前提：
--   init_schema.sql 已执行（kb_category 表已存在）
--   pgvector 扩展已安装
-- =============================================================================

-- ─── 1. SOP 文档表 ───────────────────────────────────────────────────────────

-- 生命周期：draft → published → archived
-- source_id：幂等键，格式如 sop-vm-start-failure（对应 SOP 文档内部标识）

CREATE TABLE IF NOT EXISTS sop_document (
    id          SERIAL PRIMARY KEY,
    source_id   VARCHAR(100) UNIQUE,             -- 幂等键，如 sop-vm-start-failure
    category_id VARCHAR(32) REFERENCES kb_category(code),
                                            -- 关联 KB 分类（可选）
    title       VARCHAR(500),                    -- SOP 标题
    content_md  TEXT,                            -- 完整 SOP Markdown
    docx_hash   VARCHAR(64),                     -- 源文件哈希（幂等去重）
    status      VARCHAR(20) DEFAULT 'draft',     -- draft/published/archived
    reviewer_id INTEGER,                         -- 审核人 ID
    reviewed_at TIMESTAMPTZ,                     -- 审核时间
    published_at TIMESTAMPTZ,                    -- 发布时间
    created_at  TIMESTAMPTZ DEFAULT NOW(),
    updated_at  TIMESTAMPTZ DEFAULT NOW()
);

-- 索引：分类检索（仅 published）
CREATE INDEX idx_sop_document_category
    ON sop_document(category_id) WHERE status = 'published';

-- 索引：状态筛选
CREATE INDEX idx_sop_document_status
    ON sop_document(status);

-- updated_at 自动维护触发器
CREATE TRIGGER trigger_sop_document_updated_at
    BEFORE UPDATE ON sop_document
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- 表注释
COMMENT ON TABLE sop_document IS 'SOP 排障手册文档 — 全生命周期（draft→published→archived）';
COMMENT ON COLUMN sop_document.source_id IS '幂等键，格式如 sop-vm-start-failure';
COMMENT ON COLUMN sop_document.category_id IS '关联 KB 分类（kb_category.code）';
COMMENT ON COLUMN sop_document.content_md IS '完整 SOP Markdown 文档';
COMMENT ON COLUMN sop_document.docx_hash IS '源 .docx 文件 SHA256 哈希，用于幂等去重';
COMMENT ON COLUMN sop_document.status IS 'draft=待审核, published=已发布可检索, archived=已归档';


-- ─── 2. SOP 分块检索表 ───────────────────────────────────────────────────────

-- 按 SOP 章节拆分，支持：
--   1. 向量语义检索（embedding）
--   2. 全文检索（tsv）

CREATE TABLE IF NOT EXISTS sop_chunk (
    id           SERIAL PRIMARY KEY,
    document_id  INTEGER REFERENCES sop_document(id) ON DELETE CASCADE,
                                            -- 关联 SOP 文档（删除时级联）
    chunk_index  SMALLINT NOT NULL,         -- 分块序号（0-based）
    chapter_title VARCHAR(200),             -- 章节标题（如"问题诊断"）
    content      TEXT NOT NULL,             -- 分块内容
    embedding    vector(1536),              -- 语义向量（1536 维）
    tsv          tsvector,                  -- 全文检索向量
    created_at   TIMESTAMPTZ DEFAULT NOW()
);

-- 索引：文档关联
CREATE INDEX idx_sop_chunk_document
    ON sop_chunk(document_id);

-- 索引：全文检索（GIN）
CREATE INDEX idx_sop_chunk_tsv
    ON sop_chunk USING GIN(tsv);

-- 表注释
COMMENT ON TABLE sop_chunk IS 'SOP 分块检索 — 按章节拆分，支持向量检索和全文检索';
COMMENT ON COLUMN sop_chunk.document_id IS '关联 SOP 文档（级联删除）';
COMMENT ON COLUMN sop_chunk.chunk_index IS '分块序号（0-based），同一 document_id 内有序';
COMMENT ON COLUMN sop_chunk.chapter_title IS '章节标题，如"问题诊断"、"解决方案"';
COMMENT ON COLUMN sop_chunk.embedding IS '分块语义向量（1536 维），用于向量检索';
COMMENT ON COLUMN sop_chunk.tsv IS '全文检索向量（tsvector），用于 BM25 检索';


-- migrate:down

-- 降级：按依赖顺序删除（先删子表，再删主表）
DROP TABLE IF EXISTS sop_chunk;
DROP TABLE IF EXISTS sop_document;