-- ===========================================================================
-- Migration: 011_cleanup_kbd_extract_signals_v1_prompt.sql
-- 说明: 清理关键信号分级抽取 Prompt 的历史残留，恢复「每个 stage 恰有一个激活版本」不变量。
--       背景: v2 Prompt (kbd_extract_signals_v2) 已上线并由 kb-service 的
--             extract_signals.py(_EXTRACT_PROMPT_NAME) 硬编码消费。历史 v1 行
--             (kbd_extract_signals_v1) 因早期 atlas-migrations «delete» 在部分环境
--             未生效 / 种子用 ON CONFLICT(name) DO NOTHING 留下重复行，导致 KEY 阶段
--             出现多条 is_active=true 的 v1，前端互斥开关被脏数据锁死、且
--             prompt_loader.scalar_one_or_none() 在回退到 v1 时会抛 MultipleResultsFound。
-- 处理:
--   1) 删除所有残留的 kbd_extract_signals_v1 行（v2 已接管，v1 不再被任何代码读取）。
--   2) 确保 KEY 阶段仅 kbd_extract_signals_v2 为 is_active=true，其余 KEY 行置 false，
--      恢复「每 stage 仅一个激活版本」约束。
-- 幂等: 重复执行无副作用。
-- 范围: system_prompt 表。
-- ===========================================================================

DO $$
DECLARE
    v1_count   integer := 0;
    key_active_count integer := 0;
    has_v2     boolean := false;
BEGIN
    -- 1) 统计并删除残留 v1 行
    SELECT count(*) INTO v1_count FROM system_prompt WHERE name = 'kbd_extract_signals_v1';
    IF v1_count > 0 THEN
        DELETE FROM system_prompt WHERE name = 'kbd_extract_signals_v1';
        RAISE NOTICE 'OK 011: 已删除 % 条残留 kbd_extract_signals_v1 行', v1_count;
    ELSE
        RAISE NOTICE 'OK 011: 无残留 kbd_extract_signals_v1 行，跳过删除';
    END IF;

    -- 2) 恢复 KEY 阶段「恰一个激活版本」不变量
    SELECT EXISTS (
        SELECT 1 FROM system_prompt WHERE name = 'kbd_extract_signals_v2'
    ) INTO has_v2;

    SELECT count(*) INTO key_active_count
      FROM system_prompt
     WHERE stage = 'KEY' AND is_active IS TRUE;

    IF has_v2 THEN
        -- 仅保留 v2 激活，关闭 KEY 阶段其他所有行
        UPDATE system_prompt
           SET is_active = (name = 'kbd_extract_signals_v2')
         WHERE stage = 'KEY';
        RAISE NOTICE 'OK 011: KEY 阶段已重置，仅 kbd_extract_signals_v2 激活';
    ELSIF key_active_count = 0 THEN
        -- v2 缺失且当前无激活行：激活 KEY 阶段最新一条，避免「零激活」破坏契约
        UPDATE system_prompt
           SET is_active = TRUE
         WHERE id = (
             SELECT id FROM system_prompt
             WHERE stage = 'KEY'
             ORDER BY updated_at DESC NULLS LAST, id DESC
             LIMIT 1
         );
        RAISE NOTICE 'WARN 011: 未找到 kbd_extract_signals_v2，已激活 KEY 阶段最新一条以免零激活';
    ELSE
        RAISE NOTICE 'OK 011: KEY 阶段已存在 % 个激活版本，且 v2 缺失，保持现状', key_active_count;
    END IF;
END $$;
