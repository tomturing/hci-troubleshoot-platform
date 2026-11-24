-- 069：日志包清单快照回填（「清单已入库、detail 却无快照」的历史存量）
--
-- 背景（2026-11-24 staging）：
--   put_artifact_manifest 曾把「清单快照」与「包质量结论」塞进同一条 jsonb 更新，
--   并用
--     WHERE detail ->> 'artifact_quality' IS DISTINCT FROM 'ok' OR :quality = 'ok'
--   整体挡住。质量一旦被判过 ok，后续上报的清单快照就连带一起被丢掉，
--   而清单事实其实已经写进 log_collect_artifact_manifest。
--
--   客户面板只拿任务行（避免 N+1），S7 的判据是 detail.artifact_manifest 快照，
--   读不到就落「未知」——即使证据早就到了。当日确认 5 条任务命中：
--     Q2026091700436/60a0a2e6、Q2026112301059/0f63aafc、Q2026112400585/03d79904、
--     Q2026112400992/2410d2c4、Q2026112401156/bdcf6a40。
--
-- 语义：
--   只补「真表有行、detail 无快照」的任务；快照字段逐项取自真表（source/quality/
--   reason/entry_count/entry_total_bytes/has_encrypted/inspected_at），不推断、不造数。
--   回填后 S7 恢复「证据已回传」，包好不好读仍由 quality/reason 表达。
--
-- 安全措施：
--   1. 幂等：COALESCE(detail ? 'artifact_manifest', false) = false，重跑影响 0 行；
--   2. 行内审计：detail.display_audit 记录 migration-069 与 before 摘要；
--   3. 不建备份表：Atlas 声明式 schema 会 DROP 未声明的表，行内快照才是安全区。
--
-- 唯一调用链标记：log-collect-manifest-backfill-20261124

BEGIN;

WITH latest AS (
    SELECT DISTINCT ON (m.task_id)
           m.task_id,
           m.source,
           m.quality,
           m.reason,
           m.entry_count,
           m.entry_total_bytes,
           m.has_encrypted,
           m.trace_id,
           m.inspected_at
    FROM log_collect_artifact_manifest AS m
    ORDER BY m.task_id, m.created_at DESC
)
UPDATE log_collect_task AS t
SET detail = COALESCE(t.detail, '{}'::jsonb) || jsonb_build_object(
        'artifact_manifest', jsonb_build_object(
            'received', true,
            'source', latest.source,
            'quality', latest.quality,
            'reason', latest.reason,
            'entry_count', latest.entry_count,
            'entry_total_bytes', latest.entry_total_bytes,
            'has_encrypted', latest.has_encrypted,
            'trace_id', latest.trace_id,
            'inspected_at', to_char(latest.inspected_at AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            -- 标注来源：读侧据此区分「桥上报时写的快照」与「事后回填」
            'backfill', 'migration-069'
        ),
        'display_audit', jsonb_build_object(
            'source', 'migration-069',
            'trace', 'log-collect-manifest-backfill-20261124',
            'at', to_char(now() AT TIME ZONE 'UTC', 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'before', jsonb_build_object(
                'status', t.status,
                'artifact_quality', t.detail ->> 'artifact_quality',
                'artifact_reason', t.detail ->> 'artifact_reason'
            )
        )
    )
FROM latest
WHERE t.task_id = latest.task_id
  AND COALESCE(t.detail ? 'artifact_manifest', false) = false;

-- 不变量：只要有清单行，任务行就必须有快照（这正是本次修掉的裂缝）
DO $$
DECLARE
    missed integer;
BEGIN
    SELECT count(*) INTO missed
    FROM log_collect_task AS t
    WHERE EXISTS (
            SELECT 1 FROM log_collect_artifact_manifest AS m WHERE m.task_id = t.task_id
        )
      AND COALESCE(t.detail ? 'artifact_manifest', false) = false;

    IF missed > 0 THEN
        RAISE EXCEPTION '069 校验失败：% 个任务有清单行但 detail 无快照', missed;
    END IF;
END $$;

-- 对账数字（留痕在 Job 日志里）
DO $$
DECLARE
    backfilled integer;
    with_manifest integer;
BEGIN
    SELECT count(*) INTO backfilled
    FROM log_collect_task
    WHERE detail -> 'artifact_manifest' ->> 'backfill' = 'migration-069';

    SELECT count(*) INTO with_manifest
    FROM log_collect_task
    WHERE COALESCE(detail ? 'artifact_manifest', false) = true;

    RAISE NOTICE '069 完成：本次回填 % 个任务；当前已有快照 % 个', backfilled, with_manifest;
END $$;

COMMIT;
