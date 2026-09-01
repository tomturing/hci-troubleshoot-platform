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
            e.working_snapshot_digest,
            1,
            CASE WHEN e.status = 'published' THEN 'published' ELSE 'draft_editing' END,
            COALESCE(e.trace_id, 'backfill-init-trace'),
            e.created_at,
            e.updated_at
        FROM kbd_entry e
        WHERE e.support_id IS NOT NULL AND e.support_id != ''
        ON CONFLICT (support_id) DO UPDATE SET
            status = EXCLUDED.status,
            working_snapshot_digest = COALESCE(EXCLUDED.working_snapshot_digest, kbd_package.working_snapshot_digest),
            updated_at = EXCLUDED.updated_at;

        RAISE NOTICE 'kbd_package 存量数据回填完成';
    END IF;

    -- 2. 回填动态资源使用审计到统一 audit_log
    IF to_regclass('public.dynamic_resource_usage_audit') IS NOT NULL AND to_regclass('public.audit_log') IS NOT NULL THEN
        INSERT INTO audit_log (
            event_type,
            actor_type,
            actor_id,
            resource_type,
            resource_id,
            payload_json,
            trace_id,
            created_at
        )
        SELECT 
            'dynamic_resource_loaded',
            'agent_engine',
            COALESCE(consumer, 'agent-service'),
            'dynamic_resource',
            resource_name,
            jsonb_build_object(
                'revision', revision,
                'status', status,
                'input_hash', input_hash,
                'output_hash', output_hash,
                'package_snapshot_digest', package_snapshot_digest,
                'bundle_digest', bundle_digest
            ),
            COALESCE(trace_id, 'audit-backfill-trace'),
            created_at
        FROM dynamic_resource_usage_audit
        WHERE NOT EXISTS (
            SELECT 1 FROM audit_log a 
            WHERE a.trace_id = dynamic_resource_usage_audit.trace_id 
              AND a.event_type = 'dynamic_resource_loaded'
        );

        RAISE NOTICE 'dynamic_resource_usage_audit 存量审计日志回填至 audit_log 完成';
    END IF;

    RAISE NOTICE '阶段 1 数据回填执行完毕';
END $$;
