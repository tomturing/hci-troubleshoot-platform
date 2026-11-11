-- 默认场景清单覆写：只存被管理员改过的模块，未改的模块继续用随镜像发布的快照。
-- 恢复默认 = 删除覆写行，因此不需要把 11 份清单复制进数据库。
CREATE TABLE IF NOT EXISTS log_collect_default_scenario (
    module_code varchar(32) NOT NULL,
    revision integer NOT NULL DEFAULT 1,
    items jsonb NOT NULL DEFAULT '[]'::jsonb,
    item_count integer NOT NULL DEFAULT 0,
    config_sha256 char(64) NOT NULL,
    config_bytes integer NOT NULL DEFAULT 0,
    enabled boolean NOT NULL DEFAULT true,
    updated_by varchar(64),
    trace_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CONSTRAINT log_collect_default_scenario_pkey PRIMARY KEY (module_code),
    CONSTRAINT ck_log_collect_default_scenario_revision CHECK (revision >= 1),
    CONSTRAINT ck_log_collect_default_scenario_item_count CHECK (item_count >= 0)
);

COMMENT ON TABLE log_collect_default_scenario IS '日志收集默认场景清单覆写：按 acli 模块保存管理员修改过的收集条目';
COMMENT ON COLUMN log_collect_default_scenario.module_code IS 'acli -m 模块名，也是节点上被覆写的清单文件名（不含 .json）';
COMMENT ON COLUMN log_collect_default_scenario.revision IS '覆写版本号：每次保存递增，历史任务可解释当时用的是哪一版';
COMMENT ON COLUMN log_collect_default_scenario.items IS '收集条目数组；结构与自定义场景一致，下发时按 spc 序列化';
COMMENT ON COLUMN log_collect_default_scenario.config_sha256 IS '本次序列化原文的 SHA-256，与桥侧回读校验对齐';
COMMENT ON COLUMN log_collect_default_scenario.trace_id IS '唯一调用链：把保存动作与后续下发、还原事件串起来';

-- 任务侧记录本次实际覆写的槽位集合：终态还原与平台兜底 restore_spc 都依赖它。
ALTER TABLE log_collect_task ADD COLUMN IF NOT EXISTS spc_slots jsonb;
COMMENT ON COLUMN log_collect_task.spc_slots IS '本次下发的 spc 槽位集合；多槽位时必须逐槽位还原';
