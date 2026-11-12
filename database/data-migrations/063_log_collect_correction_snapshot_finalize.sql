-- 063：把一次性备份表里的原值并入 detail 行内快照，然后销毁备份表
--
-- 背景（2026-11-12）：
--   061/062 的初版建了两张快照表
--     log_collect_task_correction_20261112_stats      （42 行，僵尸会话清理）
--     log_collect_task_correction_20261112_errorcode  （87 行，error_code 回填）
--   在 staging 手工执行过。随后发现这类"额外表"与本项目的 Atlas 声明式
--   schema 门禁直接冲突：`atlas schema apply` 会把非期望 schema 的表 DROP 掉，
--   备份表反而最先被销毁。
--
--   定稿方案改为**行内快照**（原值写进 detail），迁移 061/062 已不再建表。
--   但 staging 的既有数据是旧版产生的快照：
--     061 只写了 before.delivery_status，缺 delivery_error /
--         delivery_finished_at / delivery_attempts；
--     062 完全没有写 before（原 error_code 无从追溯）。
--   而搬迁后的 061/062 重跑时命中 0 行（数据已干净），不会补这些字段，
--   所以必须由本迁移从备份表把原值搬进 detail，再销毁备份表——
--   否则删除备份表就等于销毁唯一的取证信息。
--
-- 幂等：
--   两张备份表任一不存在（新库、或本迁移已执行过）时对应步骤直接跳过；
--   已存在的 before 键一律保留（新值放左侧，已有值优先）。
--
-- 唯一调用链标记：log-collect-correction-snapshot-finalize-20261112

BEGIN;

-- Step 1: 061 的原值补全（before 全量字段）
DO $migrate$
DECLARE
    patched int;
BEGIN
    IF to_regclass('public.log_collect_task_correction_20261112_stats') IS NULL THEN
        RAISE NOTICE '063 Step 1 跳过：备份表 log_collect_task_correction_20261112_stats 不存在';
        RETURN;
    END IF;

    UPDATE log_collect_task t
    SET detail = jsonb_set(
            COALESCE(t.detail, '{}'::jsonb),
            '{data_correction,before}',
            jsonb_build_object(
                'status', b.status,
                'delivery_status', b.delivery_status,
                'delivery_error', b.delivery_error,
                'delivery_finished_at', b.delivery_finished_at,
                'delivery_attempts', b.delivery_attempts
            ) || COALESCE(t.detail -> 'data_correction' -> 'before', '{}'::jsonb)
        ),
        updated_at = NOW()
    FROM log_collect_task_correction_20261112_stats b
    WHERE t.task_id = b.task_id
      AND t.detail -> 'data_correction' IS NOT NULL;

    GET DIAGNOSTICS patched = ROW_COUNT;
    RAISE NOTICE '063 Step 1：补全 % 行的 data_correction.before', patched;
END $migrate$;

-- Step 2: 062 的原值补全（before 原 error_code + 判定所用文案）
DO $migrate$
DECLARE
    patched int;
BEGIN
    IF to_regclass('public.log_collect_task_correction_20261112_errorcode') IS NULL THEN
        RAISE NOTICE '063 Step 2 跳过：备份表 log_collect_task_correction_20261112_errorcode 不存在';
        RETURN;
    END IF;

    UPDATE log_collect_task t
    SET detail = jsonb_set(
            COALESCE(t.detail, '{}'::jsonb),
            '{error_code_derivation,before}',
            jsonb_build_object(
                'error_code', b.error_code_before,
                'message', COALESCE(
                    NULLIF(
                        CASE WHEN t.status = 'failed' THEN b.error_message
                             ELSE COALESCE(b.delivery_error, b.error_message)
                        END, ''
                    ), ''
                )
            ) || COALESCE(t.detail -> 'error_code_derivation' -> 'before', '{}'::jsonb)
        ),
        updated_at = NOW()
    FROM log_collect_task_correction_20261112_errorcode b
    WHERE t.task_id = b.task_id
      AND t.detail -> 'error_code_derivation' IS NOT NULL;

    GET DIAGNOSTICS patched = ROW_COUNT;
    RAISE NOTICE '063 Step 2：补全 % 行的 error_code_derivation.before', patched;
END $migrate$;

-- Step 3: 验证原值已落到行内快照，再销毁备份表
DO $migrate$
DECLARE
    missing int;
BEGIN
    IF to_regclass('public.log_collect_task_correction_20261112_stats') IS NOT NULL THEN
        SELECT count(*) INTO missing
        FROM log_collect_task_correction_20261112_stats b
        JOIN log_collect_task t ON t.task_id = b.task_id
        WHERE t.detail -> 'data_correction' -> 'before' -> 'delivery_status' IS NULL;

        IF missing > 0 THEN
            RAISE EXCEPTION '063 中止：% 行仍有 061 备份未并入 detail，拒绝销毁备份表', missing;
        END IF;

        DROP TABLE log_collect_task_correction_20261112_stats;
        RAISE NOTICE '063 Step 3：061 备份已并入行内快照，表已销毁';
    END IF;
END $migrate$;

DO $migrate$
DECLARE
    missing int;
BEGIN
    IF to_regclass('public.log_collect_task_correction_20261112_errorcode') IS NOT NULL THEN
        SELECT count(*) INTO missing
        FROM log_collect_task_correction_20261112_errorcode b
        JOIN log_collect_task t ON t.task_id = b.task_id
        WHERE t.detail -> 'error_code_derivation' -> 'before' IS NULL;

        IF missing > 0 THEN
            RAISE EXCEPTION '063 中止：% 行仍有 062 备份未并入 detail，拒绝销毁备份表', missing;
        END IF;

        DROP TABLE log_collect_task_correction_20261112_errorcode;
        RAISE NOTICE '063 Step 3：062 备份已并入行内快照，表已销毁';
    END IF;
END $migrate$;

COMMIT;