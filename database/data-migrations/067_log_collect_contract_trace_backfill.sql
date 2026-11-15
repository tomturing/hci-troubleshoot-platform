-- 描述: 日志收集 error_code 语义纯净 + 唯一调用链缺口如实标记（PR #1510）
-- 日期: 2026-11-14
-- 调用链: log-collect-contract-trace-backfill-20261114
--
-- 背景：
--   1. cancel_task 与「节点自报纠正」曾把 cancelled_by_user / corrected_by_node
--      写进 error_code，而该列只允许契约规范码。取消/纠正是动作不是失败类别，
--      动作改为写在 detail 审计里，界面按「已取消」结论展示。
--   2. 存量任务的 trace_id 可能为空。绝不合成假 ID：造一个与任何真实请求
--      都不关联的 ID，会让「按调用链检索」看似有结果、实则指向空，比明确缺失更危险。
--      只在 detail.trace_backfill 里标记缺失与原因，供运维按任务号另查。
--
-- 约束：
--   - 只做 DML；列与索引的唯一权威是 database/desired_schema.sql（Atlas 声明式），
--     本文件不建表、不建持久备份表（Atlas diff 会把它们判为漂移并删除）。
--   - 幂等：可重复执行；订正前值写进行内审计，不建备份表。
--   - 不变量：执行后 error_code 中不得残留任何哨兵值，否则 RAISE EXCEPTION 中断。

BEGIN;

-- 1) 哨兵值迁出 error_code，动作与订正前值留在行内审计
UPDATE log_collect_task
SET error_code = NULL,
    detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object(
        'sentinel_migration',
        jsonb_build_object(
            'action', error_code,
            'trace', 'log-collect-contract-trace-backfill-20261114',
            'at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'before_error_code', error_code
        )
    )
WHERE error_code IN ('cancelled_by_user', 'corrected_by_node');

-- 2) 历史任务缺 trace_id：如实标记，不伪造链路
UPDATE log_collect_task
SET detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object(
        'trace_backfill',
        jsonb_build_object(
            'trace', 'log-collect-contract-trace-backfill-20261114',
            'at', to_char(NOW() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'missing', true,
            'reason', '历史任务未记录调用链，无法按 trace 检索；请按任务号与发生时间另查'
        )
    )
WHERE (trace_id IS NULL OR btrim(trace_id) = '')
  AND NOT (COALESCE(detail, '{}'::jsonb) ? 'trace_backfill');

-- 3) 硬校验：哨兵值必须清零；被标记缺失的行数可核对
DO $verify$
DECLARE
    bad_sentinels integer;
    marked_missing integer;
    sentinel_fixed integer;
BEGIN
    SELECT count(*) INTO bad_sentinels
    FROM log_collect_task
    WHERE error_code IN ('cancelled_by_user', 'corrected_by_node');
    IF bad_sentinels > 0 THEN
        RAISE EXCEPTION '067 不变量失败：error_code 仍残留 % 条哨兵值', bad_sentinels;
    END IF;

    SELECT count(*) INTO sentinel_fixed
    FROM log_collect_task
    WHERE detail -> 'sentinel_migration' ->> 'trace' = 'log-collect-contract-trace-backfill-20261114';
    SELECT count(*) INTO marked_missing
    FROM log_collect_task
    WHERE detail -> 'trace_backfill' ->> 'missing' = 'true';

    RAISE NOTICE '067 校验通过：哨兵值清零，行内审计 % 条，调用链缺失标记 % 条（未伪造 ID）',
        sentinel_fixed, marked_missing;
END $verify$;

COMMIT;
