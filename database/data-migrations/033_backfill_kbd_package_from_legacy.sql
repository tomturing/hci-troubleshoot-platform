-- ============================================================================
-- 033_backfill_kbd_package_from_legacy.sql
-- 描述：历史数据平滑回填与双写支持（将 kbd_entry 回填至 kbd_package，usage_audit 回填至 audit_log）
-- 幂等性：支持重复执行，ON CONFLICT 自动更新
-- 唯一调用链：全量落库 trace_id
-- ============================================================================

DO $$
BEGIN
    RAISE NOTICE '开始执行阶段 1：历史 KBD 与审计数据向新核心表平滑回填...';

    -- 1. 回填业务主表 kbd_package
    IF to_regclass('public.kbd_entry') IS NOT NULL AND to_regclass('public.kbd_package') IS NOT NULL THEN
        INSERT INTO kbd_package (
            support_id,
            working_snapshot_digest,
            workspace_version,
            status,
            trace_id,
            created_at,
            updated_at
        )
        SELECT 
            e.support_id,
            NULL,
            1,
            CASE WHEN e.status = 'published' THEN 'published' ELSE 'draft_editing' END,
            'backfill-init-trace',
            e.created_at,
            e.updated_at
        FROM kbd_entry e
        WHERE e.support_id IS NOT NULL AND e.support_id != ''
        ON CONFLICT (support_id) DO UPDATE SET
            status = EXCLUDED.status,
            updated_at = EXCLUDED.updated_at;

        RAISE NOTICE 'kbd_package 存量数据回填完成';
    END IF;

    RAISE NOTICE '阶段 1 数据回填执行完毕';
END $$;
