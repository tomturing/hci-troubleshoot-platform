-- spc 场景（覆写 core.json）落地：任务侧记录扇出结果与恢复状态，
-- 并把存量 visual/json 场景一次性迁移成 spc 条目模型。

ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS spc_slot varchar(32);
ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS spc_sha256 varchar(64);
ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS spc_hosts jsonb;
ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS restore_status varchar(16) NOT NULL DEFAULT 'none';

COMMENT ON COLUMN log_collect_task.spc_slot IS '被覆写的 spc 槽位（固定 core）';
COMMENT ON COLUMN log_collect_task.spc_sha256 IS '覆写的场景清单摘要，用于核对写入内容与下发内容一致';
COMMENT ON COLUMN log_collect_task.spc_hosts IS '扇出写入结果：每台节点的 ip/backup/error';
COMMENT ON COLUMN log_collect_task.restore_status IS 'none/pending/success/failed：终态恢复是否已还原原清单';

-- 存量场景迁移：items 由 {path} 补成 {action,path,category,note,...} 条目模型，
-- 并把通道口径从 visual/json 归一到 spc（routes 按 source 分叉，不改则永远走 -c）。
--
-- 只补缺失的键，绝不重建条目：serialize_spc 要原样输出 filters/date_placeholders/
-- docker/interpreter，一旦用 jsonb_build_object 重建这四项会被静默丢掉；
-- 同理也不覆盖已有的 action，否则 command/script 条目会被降级成 copy（命令串当路径 cp）。
-- 幂等：只处理仍是 visual/json 的行，重复执行是空操作。
UPDATE log_collect_custom_scenario
SET items = COALESCE((
        SELECT jsonb_agg(
                   element
                   || jsonb_build_object('action', COALESCE(element->>'action', 'copy'))
                   || jsonb_build_object('category', COALESCE(element->>'category', 'log'))
                   ORDER BY ord)
        FROM jsonb_array_elements(items) WITH ORDINALITY AS t(element, ord)
    ), '[]'::jsonb),
    source = 'spc',
    config_sha256 = NULL,
    config_bytes = NULL
WHERE source IN ('visual', 'json');
