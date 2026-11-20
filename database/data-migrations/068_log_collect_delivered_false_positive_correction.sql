-- 068：订正「5888 已交付但平台仍写 failed」的 4 条任务
--
-- 事实依据（2026-11-20 全量对账）：
--   直连 192.168.0.2:5888/archive_status 核对 101 个失败工单号，以下 4 条同时满足
--   delivery_status='delivered' 与权威落盘证据：
--     · 三条 P1：detail.p1_archive_check.verified=true（平台按产物名回查归档件）
--     · 一条 P2：relay_status=success + integrity_state=verified + 服务端摘要一致
--   节点后来写回的 status=failed 是迟到状态噪音，不代表包没到。
--
-- 为什么不直接改状态机：
--   failed→completed 在平台状态机里是禁止迁移；本迁移是带审计的一次性数据订正，
--   原值完整写进 detail.data_correction（行内快照），不建备份表，避免 Atlas
--   声明式 schema 把备份表 DROP 掉（见 061 注释）。
--
-- 反伪证据（对抗性审查）：
--   只按 task_id + 证据条件双重 WHERE，不按工单号批量翻正。
--   手工浏览器上传（如 Q2026111900236）、历史包、体积不足的包均不在名单。
--
-- 唯一调用链标记：lc-correction-Q20261120-delivered-false-positive

BEGIN;

WITH targets AS (
    SELECT t.task_id
    FROM log_collect_task AS t
    WHERE t.task_id IN (
        'a02c9080-cd6a-4a68-ac96-163c3b569b9a',  -- Q2026110302155
        '8bbe5e08-0abe-4cc4-b467-c00bf28ea9d5',  -- Q2026110601054
        'cab9f45d-1caf-42e0-b981-8ef4befe5999',  -- Q2026111199999
        'b7e0c501-66f4-430a-968d-5ebdad7fb07d'   -- Q2026111200678
    )
      AND t.status = 'failed'
      AND t.delivery_status = 'delivered'
      AND (
          t.detail #>> '{p1_archive_check,verified}' = 'true'
          OR (
              t.relay_status = 'success'
              AND t.integrity_state = 'verified'
              AND t.archive_sha256 IS NOT NULL
              AND t.archive_sha256 = t.object_sha256
          )
      )
)
UPDATE log_collect_task AS t
SET status = 'completed',
    progress = 100,
    finished_at = COALESCE(t.finished_at, t.delivery_finished_at),
    error_code = NULL,
    error_message = NULL,
    -- PostgreSQL 的 jsonb_set 不会一次创建缺失的中间层级，先补 data_correction 对象。
    detail = jsonb_set(
        jsonb_set(
            COALESCE(t.detail, '{}'::jsonb),
            '{data_correction}',
            COALESCE(t.detail -> 'data_correction', '{}'::jsonb),
            true
        ),
        '{data_correction,delivered_false_positive_20261120}',
        jsonb_build_object(
            'type', 'delivered_false_positive_correction',
            'migration', '068',
            'trace', 'lc-correction-Q20261120-delivered-false-positive',
            'corrected_at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'before', jsonb_build_object(
                'status', t.status,
                'progress', t.progress,
                'finished_at', t.finished_at,
                'error_code', t.error_code,
                'error_message', t.error_message
            ),
            'after', jsonb_build_object(
                'status', 'completed',
                'progress', 100,
                'finished_at', COALESCE(t.finished_at, t.delivery_finished_at)
            ),
            'reason', '5888 归档核验通过且交付已登记，节点迟到的 failed 不覆盖交付事实'
        ),
        true
    ),
    updated_at = NOW()
FROM targets
WHERE t.task_id = targets.task_id;

-- 验证不变量：
--   · 所有「待订正」行都已订正（空库/CI 模拟库允许 0 行）；
--   · 名单内实际存在的行都有审计快照，且重跑不会重复写入。
DO $verify$
DECLARE
    remaining int;
    known int;
    audited int;
BEGIN
    SELECT count(*) INTO remaining
    FROM log_collect_task AS t
    WHERE t.task_id IN (
        'a02c9080-cd6a-4a68-ac96-163c3b569b9a',
        '8bbe5e08-0abe-4cc4-b467-c00bf28ea9d5',
        'cab9f45d-1caf-42e0-b981-8ef4befe5999',
        'b7e0c501-66f4-430a-968d-5ebdad7fb07d'
    )
      AND t.status = 'failed'
      AND t.delivery_status = 'delivered'
      AND (
          t.detail #>> '{p1_archive_check,verified}' = 'true'
          OR (
              t.relay_status = 'success'
              AND t.integrity_state = 'verified'
              AND t.archive_sha256 IS NOT NULL
              AND t.archive_sha256 = t.object_sha256
          )
      );

    IF remaining > 0 THEN
        RAISE EXCEPTION '交付事实订正失败：仍有 % 条已核验交付被标为 failed', remaining;
    END IF;

    SELECT count(*) INTO known
    FROM log_collect_task
    WHERE task_id IN (
        'a02c9080-cd6a-4a68-ac96-163c3b569b9a',
        '8bbe5e08-0abe-4cc4-b467-c00bf28ea9d5',
        'cab9f45d-1caf-42e0-b981-8ef4befe5999',
        'b7e0c501-66f4-430a-968d-5ebdad7fb07d'
    );

    SELECT count(*) INTO audited
    FROM log_collect_task
    WHERE task_id IN (
        'a02c9080-cd6a-4a68-ac96-163c3b569b9a',
        '8bbe5e08-0abe-4cc4-b467-c00bf28ea9d5',
        'cab9f45d-1caf-42e0-b981-8ef4befe5999',
        'b7e0c501-66f4-430a-968d-5ebdad7fb07d'
    )
      AND detail #>> '{data_correction,delivered_false_positive_20261120,trace}'
          = 'lc-correction-Q20261120-delivered-false-positive';

    IF audited <> known THEN
        RAISE EXCEPTION '交付事实订正审计不完整：名单内 % 条，审计 % 条', known, audited;
    END IF;
    RAISE NOTICE '交付事实订正校验通过：名单内 % 条，审计 % 条', known, audited;
END $verify$;

COMMIT;
