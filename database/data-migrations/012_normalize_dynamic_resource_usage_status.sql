-- ===========================================================================
-- Migration: 012_normalize_dynamic_resource_usage_status.sql
-- 说明: 将 KBD 初召阶段历史误记的 matched 统一为 retrieved。
-- 原因: matched 表示已经验证命中，但 KBD 搜索端点返回的只是待差异诊断候选。
-- 幂等: 重复执行无副作用。
-- ===========================================================================

DO $$
DECLARE
    unknown_statuses text;
BEGIN
    -- 全新数据库在 data migration 阶段尚未由 Atlas 创建业务表，应直接跳过。
    IF to_regclass('public.dynamic_resource_usage_audit') IS NULL THEN
        RETURN;
    END IF;

    UPDATE dynamic_resource_usage_audit
    SET status = 'retrieved'
    WHERE status = 'matched';

    SELECT string_agg(DISTINCT status, ', ' ORDER BY status)
    INTO unknown_statuses
    FROM dynamic_resource_usage_audit
    WHERE status NOT IN ('retrieved', 'success', 'failed');

    IF unknown_statuses IS NOT NULL THEN
        RAISE EXCEPTION 'dynamic_resource_usage_audit 存在未知状态，无法添加约束: %', unknown_statuses;
    END IF;
END $$;
