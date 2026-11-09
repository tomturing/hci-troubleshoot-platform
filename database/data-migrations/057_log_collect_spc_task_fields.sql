-- spc 场景（覆写 core.json）落地：任务侧记录扇出结果与恢复状态，
-- 并把存量 visual/json 场景一次性迁移成 spc 条目模型。

ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS spc_slot varchar(32);
ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS spc_sha256 varchar(64);
ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS spc_hosts jsonb;
ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS restore_status varchar(16) NOT NULL DEFAULT 'none';

COMMENT ON COLUMN log_collect_task.spc_slot IS '被覆写的 spc 槽位（固定 core）';
COMMENT ON COLUMN log_collect_task.spc_hosts IS '扇出写入结果：每台节点的 ip/backup/error';
COMMENT ON COLUMN log_collect_task.restore_status IS 'none/pending/success/failed：终态恢复是否已还原原清单';

-- 存量场景迁移：paths 模型补 action=copy，source 统一为 spc。
-- 幂等：只处理仍是 visual/json 的行。
UPDATE log_collect_custom_scenario
SET items = COALESCE((
        SELECT jsonb_agg(jsonb_build_object(
            'action', 'copy',
            'path', element->>'path',
            'category', COALESCE(element->>'category', 'log'),
            'note', element->'note'
        ))
        FROM jsonb_array_elements(items) AS element
    ), '[]'::jsonb),
    source = 'spc',
    config_sha256 = NULL,
    config_bytes = NULL
WHERE source IN ('visual', 'json');
