-- ===========================================================================
-- 迁移: 20261019000000_add_kbd_entry_root_signal_rejected_json.sql
-- 说明: kbd_entry 新增 root_signal_rejected_json 列（根因「拒绝候选」审计快照）
-- 背景: 根因抽取闭环方案 PR-3（§5.4/§6）——闭包不可通过的 executable 根因候选
--       既不塞进 root_signal_json（CHECK 仅存 accepted），也**不服务端自动降级 text**
--       （决策②：降级会掩盖闭包缺陷，是 #1159 用 SIGNAL_ALL_REJECTED 消灭的同类陷阱），
--       而是复用关键信号既有「拒绝候选」语义落本列持久化审计，供审核 UI 逐条呈现、
--       发布门禁判「未处理拒绝候选」、以及人工恢复/显式降级。
--       元素结构与 signals_json.rejected_candidates 同构：
--       {candidate, reason, reason_code, closure_status, unresolved, literals}。
--       本列同为 AI 派生元数据（不入 kbd_revision payload，同 root_signal_json 先例）。
-- 幂等: ADD COLUMN IF NOT EXISTS / DROP+ADD CONSTRAINT
-- 参考: docs/solution/knowledge-base/根因信号抽取闭环方案.md
-- ===========================================================================

ALTER TABLE kbd_entry ADD COLUMN IF NOT EXISTS root_signal_rejected_json jsonb;

COMMENT ON COLUMN kbd_entry.root_signal_rejected_json IS '根因「拒绝候选」审计快照（与关键信号 signals_json.rejected_candidates 同构）：闭包不可通过的 executable 根因候选不进 root_signal_json、也不自动降级 text，而是连同 reason/reason_code（ROOT_CLOSURE_DANGLING/ROOT_CLOSURE_LITERAL_NOT_PARAMETERIZED/ROOT_CLOSURE_CAPABILITY_MISSING）落此列，供审核 UI 逐条呈现与发布门禁判「未处理拒绝候选」；NULL = 尚无拒绝记录';

-- 拒绝候选为顶层 JSON 数组（与 root_signal_json 同规则：仅接受数组或 NULL）
ALTER TABLE kbd_entry DROP CONSTRAINT IF EXISTS ck_kbd_entry_root_signal_rejected_json;
ALTER TABLE kbd_entry ADD CONSTRAINT ck_kbd_entry_root_signal_rejected_json
    CHECK (root_signal_rejected_json IS NULL OR jsonb_typeof(root_signal_rejected_json) = 'array');
