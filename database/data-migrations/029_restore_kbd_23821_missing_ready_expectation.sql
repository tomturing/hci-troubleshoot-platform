-- ===========================================================================
-- Migration: 029_restore_kbd_23821_missing_ready_expectation.sql
-- 背景：KBD23821 的 qmpcmd 信号用于确认 block-jobs 输出缺少 ready 字段。
--       历史 admin-ui 保存可能将该信号重新写回 expected=true，导致现场缺少
--       ready 时被判定为“与预期矛盾”。
-- 修复：将该信号的期望判断结果收敛为 false；Matcher 运行时契约保持不变。
-- 幂等：仅作用 support_id='23821' 和稳定 signal id，重复执行无副作用。
-- ===========================================================================

UPDATE kbd_entry
SET signals_json = jsonb_set(
        signals_json,
        '{signals}',
        (
            SELECT jsonb_agg(
                CASE
                    WHEN sig->>'id' = 'expert_1786499837113_brbf6r6fivk'
                     AND jsonb_typeof(sig->'match') = 'object'
                    THEN jsonb_set(sig, '{match,expected}', 'false'::jsonb, true)
                    ELSE sig
                END
                ORDER BY ord
            )
            FROM jsonb_array_elements(signals_json->'signals') WITH ORDINALITY AS t(sig, ord)
        ),
        true
    )
WHERE support_id = '23821'
  AND jsonb_typeof(signals_json) = 'object'
  AND jsonb_typeof(signals_json->'signals') = 'array'
  AND EXISTS (
      SELECT 1
      FROM jsonb_array_elements(signals_json->'signals') AS sig
      WHERE sig->>'id' = 'expert_1786499837113_brbf6r6fivk'
        AND jsonb_typeof(sig->'match') = 'object'
        AND sig->'match'->'expected' IS DISTINCT FROM 'false'::jsonb
  );
