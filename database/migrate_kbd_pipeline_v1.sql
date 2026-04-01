-- =============================================================================
-- KBD 知识生产管道 — 数据库迁移 v1
-- 创建日期：2026-04-01
-- 变更内容：
--   1. kb_category  添加 code / domain / path_labels / embedding 字段
--                   （支持 category_baseline.yaml 的 id 字段格式）
--   2. kbd_entry    新建（KBD 知识条目全生命周期）
--                   中间产物（原始JSON、图片、Vision描述）使用文件存储，不入库
-- 前提：
--   init_schema.sql 已执行（update_updated_at_column 函数已存在）
-- =============================================================================

-- ─── 1. 扩展 kb_category ────────────────────────────────────────────────────

-- code：对应 category_baseline.yaml 的 id 字段（如 "虚拟机-001"）
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS
    code VARCHAR(32) UNIQUE;

-- domain：一级技术域（中文，如 "虚拟机" / "网络" / "存储" / "硬件" / "平台"）
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS
    domain VARCHAR(50);

-- path_labels：从顶层到叶节点的完整路径（数组，供 AI 分类 prompt 使用）
-- 示例：["虚拟机", "虚拟机创建"]
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS
    path_labels JSONB DEFAULT '[]'::jsonb;

-- embedding：分类节点的语义向量（1536 维，用于意图识别路由）
-- 注意：extension pgvector 必须已安装
ALTER TABLE kb_category ADD COLUMN IF NOT EXISTS
    embedding vector(1536);

CREATE INDEX IF NOT EXISTS idx_kb_category_code
    ON kb_category(code);

COMMENT ON COLUMN kb_category.code IS
    'category_baseline.yaml 的 id 字段，格式：<domain>-<seq>，如 "虚拟机-001"';
COMMENT ON COLUMN kb_category.embedding IS
    '分类节点语义向量（1536维），用于意图识别路由，由初始化脚本批量生成';


-- ─── 2. kbd_entry — KBD 知识条目（全生命周期）────────────────────────────────

-- 中间产物说明：KBD 生产阶段的原始 JSON、图片文件、Vision 描述均使用文件存储
-- 文件结构：scripts/kbd/cache/{support_id}/
--   raw.json          — API 完整响应（存在 = fetch done，跳过）
--   fetch.failed      — 失败记录（JSON）
--   abnormal.json     — 必填字段缺失记录（写入异常队列，不入 kbd_entry）
--   img_0.png         — 原始图片（按序号）
--   img_0.desc.txt    — Vision 描述（存在 = vision done，跳过）
--   img_0.failed      — Vision 失败标记（可独立重跑）
-- 文件锁：{support_id}.lock（flock 防并发重复抓取）

-- 生命周期：draft → published → archived
--                  ↘ rejected（填写 review_note）

CREATE TABLE IF NOT EXISTS kbd_entry (
    id                 BIGSERIAL    PRIMARY KEY,
    support_id         VARCHAR(20)  UNIQUE NOT NULL,
        -- 深信服案例ID（对应 API rows.id，如 "36156"）
    support_url        TEXT,
        -- 原始案例页面URL，供审核人点击溯源查看
        -- 格式：https://support.sangfor.com.cn/cases/list?product_id=33&type=1&category_id={support_id}&isOpen=true
    title              TEXT         NOT NULL,
        -- 案例标题，对应 API rows.name

    content_md         TEXT,
        -- 全段结构化 Markdown（含图片语义块，供审核编辑/检索/展示）
        -- 必填段：#问题描述、#有效排查步骤、#解决方案；其余段有内容则包含
        -- 图片替换格式：> **【截图说明】**：{vision_desc}

    metadata           JSONB        NOT NULL DEFAULT '{}',
        -- API 补充字段，结构示例：
        -- {
        --   "sangfor_main_module": "网络问题",    -- API rows.mainModuleNames
        --   "sangfor_sub_module":  "实体机网络",  -- API rows.childModuleNames
        --   "suite_version":       "通用",        -- API rows.suiteVersion
        --   "sangfor_updated_at":  "2026-01-15",  -- API rows.updateTime
        --   "sangfor_created_at":  null,          -- API rows.createTime
        --   "create_admin_id":     "68532",       -- API rows.createAdminId（工程师追溯）
        --   "update_admin_id":     "14201"        -- API rows.updateAdminId（工程师追溯）
        -- }

    -- ── 分类（双轨：AI建议 + 人工确认）──────────────────────────────────
    category_id        VARCHAR(32)  REFERENCES kb_category(code),
        -- 人工确认分类（审核时确认或修改，NULL=未分类）
    ai_category_id     VARCHAR(32),
        -- AI 分类建议（对应 kb_category.code）
    ai_category_conf   FLOAT,
        -- AI 置信度（0~1），< 0.5 时审核页提示「需人工重新分类」
    ai_category_reason TEXT,
        -- AI 分类理由（供审核参考）

    -- ── 检索字段（status=published 时生成）──────────────────────────────
    embedding          vector(1536),
        -- content_md 全文语义向量（1536维），published 时由 kb-service 异步生成
    tsv                tsvector,
        -- BM25 全文检索（基于 title + content_md）

    -- ── 状态机 ──────────────────────────────────────────────────────────
    status             VARCHAR(20)  NOT NULL DEFAULT 'draft',
        -- draft       = 脚本导入，等待审核
        -- published   = 审核通过，embedding 已生成，可被检索
        -- archived    = 手动归档，不参与检索
        -- rejected    = 审核拒绝（review_note 记录原因）
    reviewer_id        INTEGER,
        -- 审核人 ID
    reviewed_at        TIMESTAMPTZ,
        -- 审核时间
    review_note        TEXT,
        -- 审核备注（拒绝原因 / 修改说明）
    published_at       TIMESTAMPTZ,
        -- 发布时间（触发 embedding 生成）
    archived_at        TIMESTAMPTZ,
        -- 归档时间（手动触发）

    created_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at         TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_kbd_entry_status
    ON kbd_entry(status);

CREATE INDEX IF NOT EXISTS idx_kbd_entry_category
    ON kbd_entry(category_id) WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_kbd_entry_ai_category
    ON kbd_entry(ai_category_id);

CREATE INDEX IF NOT EXISTS idx_kbd_entry_published
    ON kbd_entry(published_at DESC) WHERE status = 'published';

CREATE INDEX IF NOT EXISTS idx_kbd_entry_tsv
    ON kbd_entry USING GIN(tsv);

CREATE INDEX IF NOT EXISTS idx_kbd_entry_metadata
    ON kbd_entry USING GIN(metadata);

-- updated_at 自动维护触发器
CREATE TRIGGER trigger_kbd_entry_updated_at
    BEFORE UPDATE ON kbd_entry
    FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();

-- 注释
COMMENT ON TABLE kbd_entry IS
    'KBD 知识条目 — 全生命周期（draft→published→archived/rejected）；中间产物见 scripts/kbd/cache/ 文件存储';
COMMENT ON COLUMN kbd_entry.support_id IS
    '深信服案例ID（API rows.id），幂等键';
COMMENT ON COLUMN kbd_entry.support_url IS
    '原始案例页面URL，供审核人溯源：https://support.sangfor.com.cn/cases/list?product_id=33&type=1&category_id={id}&isOpen=true';
COMMENT ON COLUMN kbd_entry.content_md IS
    '全段结构化Markdown，包含所有有内容的HTML section（图片替换为Vision语义块）';
COMMENT ON COLUMN kbd_entry.metadata IS
    'API补充字段：sangfor_main_module/sangfor_sub_module/suite_version/sangfor_updated_at/sangfor_created_at/create_admin_id/update_admin_id';
COMMENT ON COLUMN kbd_entry.status IS
    'draft=待审核, published=已发布可检索, archived=已归档不检索, rejected=已拒绝';
COMMENT ON COLUMN kbd_entry.ai_category_conf IS
    'AI分类置信度 0~1，< 0.5 时审核页提示需人工重新确认';
COMMENT ON COLUMN kbd_entry.embedding IS
    'content_md 全文语义向量（1536维），status→published 时由 kb-service 异步生成';
