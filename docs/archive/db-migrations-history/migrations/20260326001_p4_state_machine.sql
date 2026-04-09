-- migrate:up

-- P4 数据库迁移：raw_cases 和 knowledge_atoms 表
-- 执行方式：psql -f database/migrate_p4_v1.sql

-- ──────────────────────────────────────────────────────────────────────────────
-- raw_cases：历史工单原始数据（T16 工单数据管道）
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS raw_cases (
    id             BIGSERIAL    PRIMARY KEY,
    case_id        VARCHAR(64)  NOT NULL UNIQUE,     -- 来源系统的工单 ID（幂等键）
    source_url     TEXT         NOT NULL DEFAULT '',  -- 工单在来源系统的 URL
    content_text   TEXT         NOT NULL DEFAULT '',  -- 已脱敏的工单正文（Markdown 格式）
    images         JSONB        NOT NULL DEFAULT '[]',-- 图片/附件 URL 列表（已脱敏）
    classification VARCHAR(128) NOT NULL DEFAULT '',  -- 故障分类（如 vm_power_failure）
    quality_score  SMALLINT     NOT NULL DEFAULT 0,   -- 质量评分 0-100
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW()
);

COMMENT ON TABLE  raw_cases              IS 'HCI 历史工单原始数据（已脱敏），供知识库提炼使用';
COMMENT ON COLUMN raw_cases.quality_score IS '工单质量评分 0-100，低于 20 分的不入库';
COMMENT ON COLUMN raw_cases.images        IS 'JSON 数组，每项 {"url": "...", "type": "error_screenshot|command_output|..."}';

CREATE INDEX IF NOT EXISTS idx_raw_cases_classification
    ON raw_cases (classification);
CREATE INDEX IF NOT EXISTS idx_raw_cases_quality
    ON raw_cases (quality_score DESC);
CREATE INDEX IF NOT EXISTS idx_raw_cases_created_at
    ON raw_cases (created_at DESC);

-- 自动更新 updated_at
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


-- ──────────────────────────────────────────────────────────────────────────────
-- knowledge_atoms：知识原子候选（T17 知识反馈闭环）
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TABLE IF NOT EXISTS knowledge_atoms (
    id             VARCHAR(32)  PRIMARY KEY,          -- 格式：ka-{12位hex}
    atom_type      VARCHAR(32)  NOT NULL,              -- diagnostic_step|fix_action|decision_gate
    category_id    VARCHAR(64)  NOT NULL DEFAULT '',   -- 关联的故障分类 ID
    trigger_json   JSONB        NOT NULL DEFAULT '{}', -- 触发条件 {stage, conditions, ...}
    content_json   JSONB        NOT NULL DEFAULT '{}', -- 内容 {full_text, commands, ...}
    source_type    VARCHAR(16)  NOT NULL DEFAULT 'session', -- session|manual
    source_ref     VARCHAR(64)  NOT NULL DEFAULT '',   -- 来源 session_id 或操作者
    verified       BOOLEAN      NOT NULL DEFAULT FALSE,-- 人工审核状态
    confidence     NUMERIC(3,2) NOT NULL DEFAULT 0.70, -- 置信度 0.00-1.00
    created_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    updated_at     TIMESTAMPTZ  NOT NULL DEFAULT NOW(),
    verified_at    TIMESTAMPTZ,                        -- 审核通过时间
    verified_by    VARCHAR(64)                         -- 审核者 ID
);

COMMENT ON TABLE  knowledge_atoms           IS 'AI 自动提炼的知识原子候选，verified=false 时待人工审核';
COMMENT ON COLUMN knowledge_atoms.verified  IS '人工审核通过后设为 true，被知识检索系统使用';
COMMENT ON COLUMN knowledge_atoms.confidence IS '机器生成默认 0.70，人工修正后可调高';

CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_verified
    ON knowledge_atoms (verified, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_category
    ON knowledge_atoms (category_id);
CREATE INDEX IF NOT EXISTS idx_knowledge_atoms_source
    ON knowledge_atoms (source_ref);

-- 自动更新 updated_at
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


-- migrate:down
-- 不提供自动降级，手动回滚
