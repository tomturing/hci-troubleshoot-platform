-- ===========================================================================
-- Migration: 024_fix_kbd_23821_scope_and_matcher.sql
-- 背景: KBD 23821（[HCI] 690虚拟机迁移存储位置，一直卡在9%）仿真诊断反复输出
--       「关键信号执行结果：无可执行证据」+ DEFINITIVE: INCONCLUSIVE。根因链路
--       （见 docs/verify/events/2026-08-12-KBD23821对比与优化验证.md）：
--       1) verification_contract.scope.versions 硬编码 ["6.9.0","6.11.1_R1"]，
--          环境缺 version 变量时 evaluate_scope 判 UNKNOWN → INCONCLUSIVE，
--          调度器只执行 CANDIDATE 候选，信号在调度入口被整体跳过（零执行）；
--       2) 信号 expert_1786499837113_brbf6r6fivk 的 match.expected=true 与故障
--          语义反向（根因是 Qemu info block-jobs 缺失 ready 字段，expected=true
--          要求包含 ready 会把故障现场反向误判为 CONTRADICTED）。
--       此前两项修复均为运行时手工改库（未固化），环境重建即丢失，导致问题
--       跨 PR743/PR745 反复出现。本迁移将其固化为可重放数据迁移。
-- 修复: 1) 23821 的 verification_contract.scope.versions 置 []（与其他已发布
--          KBD 一致：空数组在 evaluate_scope 中跳过版本检查，恒 APPLICABLE）；
--       2) 信号 expert_1786499837113_brbf6r6fivk 的 match.expected 置 false。
-- 幂等: 仅作用 support_id='23821'；jsonb_set 结果收敛，重复执行无副作用；
--       第二段仅在目标信号 expected 当前值非 false 时才 UPDATE。
-- 注意: 本迁移不处理 publish_validation.tool_contract_revision 过期问题——
--       该值由发布门禁（certify_publishable_signals_json）按当前代码契约生成，
--       任何 signals/*.schema.json 变更都会使其失配，属预期防漂移行为；已发布
--       KBD 须走 maintenance 工作稿重新发布（POST /{kbd_id}/maintenance/publish）
--       刷新盖章，禁止在迁移中写死 hash 或删除发布门禁字段。
-- ===========================================================================

-- 1) scope.versions → []：消除「missing environment.version → UNKNOWN → 信号全跳过」
UPDATE kbd_entry
SET signals_json = jsonb_set(
        signals_json,
        '{verification_contract,scope,versions}',
        '[]'::jsonb,
        true
    )
WHERE support_id = '23821'
  AND jsonb_typeof(signals_json) = 'object'
  AND signals_json ? 'verification_contract';

-- 2) match.expected → false：仅对目标信号、且当前值非 false 时收敛更新
UPDATE kbd_entry
SET signals_json = jsonb_set(
        signals_json,
        '{signals}',
        (
            SELECT jsonb_agg(
                CASE
                    WHEN sig->>'id' = 'expert_1786499837113_brbf6r6fivk'
                     AND jsonb_typeof(sig->'match') = 'object'
                     AND sig->'match'->'expected' IS DISTINCT FROM 'false'::jsonb
                    THEN jsonb_set(sig, '{match,expected}', 'false'::jsonb)
                    ELSE sig
                END
                ORDER BY ord
            )
            FROM jsonb_array_elements(signals_json->'signals') WITH ORDINALITY AS t(sig, ord)
        )
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
