-- 自定义场景收敛：删除零消费的审核态列与重复审计列。
-- 依据：review_state 全仓没有任何过滤/拦截消费方，客户侧只按 enabled 判定；
--       created_by / updated_by / created_at / updated_at 由 operation_audit（含 actor 与 trace_id）
--       和结构化日志覆盖，界面直接打印 updated_by 只会把内置管理员 UUID 摆到用户面前。
-- CHECK 约束 ck_log_collect_custom_scenario_review_state 随所属列被 PostgreSQL 一并删除。

BEGIN;

-- CI 顺序是先按 desired_schema.sql 建表、再重放全部 data-migrations（ON_ERROR_STOP=1）。
-- 空库路径下 data-migrations 也可能先于 Atlas 执行，因此必须 IF EXISTS；
-- 本文件不得出现指向已删列的 COMMENT ON COLUMN。
ALTER TABLE IF EXISTS log_collect_custom_scenario
    DROP COLUMN IF EXISTS review_state,
    DROP COLUMN IF EXISTS created_by,
    DROP COLUMN IF EXISTS updated_by,
    DROP COLUMN IF EXISTS created_at,
    DROP COLUMN IF EXISTS updated_at;

COMMIT;
