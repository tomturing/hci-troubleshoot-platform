-- ===========================================================================
-- 迁移: 20260714000000_add_signals_json_drop_steps_json.sql
-- 说明: 关键信号字段级分别抽取（Key-Signal Field-level Extraction）
-- 背景: 原 steps_json 为扁平工具步骤 [{tool_name,tool_args_template,expected_pattern}]，
--       缺 producer/consumer 角色与变量绑定元数据，无法无损转为关键信号；
--       开发测试阶段无对外发布契约，直接彻底移除 steps_json，新增 signals_json
--       作为唯一结构化信号字段（跨 kbd_entry / sop_document 通用）。
-- 参考: docs/solution/agent/02-架构设计/关键信号字段级分别抽取.md ADR-1
-- ===========================================================================

-- kbd_entry：移除 steps_json，新增 signals_json
ALTER TABLE kbd_entry DROP COLUMN IF EXISTS steps_json;
ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS signals_json jsonb NOT NULL DEFAULT '[]'::jsonb;

-- sop_document：新增 signals_json（该表原无 steps_json，跨文档同构）
ALTER TABLE sop_document ADD COLUMN IF NOT EXISTS signals_json jsonb NOT NULL DEFAULT '[]'::jsonb;

-- 部分 GIN 索引（仅 published 且含信号时可见，供 InvestigationAgent 检索）
DROP INDEX IF EXISTS idx_kbd_entry_steps_json;
CREATE INDEX IF NOT EXISTS idx_kbd_signals ON kbd_entry
  USING GIN (signals_json) WHERE status = 'published' AND signals_json != '[]'::jsonb;
CREATE INDEX IF NOT EXISTS idx_sop_signals ON sop_document
  USING GIN (signals_json) WHERE status = 'published' AND signals_json != '[]'::jsonb;

-- 列注释
COMMENT ON COLUMN kbd_entry.signals_json IS '关键信号集合（Producer/Consumer 信号，agent 执行与判定；默认[]，由抽取阶段/审核期填充；占位符 {{VAR}} 大写）';
COMMENT ON COLUMN sop_document.signals_json IS '关键信号集合（跨文档通用，与 kbd_entry.signals_json 同构）';
