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
CREATE TABLE IF NOT EXISTS schema_migrations (
    version  VARCHAR(255) NOT NULL PRIMARY KEY,
    ts       TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
);

-- Step 2: 将历史迁移文件标记为"已执行"
-- 每行对应 database/migrations/ 目录下的一个文件（version = 文件名去掉 .sql 后缀）
-- 按实际情况调整：如果某个环境真的没有执行某个 migration，就删掉对应的行
INSERT INTO schema_migrations (version) VALUES
    ('20260305_001_init_schema'),           -- 20260305_001_init_schema.sql
    ('20260312_001_kb_rag_v3'),             -- 20260312_001_kb_rag_v3.sql
    ('20260312_002_evaluation'),            -- 20260312_002_evaluation.sql
    ('20260326_001_p4_state_machine'),      -- 20260326_001_p4_state_machine.sql
    ('20260326_002_conversation_p4'),       -- 20260326_002_conversation_p4.sql
    ('20260326_003_tool_audit_log'),        -- 20260326_003_tool_audit_log.sql
    ('20260401_001_kbd_pipeline')           -- 20260401_001_kbd_pipeline.sql（如环境未执行，删除此行）
ON CONFLICT (version) DO NOTHING;

-- Step 3: 验证结果
SELECT version, ts FROM schema_migrations ORDER BY version;

-- ===========================================================================
-- 执行后，dbmate up 会从下一个迁移文件开始（如 20260415_001_xxx.sql）
-- ===========================================================================
