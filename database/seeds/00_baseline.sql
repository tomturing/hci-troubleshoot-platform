-- ===========================================================================
-- database/seeds/00_baseline.sql — 存量环境历史迁移引导脚本
-- ===========================================================================
-- 用途：对已有数据库（dev/staging/prod）的一次性引导操作
--       作用是告诉 dbmate "这些文件已经执行过了，不要重复跑"
--
-- 执行时机：仅在首次接入 dbmate 时执行一次
--
-- 执行方法：
--   psql "$DATABASE_URL" -f database/seeds/00_baseline.sql
--
-- 确认方法：
--   psql "$DATABASE_URL" -c "SELECT * FROM schema_migrations ORDER BY version;"
-- ===========================================================================

-- Step 1: 创建 dbmate 的版本记录表（如果尚不存在）
-- 注意：dbmate 原生只有 version 列，此处仅用于兼容 baseline 脚本的 IF NOT EXISTS
CREATE TABLE IF NOT EXISTS schema_migrations (
    version  VARCHAR(255) NOT NULL PRIMARY KEY
);

-- Step 2: 将历史迁移文件标记为"已执行"
-- 每行对应 database/migrations/ 目录下的一个文件
-- version = dbmate 从文件名提取的纯数字前缀（\d+ 正则，遇到非数字字符截止）
-- 文件格式 20260305001_init_schema.sql -> version = "20260305001"
-- 按实际情况调整：如果某个环境真的没有执行某个 migration，就删掉对应的行
--
-- 注意：20260402001_sop_tables.sql 已删除（重复 version），
--       sop_document/sop_chunk 由 20260408001_sop_tables_fix_version.sql 创建。
INSERT INTO schema_migrations (version) VALUES
    ('20260305001'),    -- 20260305001_init_schema.sql
    ('20260312001'),    -- 20260312001_kb_rag_v3.sql
    ('20260312002'),    -- 20260312002_evaluation.sql
    ('20260326001'),    -- 20260326001_p4_state_machine.sql
    ('20260326002'),    -- 20260326002_conversation_p4.sql
    ('20260326003'),    -- 20260326003_tool_audit_log.sql
    ('20260401001'),    -- 20260401001_kbd_pipeline.sql
    ('20260401002'),    -- 20260401002_gap_fill_kb_tables.sql
    ('20260402001'),    -- 20260402001_kb_category_sop_hit_tracking.sql
    ('20260402002'),    -- 20260402002_migrate_audit_data.sql
    ('20260402003'),    -- 20260402003_drop_deprecated_tables.sql
    ('20260404001'),    -- 20260404001_v6_redesign.sql
    ('20260407001'),    -- 20260407001_schema_repair.sql
    ('20260407002'),    -- 20260407002_cleanup_deprecated_tables.sql
    ('20260407003')     -- 20260407003_fix_migration_chain.sql
ON CONFLICT (version) DO NOTHING;

-- Step 3: 验证结果
SELECT version FROM schema_migrations ORDER BY version;

-- ===========================================================================
-- 执行后，dbmate up 仅执行 20260408001 及之后的新迁移文件
-- ===========================================================================
