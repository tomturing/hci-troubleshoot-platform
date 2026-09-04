-- 信号资产与复盘记录补齐唯一链路标识。
-- 兼容 20260904000000 已在环境执行的部署：已有行使用迁移批次标识，
-- 新写入由应用传入当前 W3C trace_id。
ALTER TABLE signal_modeling_template
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64) NOT NULL DEFAULT 'migration:20260904000000';
ALTER TABLE signal_best_practice
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64) NOT NULL DEFAULT 'migration:20260904000000';
ALTER TABLE signal_failure_extraction
    ADD COLUMN IF NOT EXISTS trace_id VARCHAR(64);
UPDATE signal_failure_extraction
SET trace_id = 'migration:20260904000000'
WHERE trace_id IS NULL;
ALTER TABLE signal_failure_extraction
    ALTER COLUMN trace_id SET NOT NULL;
CREATE INDEX IF NOT EXISTS idx_signal_asset_template_trace_id ON signal_modeling_template(trace_id);
CREATE INDEX IF NOT EXISTS idx_signal_asset_best_practice_trace_id ON signal_best_practice(trace_id);
CREATE INDEX IF NOT EXISTS idx_signal_failure_trace_id ON signal_failure_extraction(trace_id);
