-- SOP 变量包（Variable Pack）
--
-- 目的：消灭"同一份通用排障变量被复制粘贴到多个 SOP，改一处漏两处"的缺陷模式。
-- 背景：SOP#1/#2/#3 各有 22 个同名变量（apache2_count / qemu_log_path / ...），
--       三份副本全部是 `tool_call + acquisition_tool=NULL`，一次整改要改 66 处。
--
-- 设计：
--   * 包只是"变量定义的命名集合"，展开后仍写入 sop_document.variable_schema，
--     运行时零改动（永远读扁平数组）；
--   * sop_document.applied_packs 记录引用关系，用于漂移检测与批量同步；
--   * 包不是运行时依赖，删包不影响已发布的 SOP。

CREATE TABLE IF NOT EXISTS sop_variable_pack (
    id              SERIAL PRIMARY KEY,
    pack_name       VARCHAR(64) NOT NULL UNIQUE,
    version         INTEGER NOT NULL DEFAULT 1,
    description     TEXT NOT NULL DEFAULT '',
    variables       JSONB NOT NULL DEFAULT '[]'::jsonb,
    is_active       BOOLEAN NOT NULL DEFAULT TRUE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT chk_sop_variable_pack_variables_array CHECK (jsonb_typeof(variables) = 'array')
);

COMMENT ON TABLE sop_variable_pack IS 'SOP 变量包：可被多个 SOP 复用的命名变量集合';
COMMENT ON COLUMN sop_variable_pack.variables IS '变量定义数组，结构与 sop_document.variable_schema 一致';

-- SOP 引用了哪些包（含版本），用于漂移检测
ALTER TABLE sop_document
    ADD COLUMN IF NOT EXISTS applied_packs JSONB NOT NULL DEFAULT '[]'::jsonb;

COMMENT ON COLUMN sop_document.applied_packs IS
    '已应用的变量包引用列表，形如 [{"pack":"hci_common_vt_diag","version":1}]';

CREATE INDEX IF NOT EXISTS idx_sop_document_applied_packs
    ON sop_document USING GIN (applied_packs);
