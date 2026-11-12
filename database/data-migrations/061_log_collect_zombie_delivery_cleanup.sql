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
-- 备份策略（为什么不建备份表）：
--   历史做法是 CREATE TABLE 快照，但项目用 Atlas 声明式 schema 做门禁与部署，
--   多出来的表会让 diff 门禁失败；更危险的是 db-migrate.sh 的 atlas schema apply
--   会把这类"非期望 schema"的表 DROP 掉——备份反而最先被销毁。
--   因此改为**行内快照**：被改行的原值完整写进 detail.data_correction.before，
--   回滚时可直接从 detail 反推 UPDATE，取证信息与数据同生共死，也不污染 schema。
--
-- 安全措施：
--   1. 只改 delivery_status、delivery_error、delivery_finished_at 和 detail 审计；
--   2. 不动 status / progress / finished_at / archive_* 等采集侧字段；
--   3. 幂等：只处理 delivery_status='queued' 且 status 已终态的行，重跑影响 0 行。
--
-- 唯一调用链标记：log-collect-zombie-cleanup-20261112

BEGIN;

UPDATE log_collect_task AS t
SET delivery_status = 'failed',
    delivery_error = COALESCE(NULLIF(t.delivery_error, ''), '')
        || CASE WHEN COALESCE(t.delivery_error, '') = '' THEN '' ELSE '；' END
        || '采集已终态（' || t.status || '），交付会话仍排队中，判定为僵尸会话已清理（061）',
    delivery_finished_at = COALESCE(t.delivery_finished_at, NOW()),
    detail = jsonb_set(
        COALESCE(t.detail, '{}'::jsonb),
        '{data_correction}',
        jsonb_build_object(
            'type', 'zombie_delivery_cleanup',
            'migration', '061',
            'trace', 'log-collect-zombie-cleanup-20261112',
            'corrected_at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            -- 行内备份：原值全量留存，可用 detail 反推回滚
            'before', jsonb_build_object(
                'status', t.status,
                'delivery_status', t.delivery_status,
                'delivery_error', t.delivery_error,
                'delivery_finished_at', t.delivery_finished_at,
                'delivery_attempts', t.delivery_attempts
            ),
            'after', jsonb_build_object(
                'delivery_status', 'failed',
                'delivery_finished_at', to_char(COALESCE(t.delivery_finished_at, NOW()) AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"')
            ),
            'reason', '采集已终态但交付仍排队，僵尸会话清理'
        )
    ),
    updated_at = NOW()
WHERE t.delivery_status = 'queued'
  AND t.status IN ('failed', 'cancelled', 'completed');

-- 验证不变量：采集终态 + 交付 queued = 0
DO $verify$
DECLARE
    remaining int;
BEGIN
    SELECT count(*) INTO remaining
    FROM log_collect_task
    WHERE delivery_status = 'queued'
      AND status IN ('failed', 'cancelled', 'completed');

    IF remaining > 0 THEN
        RAISE EXCEPTION '僵尸清理失败：仍有 % 个采集终态但交付仍排队的任务', remaining;
    END IF;
    RAISE NOTICE '僵尸清理完成：采集终态 + delivery=queued 遗留 0 条';
END $verify$;

COMMIT;