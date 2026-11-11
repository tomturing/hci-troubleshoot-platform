-- 默认场景收敛：删除零消费列与可由 items 推导的列。
-- 依据：审核态没有任何消费方且种子数据从未流转；审计时间列由 operation_audit 覆盖；
-- 清单表的 enabled 恒为 true，item_count / config_sha256 / config_bytes 均可由 items 现场计算。
-- CHECK 约束随所属列被 PostgreSQL 一并删除。

BEGIN;

-- 空库路径下 data-migrations 先于 Atlas 建表执行，必须 IF EXISTS 才不会
-- 因 "relation does not exist" 让整个 db-migrate Job（set -e）中止。
ALTER TABLE IF EXISTS log_collect_scenario
    DROP COLUMN IF EXISTS review_state,
    DROP COLUMN IF EXISTS updated_by,
    DROP COLUMN IF EXISTS created_at,
    DROP COLUMN IF EXISTS updated_at;

ALTER TABLE IF EXISTS log_collect_default_scenario
    DROP COLUMN IF EXISTS item_count,
    DROP COLUMN IF EXISTS config_sha256,
    DROP COLUMN IF EXISTS config_bytes,
    DROP COLUMN IF EXISTS enabled,
    DROP COLUMN IF EXISTS updated_by,
    DROP COLUMN IF EXISTS created_at,
    DROP COLUMN IF EXISTS updated_at;

COMMIT;
