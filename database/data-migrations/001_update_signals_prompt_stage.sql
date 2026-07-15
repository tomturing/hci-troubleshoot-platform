-- ===========================================================================
-- Migration: 001_update_signals_prompt_stage.sql
-- 说明: 将关键信号抽取 Prompt (kbd_extract_signals_v1) 的 stage 由 KBD 调整至 KEY，
--       使其归属到独立的「KEY 关键信号分级抽取」阶段（admin-ui Prompt 管理页面）。
-- 背景: PR #555 已将种子文件中的 stage 改为 KEY，但历史部署的数据库中该记录的 stage
--       仍为 KBD（种子使用 ON CONFLICT DO NOTHING 不覆盖已有记录）。
-- 幂等: 仅当 name 匹配且当前 stage 仍为 KBD 时才更新，可重复执行。
-- 参考: docs/solution/database/data-migration-design.md
-- ===========================================================================

UPDATE system_prompt
SET stage = 'KEY'
WHERE name = 'kbd_extract_signals_v1'
  AND stage = 'KBD';

-- 验证更新结果（可选，用于日志确认）
DO $$
DECLARE
    actual_stage VARCHAR(10);
BEGIN
    SELECT stage INTO actual_stage
    FROM system_prompt
    WHERE name = 'kbd_extract_signals_v1';
    
    IF actual_stage = 'KEY' THEN
        RAISE NOTICE '✅ kbd_extract_signals_v1 stage 已确认为 KEY';
    ELSE
        RAISE NOTICE '⚠️ kbd_extract_signals_v1 stage 为 % (预期 KEY)', actual_stage;
    END IF;
END $$;