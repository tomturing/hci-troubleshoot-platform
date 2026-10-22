-- ===========================================================================
-- 迁移: 20261022000002_add_log_collect_relay_status.sql
-- 说明: 为 P3 段③（平台 → 5888 日志服务器 代传）补充独立状态字段。
-- 背景:
--   1. 代传是异步长任务（20 GiB ≈ 30 分钟），必须与任务主状态机解耦：
--      主状态反映业务阶段，relay_status 反映归档阶段的真实结果；
--   2. 5888 的 init_upload 每次生成新 upload_id（不复用），因此平台必须持久化
--      relay_upload_id / relay_chunk_index 才能续传；
--   3. 归档成功后服务端删除会话 JSON，平台以 upload_status 404 作为落地判据，
--      收到 complete 并不等于归档完成；
--   4. 平台不做自动清理：本字段只记录状态，不驱动任何删除。
-- 幂等: ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS
-- ===========================================================================

ALTER TABLE log_collect_task
    ADD COLUMN IF NOT EXISTS relay_status varchar(16) NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS relay_chunk_size integer,
    ADD COLUMN IF NOT EXISTS relay_updated_at timestamptz;

ALTER TABLE log_collect_task DROP CONSTRAINT IF EXISTS ck_log_collect_relay_status;
ALTER TABLE log_collect_task ADD CONSTRAINT ck_log_collect_relay_status
    CHECK ((relay_status)::text = ANY ((ARRAY['none'::character varying, 'pending'::character varying, 'running'::character varying, 'success'::character varying, 'failed'::character varying])::text[]));

COMMENT ON COLUMN log_collect_task.relay_status IS 'P3 段③ 代传状态：none/pending/running/success/failed；与任务主状态机解耦';
COMMENT ON COLUMN log_collect_task.relay_chunk_size IS 'P3 段③ 服务端返回的分片大小（10 MiB），续传时用于定位分片';
COMMENT ON COLUMN log_collect_task.relay_updated_at IS 'P3 段③ 状态最近更新时间';
