-- ============================================================================
-- 047_retire_root_signal_prompt_v1.sql
-- 描述：物理删除根因信号抽取 Prompt v1（kbd_extract_root_signal_v1）遗留死行。
-- 问题：根因信号抽取 v2 重设计（PR #1147，多信号数组 + 整篇输入 + 溯源分层）把
--       种子模板 kbd_extract_root_signal_v1 改名为 kbd_extract_root_signal_v2，
--       而 database/seeds/02_system_prompts.sql 用 INSERT ... ON CONFLICT (name)
--       DO NOTHING（只增不删），故早期部署写入的 v1 行残留在存量 live 库中，
--       且 is_active 仍为 TRUE，在 admin-ui Prompt 管理页显示为「正在使用」。
--       运行时抽取按名精确加载 kbd_extract_root_signal_v2
--       （backend/kb-service/app/services/root_signal_extractor.py:52
--        ROOT_SIGNAL_PROMPT_NAME，无 prompt_slot 重定向），永不读取 v1；
--       v1 属零引用的死配置，仅造成 UI 混淆与「误编辑 v1 以为能改行为」的人为风险。
-- 影响：仅删除 name='kbd_extract_root_signal_v1' 一行；v2 及业务功能零影响。
--       全新/CI 环境种子本就不含 v1（种子已只写 v2），DELETE 命中 0 行、无副作用。
-- 幂等性：DELETE 天然可重复执行（行不存在时 0 rows affected，不报错）。
-- 唯一调用链：migration:20261016000001
-- ============================================================================

DO $$
DECLARE
    v_deleted integer;
BEGIN
    RAISE NOTICE '开始执行 047 数据迁移：删除根因信号抽取 Prompt v1 死行...';

    DELETE FROM system_prompt
    WHERE name = 'kbd_extract_root_signal_v1';
    GET DIAGNOSTICS v_deleted = ROW_COUNT;

    IF v_deleted > 0 THEN
        RAISE NOTICE '已删除 kbd_extract_root_signal_v1 死行 % 行（trace_id=migration:20261016000001）', v_deleted;
    ELSE
        RAISE NOTICE '未找到 kbd_extract_root_signal_v1（全新/CI 环境种子不含该行），无需清理（trace_id=migration:20261016000001）';
    END IF;

    -- fail-loud：确保在用版本 v2 未被误伤（本迁移只应删 v1）。
    IF EXISTS (
        SELECT 1 FROM system_prompt WHERE name = 'kbd_extract_root_signal_v1'
    ) THEN
        RAISE EXCEPTION 'migration:20261016000001 未生效：kbd_extract_root_signal_v1 仍存在，删除未成功';
    END IF;

    RAISE NOTICE '047 数据迁移完成：v1 死行已清理，v2 不受影响';
END $$;
