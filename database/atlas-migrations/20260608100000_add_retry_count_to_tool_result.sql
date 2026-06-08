-- ============================================================
-- 迁移：tool_result 表新增 retry_count 字段（T1-4）
-- Version : 20260608100000
-- Issue   : T-RELIABILITY-P1 (PR-1)
-- 背景    : ReactEngine 在工具执行失败可重试时，先前仅 logger.warning，
--          没有把实际重试次数落库审计；T1-4 要求把每次工具调用的
--          retry_count 写入 tool_result 表，便于评测闭环计算可靠性。
-- ============================================================

-- ── UP ───────────────────────────────────────────────────────────────
ALTER TABLE tool_result
    ADD COLUMN IF NOT EXISTS retry_count smallint NOT NULL DEFAULT 0;

COMMENT ON COLUMN tool_result.retry_count IS '工具执行重试次数（T1-4），0 表示一次成功，N 表示经过 N 次重试';

-- ── DOWN (回滚) ──────────────────────────────────────────────────────
-- ALTER TABLE tool_result DROP COLUMN IF EXISTS retry_count;
