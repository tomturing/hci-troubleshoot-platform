-- 065：日志包内容证据回填（历史「假完成 / 空壳包」一次性标注）
--
-- 背景（2026-11-12 staging）：
--   Q2026111201130 的 519c8644：status=completed、progress=100，但 archive_size 与
--   object_size 均为空、delivery_status=none，节点上 zip 根本不存在——面板显示
--   「已完成 100%」与「交付结论 未开始」并排，属于自相矛盾。
--   Q2026111200624 的 3666833a：全链路 sha256 一致、delivery+relay 都成功，但包内只有
--   _members / cfgmaster_ini 两个配置条目（608 字节），面板显示绿色成功。
--
-- 规则来源：
--   backend/case-service/app/domain/log_collect_display.py 的 artifact 判定与
--   contracts/log_collect_display.json 的 thresholds.min_archive_bytes。
--   SQL 无法 import Python，此处只做「缺失 / 过小」两类可判定的标注；
--   「仅元数据 / 条目数不足」需要读 zip 中央目录，由 bridge 探测后写入
--   log_collect_artifact_manifest，不在本迁移内猜测。
--
-- 语义：
--   只补 status='completed' 且 detail.artifact_quality 为空的任务；不改 status，
--   不改 delivery_status——状态机语义保持不变，仅补证据字段，由派生层改变展示结论。
--
-- 安全措施：
--   1. 幂等：WHERE detail->>'artifact_quality' IS NULL，重跑影响 0 行；
--   2. 行内快照：before 值写进 detail.display_audit，事后可区分「探测得出的结论」
--      与「按缺失/体积推断的结论」；
--   3. 不建备份表：Atlas 声明式 schema 会 DROP 掉未声明的表，行内快照才是安全区。
--
-- 唯一调用链标记：log-collect-artifact-backfill-20261112

BEGIN;

-- Step 1: 标注「无产物」与「产物过小」两类历史任务
WITH judged AS (
    SELECT
        task_id,
        CASE
            WHEN COALESCE(NULLIF(object_size, 0), NULLIF(archive_size, 0)) IS NULL THEN 'invalid'
            WHEN COALESCE(NULLIF(object_size, 0), NULLIF(archive_size, 0)) < 1048576 THEN 'invalid'
            ELSE NULL
        END AS quality,
        CASE
            WHEN COALESCE(NULLIF(object_size, 0), NULLIF(archive_size, 0)) IS NULL THEN 'missing'
            WHEN COALESCE(NULLIF(object_size, 0), NULLIF(archive_size, 0)) < 1048576 THEN 'too_small'
            ELSE NULL
        END AS reason
    FROM log_collect_task
    WHERE status = 'completed'
      AND detail ->> 'artifact_quality' IS NULL
)
UPDATE log_collect_task AS t
SET detail = COALESCE(t.detail, '{}'::jsonb) || jsonb_build_object(
        'artifact_quality', judged.quality,
        'artifact_reason', judged.reason,
        'display_audit', jsonb_build_object(
            'source', 'migration-065',
            'trace', 'log-collect-artifact-backfill-20261112',
            'at', to_char((now() AT TIME ZONE 'UTC'), 'YYYY-MM-DD"T"HH24:MI:SS"Z"'),
            'before', jsonb_build_object(
                'status', t.status,
                'delivery_status', t.delivery_status,
                'relay_status', t.relay_status,
                'archive_size', t.archive_size,
                'object_size', t.object_size,
                'upload_mode', t.upload_mode
            )
        )
    )
FROM judged
WHERE t.task_id = judged.task_id
  AND judged.quality IS NOT NULL;

-- Step 2: 不变量校验（数据完整性，不是展示口径）
--   2.1 「已完成且零产物的 P2 任务」不得漏标
DO $$
DECLARE
    missed integer;
BEGIN
    SELECT count(*) INTO missed
    FROM log_collect_task
    WHERE status = 'completed'
      AND COALESCE(upload_mode, 'bridge_relay') = 'bridge_relay'
      AND COALESCE(NULLIF(object_size, 0), NULLIF(archive_size, 0)) IS NULL
      AND COALESCE(detail ->> 'artifact_quality', '') <> 'invalid';

    IF missed > 0 THEN
        RAISE EXCEPTION '065 校验失败：% 个已完成但零产物的任务未被标注为 invalid', missed;
    END IF;
END $$;

--   2.2 「标注为过小」的任务体积必须真的小于阈值
DO $$
DECLARE
    inconsistent integer;
BEGIN
    SELECT count(*) INTO inconsistent
    FROM log_collect_task
    WHERE detail ->> 'artifact_reason' = 'too_small'
      AND COALESCE(NULLIF(object_size, 0), NULLIF(archive_size, 0)) >= 1048576;

    IF inconsistent > 0 THEN
        RAISE EXCEPTION '065 校验失败：% 个被标为过小的任务体积不小于阈值', inconsistent;
    END IF;
END $$;

-- Step 3: 输出对账数字（留痕在迁移日志里）
DO $$
DECLARE
    total_completed integer;
    marked_missing integer;
    marked_small integer;
BEGIN
    SELECT count(*) INTO total_completed FROM log_collect_task WHERE status = 'completed';
    SELECT count(*) INTO marked_missing
    FROM log_collect_task
    WHERE detail ->> 'artifact_reason' = 'missing';
    SELECT count(*) INTO marked_small
    FROM log_collect_task
    WHERE detail ->> 'artifact_reason' = 'too_small';

    RAISE NOTICE '065 完成：completed 任务 % 个；标注无产物 % 个、产物过小 % 个',
        total_completed, marked_missing, marked_small;
END $$;

COMMIT;