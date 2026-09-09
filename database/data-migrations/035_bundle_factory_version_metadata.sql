-- Bundle 工厂版本元数据表
-- 用于跟踪每个 Bundle 使用的工厂版本，支持自动迁移

CREATE TABLE IF NOT EXISTS bundle_metadata (
    id SERIAL PRIMARY KEY,
    kbd_id INTEGER NOT NULL REFERENCES kbd_entry(id) ON DELETE CASCADE,
    support_id VARCHAR(20) NOT NULL,
    bundle_digest VARCHAR(128) NOT NULL,
    factory_version VARCHAR(50) NOT NULL,
    compiler_revision VARCHAR(100),
    compiled_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    command_template TEXT,
    parameters JSONB DEFAULT '{}',
    created_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMP WITH TIME ZONE NOT NULL DEFAULT NOW(),
    UNIQUE(kbd_id, bundle_digest),
    UNIQUE(support_id, bundle_digest)
);

-- 索引
CREATE INDEX IF NOT EXISTS idx_bundle_metadata_kbd_id ON bundle_metadata(kbd_id);
CREATE INDEX IF NOT EXISTS idx_bundle_metadata_support_id ON bundle_metadata(support_id);
CREATE INDEX IF NOT EXISTS idx_bundle_metadata_factory_version ON bundle_metadata(factory_version);
CREATE INDEX IF NOT EXISTS idx_bundle_metadata_compiled_at ON bundle_metadata(compiled_at DESC);

-- 注释
COMMENT ON TABLE bundle_metadata IS 'Bundle 工厂版本元数据表，用于跟踪和迁移';
COMMENT ON COLUMN bundle_metadata.factory_version IS 'Bundle 工厂版本（如 v4-fixture-assets）';
COMMENT ON COLUMN bundle_metadata.compiler_revision IS '编译器完整修订版本';
COMMENT ON COLUMN bundle_metadata.compiled_at IS 'Bundle 编译时间';

-- 触发器：自动更新 updated_at
CREATE OR REPLACE FUNCTION update_bundle_metadata_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = NOW();
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER update_bundle_metadata_updated_at
    BEFORE UPDATE ON bundle_metadata
    FOR EACH ROW
    EXECUTE FUNCTION update_bundle_metadata_updated_at();
