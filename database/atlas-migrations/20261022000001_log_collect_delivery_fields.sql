-- ===========================================================================
-- 迁移: 20261022000001_log_collect_delivery_fields.sql
-- 说明: 为大文件交付（2–20 GiB）补充断点续传与清理状态字段。
-- 背景:
--   1. 交付路径收敛为两条：P1（HCI 直传 5888，upload_mode='acli_direct'）、
--      P3（bridge 中转，upload_mode='bridge_relay'）。原 P2（平台直传）废弃，
--      故收敛 upload_mode 枚举，移除 'platform_direct'。
--   2. 20 GiB 传输必然遭遇中断，续传依赖 pull_offset（段①）与
--      relay_upload_id/relay_chunk_index（段③）。段② 的分片以文件系统目录为准，
--      不落库，避免为 1:1 关系新建 upload 会话表。
--   3. 平台不做任何自动清理：仅以 cleanup_state 标记告警，删除由用户（HCI）
--      或运维（平台）执行，故 archive_deleted 只记录事实，不驱动删除。
-- 幂等: ADD COLUMN IF NOT EXISTS / DROP CONSTRAINT IF EXISTS
-- ===========================================================================

-- 历史数据防御：'platform_direct' 对应已废弃的 P2，语义无法区分 P1/P3，
-- 统一按 P1（插件直传）收敛；新建表通常无此类数据。
UPDATE log_collect_task SET upload_mode = 'acli_direct' WHERE upload_mode = 'platform_direct';

ALTER TABLE log_collect_task
    ADD COLUMN IF NOT EXISTS pull_offset_bytes bigint,
    ADD COLUMN IF NOT EXISTS pull_sha256 char(64),
    ADD COLUMN IF NOT EXISTS relay_upload_id text,
    ADD COLUMN IF NOT EXISTS relay_chunk_index integer,
    ADD COLUMN IF NOT EXISTS relay_chunk_total integer,
    ADD COLUMN IF NOT EXISTS relay_restarts integer NOT NULL DEFAULT 0,
    ADD COLUMN IF NOT EXISTS cleanup_state varchar(16) NOT NULL DEFAULT 'none',
    ADD COLUMN IF NOT EXISTS cleanup_reason varchar(64),
    ADD COLUMN IF NOT EXISTS cleanup_ack_by varchar(64),
    ADD COLUMN IF NOT EXISTS cleanup_ack_at timestamptz,
    ADD COLUMN IF NOT EXISTS archive_deleted boolean NOT NULL DEFAULT false,
    ADD COLUMN IF NOT EXISTS archive_deleted_at timestamptz,
    ADD COLUMN IF NOT EXISTS archive_deleted_by varchar(64);

-- 收敛 upload_mode 枚举：移除已废弃的 platform_direct。
-- 表达式按 Atlas 规范化形式书写，保证声明式 schema apply 后 schema diff 为空。
ALTER TABLE log_collect_task DROP CONSTRAINT IF EXISTS ck_log_collect_upload_mode;
ALTER TABLE log_collect_task ADD CONSTRAINT ck_log_collect_upload_mode
    CHECK ((upload_mode IS NULL) OR ((upload_mode)::text = ANY ((ARRAY['acli_direct'::character varying, 'bridge_relay'::character varying, 'none'::character varying])::text[])));

ALTER TABLE log_collect_task DROP CONSTRAINT IF EXISTS ck_log_collect_cleanup_state;
ALTER TABLE log_collect_task ADD CONSTRAINT ck_log_collect_cleanup_state
    CHECK ((cleanup_state)::text = ANY ((ARRAY['none'::character varying, 'required'::character varying, 'acked'::character varying])::text[]));

ALTER TABLE log_collect_task DROP CONSTRAINT IF EXISTS ck_log_collect_pull_offset;
ALTER TABLE log_collect_task ADD CONSTRAINT ck_log_collect_pull_offset
    CHECK ((pull_offset_bytes IS NULL) OR (pull_offset_bytes >= 0));

COMMENT ON COLUMN log_collect_task.pull_offset_bytes IS 'P3 段① 已从 HCI 拉取到客户 PC 的字节数（rsync/SFTP 续传依据）';
COMMENT ON COLUMN log_collect_task.pull_sha256 IS 'P3 段① 完成后源包 SHA-256，用于平台侧比对';
COMMENT ON COLUMN log_collect_task.relay_upload_id IS 'P3 段③ 日志服务器返回的 upload_id，用于判断能否续传';
COMMENT ON COLUMN log_collect_task.relay_chunk_index IS 'P3 段③ 最后成功块下标（0 基），必须逐块写入';
COMMENT ON COLUMN log_collect_task.relay_chunk_total IS 'P3 段③ 总块数（由服务端 chunk_size 计算）';
COMMENT ON COLUMN log_collect_task.relay_restarts IS 'P3 段③ 重新 init 次数；>0 表示服务端不支持续传，已整包重传';
COMMENT ON COLUMN log_collect_task.cleanup_state IS '清理告警状态：none/required/acked；仅作标记，绝不驱动删除';
COMMENT ON COLUMN log_collect_task.cleanup_reason IS '触发清理告警的原因：storage_high/expired/orphan_hci/relay_failed';
COMMENT ON COLUMN log_collect_task.archive_deleted IS '平台副本是否已被运维删除（仅记录事实，平台不自动删）';
