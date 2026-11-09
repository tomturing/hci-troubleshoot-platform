-- 自定义场景新增 spc 模式：场景不再只是路径清单，而是一份可直接覆写
-- spc/<core>.json 的收集定义（含 copy/command/script 与 filters）。
-- 原 CHECK 只允许 visual/json，必须重建；DROP IF EXISTS 保证可重复执行。

ALTER TABLE log_collect_custom_scenario
    DROP CONSTRAINT IF EXISTS ck_log_collect_custom_scenario_source;

ALTER TABLE log_collect_custom_scenario
    ADD CONSTRAINT ck_log_collect_custom_scenario_source
    CHECK ((source)::text = ANY ((ARRAY['visual'::character varying, 'json'::character varying, 'spc'::character varying])::text[]));

COMMENT ON COLUMN log_collect_custom_scenario.source IS '创建方式：visual=勾选列表；json=JSON 导入；spc=spc 收集定义';
