-- 061：清理僵尸 delivery_status=queued（采集已终态但交付仍排队）
--
-- 背景（2026-11-12 现场）：
--   管理端面板「传输中」虚高 53 个，其中 42 个是僵尸：采集早已终态
--   （23 failed + 12 completed + 7 cancelled），但 delivery_status 一直停在 queued。
--   原因是 bridge 侧任务结束后未清理 delivery 状态，queued 永远不会被消费。
--   旧统计口径 _TRANSFERRING 用 OR 条件把这些僵尸算进了「传输中」。
--
-- 第一性原理：
--   delivery_status=queued 的语义是"已签发上传会话，等 bridge 执行"。
--   如果采集已经 failed/cancelled，bridge 不会执行这个会话（节点侧已无任务）。
--   如果采集已经 completed，说明全链路已完成；delivery 还在 queued 说明
--   交付从未真正启动，也是 failed。
--   因此，采集终态 + delivery=queued = 交付失败（僵尸会话）。
--
-- 安全措施：
--   1. 先备份到 log_collect_task_correction_20261112_stats（含完整 jsonb）
--   2. 只改 delivery_status、delivery_error、delivery_finished_at 和 detail 审计
--   3. 不动 status / progress / finished_at / archive_* 等采集侧字段
--   4. 幂等：只处理 delivery_status='queued' 且 status 已终态的行
--
-- 唯一调用链标记：log-collect-zombie-cleanup-20261112

BEGIN;

-- Step 1: 备份（只在表不存在时创建，幂等）
DO $backup$
BEGIN
    IF to_regclass('public.log_collect_task_correction_20261112_stats') IS NULL THEN
        EXECUTE $$
            CREATE TABLE log_collect_task_correction_20261112_stats AS
            SELECT task_id, case_id, status, delivery_status, delivery_error,
                   delivery_finished_at, delivery_attempts,
                   detail AS detail_snapshot,
                   created_at, finished_at,
                   'zombie_delivery_queued' AS correction_type,
                   NOW() AS correction_at
            FROM log_collect_task
            WHERE delivery_status = 'queued'
              AND status IN ('failed', 'cancelled', 'completed')
        $$;
        RAISE NOTICE '备份完成：%', (SELECT count(*) FROM log_collect_task_correction_20261112_stats);
    ELSE
        RAISE NOTICE '备份表已存在，跳过创建';
    END IF;
END $backup$;

-- Step 2: 清理僵尸 delivery_status=queued
-- 采集已 failed/cancelled → 交付也 failed（会话从未被执行）
-- 采集已 completed 但 delivery 还在 queued → 交付 failed（已完成但交付从未启动）
UPDATE log_collect_task
SET delivery_status = 'failed',
    delivery_error = COALESCE(delivery_error, '')
        || CASE WHEN delivery_error IS NULL OR delivery_error = '' THEN '' ELSE '；' END
        || '采集已终态（' || status || '），交付会话仍排队中，判定为僵尸会话已清理（061）',
    delivery_finished_at = COALESCE(delivery_finished_at, NOW()),
    detail = jsonb_set(
        detail,
        '{data_correction}',
        jsonb_build_object(
            'type', 'zombie_delivery_cleanup',
            'migration', '061',
            'trace', 'log-collect-zombie-cleanup-20261112',
            'corrected_at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'before', jsonb_build_object('delivery_status', 'queued'),
            'after', jsonb_build_object('delivery_status', 'failed'),
            'reason', '采集已终态但交付仍排队，僵尸会话清理'
        )
    )
WHERE delivery_status = 'queued'
  AND status IN ('failed', 'cancelled', 'completed');

-- 验证：确认没有遗留的僵尸
DO $verify$
    DECLARE
        remaining int;
BEGIN
    SELECT count(*) INTO remaining
    FROM log_collect_task
    WHERE delivery_status = 'queued'
      AND status IN ('failed', 'cancelled', 'completed');

    IF remaining > 0 THEN
        RAISE WARNING '仍有 % 个僵尸未清理', remaining;
    ELSE
        RAISE NOTICE '僵尸清理完成：0 个遗留';
    END IF;
END $verify$;

COMMIT;
