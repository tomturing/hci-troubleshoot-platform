-- ===================================================================
-- Migration 007: 扩展 bridge_execution_logs 表结构
-- 工单: Q2026072160299 - terminal_bridge 日志回采完整能力补齐
-- 日期: 2026-07-21
-- ===================================================================

-- 说明：
-- 1. 添加命令执行的关键字段，支持完整的执行日志记录
-- 2. 包含：exec_id、command、exit_code、duration_ms、stdout_len、stderr_len、output_preview、success、error_type
-- 3. 本 migration 幂等，可安全重复执行

-- ===================================================================
-- 1. 添加字段
-- ===================================================================

-- exec_id: 命令执行 ID（用于去重和关联 exec.start/exec.done）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS exec_id varchar(64);

-- command: 执行的命令（完整命令）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS command TEXT;

-- exit_code: 命令退出码（0=成功，非0=失败，-1=异常）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS exit_code INTEGER;

-- duration_ms: 命令执行耗时（毫秒）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS duration_ms BIGINT;

-- stdout_len: 标准输出长度（字节）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS stdout_len INTEGER;

-- stderr_len: 标准错误长度（字节）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS stderr_len INTEGER;

-- output_preview: 输出预览（截断到 500 字符）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS output_preview TEXT;

-- success: 是否成功（exit_code=0 为成功）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS success BOOLEAN;

-- error_type: 错误类型分类（session_creation_failed / command_start_failed / timeout 等）
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS error_type VARCHAR(50);

-- ===================================================================
-- 2. 创建索引
-- ===================================================================

-- 按 exec_id 查询（用于去重和关联）
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_exec_id ON bridge_execution_logs(exec_id);

-- 按成功/失败查询（用于统计成功率）
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_success ON bridge_execution_logs(success) WHERE success IS NOT NULL;

-- 按错误类型查询（用于错误分析）
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_error_type ON bridge_execution_logs(error_type) WHERE error_type IS NOT NULL;

-- 按执行耗时查询（用于性能分析）
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_duration ON bridge_execution_logs(duration_ms) WHERE duration_ms IS NOT NULL;

-- ===================================================================
-- 验证脚本（可选）
-- ===================================================================
-- 执行后可运行以下 SQL 验证：
-- SELECT column_name, data_type, is_nullable, column_default
-- FROM information_schema.columns
-- WHERE table_name = 'bridge_execution_logs'
-- ORDER BY ordinal_position;