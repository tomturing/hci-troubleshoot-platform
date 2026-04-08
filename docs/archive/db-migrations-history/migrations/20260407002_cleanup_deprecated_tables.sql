-- migrate:up

-- =============================================================================
-- 废弃表再次清理迁移
-- 创建日期：2026-04-07
--
-- 背景：
--   PR #108 的 schema_repair.sql 重新创建了已在 20260402003_drop_deprecated_tables.sql
--   中删除的废弃表。这是 Copilot 创建迁移时直接复制设计文档中的 CREATE TABLE 语句，
--   没有排除废弃表清单中的表。
--
-- 需删除的表（7 张）：
--   - kb_document, kb_chunk：被 kbd_entry 替代（方案B）
--   - kb_sop_node, kb_synonym：被 sop_document/sop_chunk 替代
--   - raw_cases, knowledge_atoms：数据管道已废弃
--   - prompt_audit：已合并入 audit_log
-- =============================================================================

-- ─── 1. 删除知识库废弃表（kb_chunk 依赖 kb_document，先删 kb_chunk）───

DROP TABLE IF EXISTS kb_chunk CASCADE;
RAISE NOTICE '已删除 kb_chunk 表（再次清理）';

DROP TABLE IF EXISTS kb_document CASCADE;
RAISE NOTICE '已删除 kb_document 表（再次清理）';

DROP TABLE IF EXISTS kb_sop_node CASCADE;
RAISE NOTICE '已删除 kb_sop_node 表（再次清理）';

DROP TABLE IF EXISTS kb_synonym CASCADE;
RAISE NOTICE '已删除 kb_synonym 表（再次清理）';

-- ─── 2. 删除数据管道废弃表 ───────────────────────────────────────────────

DROP TABLE IF EXISTS raw_cases CASCADE;
RAISE NOTICE '已删除 raw_cases 表（再次清理）';

DROP TABLE IF EXISTS knowledge_atoms CASCADE;
RAISE NOTICE '已删除 knowledge_atoms 表（再次清理）';

-- ─── 3. 删除已合并的审计表 ───────────────────────────────────────────────

DROP TABLE IF EXISTS prompt_audit CASCADE;
RAISE NOTICE '已删除 prompt_audit 表（再次清理）';

-- ─── 4. 验证最终表结构 ───────────────────────────────────────────────────

DO $$
DECLARE
    remaining_tables TEXT[];
    expected_tables TEXT[] := ARRAY[
        'customer', 'user', 'case', 'environment', 'assistant_evaluation',
        'session', 'conversation', 'message', 'audit_log',
        'diagnostic_item', 'tool_result', 'system_prompt', 'tool_definition',
        'kb_category', 'kbd_entry', 'sop_document', 'sop_chunk'
    ];
    deprecated_tables TEXT[] := ARRAY[
        'kb_document', 'kb_chunk', 'kb_sop_node', 'kb_synonym',
        'raw_cases', 'knowledge_atoms', 'prompt_audit', 'tool_audit_log'
    ];
    found_deprecated TEXT[];
    missing_expected TEXT[];
    t TEXT;
BEGIN
    -- 获取当前所有用户表
    SELECT array_agg(tablename)
    INTO remaining_tables
    FROM pg_tables
    WHERE schemaname = 'public';

    -- 检查废弃表是否还存在
    FOREACH t IN ARRAY deprecated_tables LOOP
        IF t = ANY(remaining_tables) THEN
            found_deprecated := array_append(found_deprecated, t);
        END IF;
    END LOOP;

    -- 检查期望表是否缺失
    FOREACH t IN ARRAY expected_tables LOOP
        IF NOT t = ANY(remaining_tables) THEN
            missing_expected := array_append(missing_expected, t);
        END IF;
    END LOOP;

    -- 报告结果
    RAISE NOTICE '=== 表清理验证 ===';
    RAISE NOTICE '当前表数量：%', array_length(remaining_tables, 1);
    RAISE NOTICE '期望表数量：17';

    IF array_length(found_deprecated, 1) > 0 THEN
        RAISE WARNING '废弃表仍存在：%', found_deprecated;
    ELSE
        RAISE NOTICE '所有废弃表已清理';
    END IF;

    IF array_length(missing_expected, 1) > 0 THEN
        RAISE WARNING '缺失期望表：%', missing_expected;
    ELSE
        RAISE NOTICE '所有期望表已存在';
    END IF;
END
$$;

-- 列出最终保留的表
SELECT tablename AS "保留的表"
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;

-- migrate:down

-- 无法自动回滚，需要从备份恢复

RAISE NOTICE '无法自动回滚表删除操作，请从备份恢复';