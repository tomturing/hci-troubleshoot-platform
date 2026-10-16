-- ===========================================================================
-- 迁移: 20261013000000_add_kbd_entry_root_signal_json.sql
-- 说明: kbd_entry 新增 root_signal_json 列（根因信号落库）+ 批量任务类型扩枚举
-- 背景: 根因信号是与关键信号（signals_json）完全解耦的第二条抽取轨——每篇 KBD 从
--       整篇内容抽取全部「可确证根因」的结论侧定位信号（不限条数），双形态
--       executable（qfk_* 只读采集判定）/ text（可溯源关键词包含匹配）。
--       v2 落库为顶层 JSON 数组 [{signal}, ...]（v1 单对象已废，无存量数据不做迁移）。
--       本列是 AI 派生元数据（同 ai_category_id 先例）：不入 kbd_revision payload，
--       因此 published 条目亦可回刷本列而不产生新版本、不影响发布盖章有效性。
-- 幂等: ADD COLUMN IF NOT EXISTS / CREATE INDEX IF NOT EXISTS / DROP+ADD CONSTRAINT
-- 参考: docs/solution/knowledge-base/根因信号抽取方案.md
-- ===========================================================================

ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS root_signal_json jsonb;

COMMENT ON COLUMN kbd_entry.root_signal_json IS '根因信号 v2 顶层数组（结论侧定位信号集合，与关键信号 signals_json 完全解耦，不限条数）：每个元素 match_type=executable 携带 qfk_* 只读采集命令、=text 携带可溯源关键词；provenance.trace_tier 标主锚(root_cause)/兑底(content)，兑底强制 needs_review；distinctiveness 记录冲突度；NULL 表示尚未抽取';

-- v2 多信号：落库为顶层 JSON 数组（不再接受单对象/标量；一 KBD 可有多条定位信号）
ALTER TABLE kbd_entry DROP CONSTRAINT IF EXISTS ck_kbd_entry_root_signal_json;
ALTER TABLE kbd_entry ADD CONSTRAINT ck_kbd_entry_root_signal_json
    CHECK (root_signal_json IS NULL OR jsonb_typeof(root_signal_json) = 'array');

-- 审核队列过滤：数组内「任一信号满足条件」的包含查询（按形态 match_type / 待复核 needs_review）
-- v1 的两个标量表达式索引（root_signal_json->>'match_type' 等）对数组失效，改为 GIN(jsonb_path_ops)。
DROP INDEX IF EXISTS idx_kbd_entry_root_signal_type;
DROP INDEX IF EXISTS idx_kbd_entry_root_signal_review;
CREATE INDEX IF NOT EXISTS idx_kbd_entry_root_signal_gin ON kbd_entry USING GIN (root_signal_json jsonb_path_ops)
    WHERE root_signal_json IS NOT NULL;

-- 批量任务类型扩枚举：extract_root_signal（根因信号回刷）
ALTER TABLE kbd_batch_job DROP CONSTRAINT IF EXISTS ck_kbd_batch_job_type;
ALTER TABLE kbd_batch_job ADD CONSTRAINT ck_kbd_batch_job_type
    CHECK (job_type::text = ANY (ARRAY[
        'reanalyze_images'::character varying::text,
        'reclassify'::character varying::text,
        'extract_signals'::character varying::text,
        'extract_root_signal'::character varying::text,
        'approve'::character varying::text,
        'reject'::character varying::text,
        'set_review_owner'::character varying::text,
        'set_unpublishable'::character varying::text
    ]));

COMMENT ON COLUMN kbd_batch_job.job_type IS '任务类型：reanalyze_images（重新识图）/reclassify（重新分类）/extract_signals（重新抽取关键信号）/extract_root_signal（回刷根因信号）/approve（审核通过）/reject（审核拒绝）';
