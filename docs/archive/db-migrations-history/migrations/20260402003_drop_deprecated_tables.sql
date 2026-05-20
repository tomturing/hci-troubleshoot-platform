-- migrate:up

-- =============================================================================
-- 废弃表清理迁移
-- 创建日期：2026-04-02
--
-- 背景：
--   根据 docs/architecture/20_数据库重规划分析.md，以下表已废弃：
--   - kb_document, kb_chunk：被 kbd_entry 替代（方案B：KBD 整条存储，不分块）
--   - kb_sop_node：被 sop_document/sop_chunk 替代（关键字路由架构已废弃）
--   - kb_synonym：已备份并合并到 kb_category.metadata
--   - raw_cases：数据不可用，已被 data-pipeline/kbd/ 新流水线替代
--   - knowledge_atoms：方案B 中不是核心表
--   - prompt_audit：已迁移到 audit_log
--   - tool_audit_log：已迁移到 audit_log
--
-- 前提：
--   - 执行 20260402002_migrate_audit_data.sql 完成数据迁移
--   - 确认无业务依赖这些表
-- =============================================================================

-- ─── 1. 删除知识库废弃表 ─────────────────────────────────────────────────────────

-- kb_chunk 依赖 kb_document，先删除 kb_chunk
DROP TABLE IF EXISTS kb_chunk CASCADE;
RAISE NOTICE '已删除 kb_chunk 表';

DROP TABLE IF EXISTS kb_document CASCADE;
RAISE NOTICE '已删除 kb_document 表';

DROP TABLE IF EXISTS kb_sop_node CASCADE;
RAISE NOTICE '已删除 kb_sop_node 表';

DROP TABLE IF EXISTS kb_synonym CASCADE;
RAISE NOTICE '已删除 kb_synonym 表（数据已备份到 _kb_synonym_backup）';

-- ─── 2. 删除历史遗留表 ─────────────────────────────────────────────────────────

DROP TABLE IF EXISTS raw_cases CASCADE;
RAISE NOTICE '已删除 raw_cases 表';

DROP TABLE IF EXISTS knowledge_atoms CASCADE;
RAISE NOTICE '已删除 knowledge_atoms 表';

-- ─── 3. 删除已迁移的审计表 ─────────────────────────────────────────────────────

DROP TABLE IF EXISTS prompt_audit CASCADE;
RAISE NOTICE '已删除 prompt_audit 表（数据已迁移到 audit_log）';

DROP TABLE IF EXISTS tool_audit_log CASCADE;
RAISE NOTICE '已删除 tool_audit_log 表（数据已迁移到 audit_log）';

-- ─── 4. 清理备份表 ─────────────────────────────────────────────────────────────

DROP TABLE IF EXISTS _kb_synonym_backup CASCADE;
RAISE NOTICE '已删除 _kb_synonym_backup 备份表';

-- ─── 5. 验证最终表结构 ─────────────────────────────────────────────────────────

DO $$
DECLARE
    remaining_tables TEXT[];
    expected_tables TEXT[] := ARRAY[
        'user', 'case', 'environment',
        'conversation', 'message', 'audit_log',
        'assistant_evaluation',
        'kb_category', 'kbd_entry', 'sop_document', 'sop_chunk'
    ];
    missing_tables TEXT[];
    extra_tables TEXT[];
    t TEXT;
BEGIN
    -- 获取当前所有用户表
    SELECT array_agg(tablename)
    INTO remaining_tables
    FROM pg_tables
    WHERE schemaname = 'public';

    -- 检查缺失的表
    FOREACH t IN ARRAY expected_tables LOOP
        IF NOT t = ANY(remaining_tables) THEN
            missing_tables := array_append(missing_tables, t);
        END IF;
    END LOOP;

    -- 报告结果
    RAISE NOTICE '=== 表清理验证 ===';
    RAISE NOTICE '当前表数量：%', array_length(remaining_tables, 1);
    RAISE NOTICE '期望表：%', array_length(expected_tables, 1);

    IF array_length(missing_tables, 1) > 0 THEN
        RAISE WARNING '缺失表：%', missing_tables;
    ELSE
        RAISE NOTICE '所有目标表已存在';
    END IF;
END
$$;

-- 列出最终保留的表
SELECT tablename AS "保留的表"
FROM pg_tables
WHERE schemaname = 'public'
ORDER BY tablename;

-- migrate:down

-- 无法自动回滚，需要从备份恢复或重新运行迁移

RAISE NOTICE '无法自动回滚表删除操作，请从备份恢复';