-- ===================================================================
-- Migration 006: 对齐 bridge_execution_logs 表结构到 desired_schema
-- 工单: Q2026072055042 - terminal_bridge 回采链路断裂修复
-- 日期: 2026-07-20
-- ===================================================================

-- 说明：
-- 1. 生产环境表由 migration 005 创建，部分字段类型与 desired_schema 不一致
-- 2. desired_schema.sql 是权威定义，生产应向其对齐
-- 3. 本 migration 幂等，可安全重复执行

-- ===================================================================
-- 1. 字段类型对齐
-- ===================================================================

-- case_id: TEXT NOT NULL -> varchar(32) nullable
ALTER TABLE bridge_execution_logs ALTER COLUMN case_id TYPE varchar(32);
ALTER TABLE bridge_execution_logs ALTER COLUMN case_id DROP NOT NULL;

-- trace_id: TEXT -> varchar(64)
ALTER TABLE bridge_execution_logs ALTER COLUMN trace_id TYPE varchar(64);

-- custom_ui: TEXT -> varchar(255)
ALTER TABLE bridge_execution_logs ALTER COLUMN custom_ui TYPE varchar(255);

-- user_id: TEXT -> varchar(64)
ALTER TABLE bridge_execution_logs ALTER COLUMN user_id TYPE varchar(64);

-- node_ip: TEXT -> varchar(64)
ALTER TABLE bridge_execution_logs ALTER COLUMN node_ip TYPE varchar(64);

-- level: TEXT -> varchar(16), 设置 DEFAULT
ALTER TABLE bridge_execution_logs ALTER COLUMN level TYPE varchar(16);
ALTER TABLE bridge_execution_logs ALTER COLUMN level SET DEFAULT 'INFO';

-- event: TEXT -> varchar(64)
ALTER TABLE bridge_execution_logs ALTER COLUMN event TYPE varchar(64);

-- ===================================================================
-- 2. 索引对齐（desired_schema 命名规范）
-- ===================================================================

-- 删除旧索引（migration 005 创建的不规范命名）
DROP INDEX IF EXISTS idx_bridge_logs_case_id;
DROP INDEX IF EXISTS idx_bridge_logs_trace_id;
DROP INDEX IF EXISTS idx_bridge_logs_custom_ui;
DROP INDEX IF EXISTS idx_bridge_logs_created_at;
DROP INDEX IF EXISTS idx_bridge_logs_event;

-- 创建新索引（对齐 desired_schema 命名）
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_case_time
    ON bridge_execution_logs (case_id, created_at);

CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_trace
    ON bridge_execution_logs (trace_id);

CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_custom_ui
    ON bridge_execution_logs (custom_ui);

-- ===================================================================
-- 验证脚本（可选）
-- ===================================================================
-- 执行后可运行以下 SQL 验证：
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_name = 'bridge_execution_logs'
-- ORDER BY ordinal_position;