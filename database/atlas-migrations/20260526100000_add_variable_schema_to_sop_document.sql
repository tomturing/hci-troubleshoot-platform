-- migrate:up
-- T-AGT-24：为 sop_document 表添加 variable_schema 字段，用于存储 SOP 变量定义
ALTER TABLE sop_document ADD COLUMN IF NOT EXISTS variable_schema jsonb DEFAULT '[]'::jsonb;

COMMENT ON COLUMN sop_document.variable_schema IS 'SOP 变量定义列表（JSONB 数组），每个元素包含 name、display_name、description、type、acquisition_strategy 等字段。approve 时自动解析生成';

-- migrate:down
ALTER TABLE sop_document DROP COLUMN IF EXISTS variable_schema;