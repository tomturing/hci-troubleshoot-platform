-- 055：日志收集产物索引（object_key / object_size / object_sha256）
--
-- 背景（2026-10-28 现场）：
--   平台对 P2 会在 objects/<case_id>/<upload_id>.zip 留一份副本，但任务表里
--   没有"平台对象"的字段，只有 archive_path。而 archive_path 的语义是混的：
--     P1 -> 节点上的 /sf/data/local/acli/tmp/.../Qxxx.zip（扫描后自删）
--     P2 -> 平台对象 key（<case_id>/<upload_id>.zip）
--   结果：判断"平台有没有留包"只能进容器 ls，任何产物索引/清理/校验都做不了；
--   complete 回写失败时（Q2026102800720 的 connection is closed），
--   连"对象在哪、多大、sha 是多少"都无从追溯。
--
-- 设计：
--   1. 新列只描述**平台对象**，与 archive_path（节点侧）解耦；
--   2. 仅 P2 且有对象时写入，P1 保持 NULL（P1 平台不留副本，NULL 即"无对象"）；
--   3. 回填只认 upload_mode='bridge_relay' 且 archive_path 非绝对路径的行，
--      不去猜 P1 的节点路径。
--
-- 唯一调用链标记：log_collect_object_index_v1
-- 幂等：ADD COLUMN IF NOT EXISTS + 回填条件 object_key IS NULL。

BEGIN;

ALTER TABLE log_collect_task
    ADD COLUMN IF NOT EXISTS object_key text,
    ADD COLUMN IF NOT EXISTS object_size bigint,
    ADD COLUMN IF NOT EXISTS object_sha256 char(64);

COMMENT ON COLUMN log_collect_task.object_key IS
    '平台对象存储内的相对 key（P2：<case_id>/<upload_id>.zip）；P1 为 NULL，表示平台不留副本';
COMMENT ON COLUMN log_collect_task.object_size IS '平台对象字节数（与 archive_size 区分：后者可能是节点侧包大小）';
COMMENT ON COLUMN log_collect_task.object_sha256 IS '平台对象 SHA-256，用于事后校验对象是否被改写';

-- 回填：archive_path 不以 / 开头的是对象 key（P1 的节点路径一定是绝对路径）
UPDATE log_collect_task
SET object_key = archive_path,
    object_size = archive_size,
    object_sha256 = archive_sha256
WHERE object_key IS NULL
  AND upload_mode = 'bridge_relay'
  AND archive_path IS NOT NULL
  AND archive_path NOT LIKE '/%';

COMMIT;
