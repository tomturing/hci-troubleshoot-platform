-- 064：场景清单分发（SPC）/专项类失败的历史归因订正
--
-- 背景（2026-11-12 Q2026110302155，同一工单连点 5 次）：
--   三次失败踩在不同阶段，历史数据却把它们压成了几个"看起来一样"的码：
--     1. 场景清单扇出失败 → 落成 LC-PLATFORM-011（未归因），
--        文案「场景未能写入全部目标节点（节点不可达…；可用节点为 0）」；
--     2. acli 明确回了「收集专项 core 不存在！」→ 落 LC-EXEC-003，
--        且 error_message 被前端过滤成空串，原因彻底丢失；
--     3. 下发后节点根本没建任务 → 落 LC-PLUGIN-008（"任务不存在"），
--        与 015（"下发后未建立"）处置动作完全不同：一个要查任务 ID，
--        一个要核对 acli 版本/专项。
--
-- 本迁移只做**可复现、可回滚**的订正：
--   a. 只处理「011（未归因占位码）」与「旧语义的 008（文案明确写着未建立采集任务）」，
--      不碰任何上游精确码——谁失败谁出码，bridge 报的码比文案推导的强。
--      011 是 catalog 里"推导不出来"的占位码，本身不含语义，精化它不会覆盖真实结论；
--   b. 旧码写进 detail.error_code_derivation.before_code，事后可逐行核对/回滚；
--   c. 幂等：订正后旧码不再命中，重复执行无副作用；
--   d. 匹配不到的行保持原样——宁可留 011，也不靠猜测改码。
--
-- 为什么第 3 条不算"覆盖上游码"：008 与 015 都是平台自己按文案推导出来的码，
-- 不是节点回报的语义；文案本身已经明确否定了"任务不存在"这个解释。
--
-- 规则子集来源：backend/case-service/app/services/log_collect_error_attribution.py
-- 的 _RULES 中 LC-SPC-* 与 LC-PLUGIN-015/016 部分，顺序即优先级（先具体后泛化）。
-- 该子集与 Python 侧的一致性由 tests/unit/test_log_collect_error_attribution.py 强校验。
--
-- 唯一调用链标记：log-collect-errorcode-spc-20261112

BEGIN;

-- Step 1: 新增规则子集（临时表：仅本事务可见，不给 Atlas 声明式 schema 留表）
CREATE TEMP TABLE lc_error_rules_spc (
    ord integer NOT NULL,
    needle text NOT NULL,
    code text NOT NULL
) ON COMMIT DROP;

INSERT INTO lc_error_rules_spc (ord, needle, code) VALUES
        (1, '场景清单目录缺失', 'LC-SPC-001'),
        (2, '场景清单目录不唯一', 'LC-SPC-002'),
        (3, '场景清单备份回读不一致', 'LC-SPC-003'),
        (4, '场景清单备份失败', 'LC-SPC-003'),
        (5, '场景清单写入后校验不一致', 'LC-SPC-004'),
        (6, '场景清单写入被拒绝', 'LC-SPC-004'),
        (7, '场景清单写入失败', 'LC-SPC-004'),
        (8, '场景清单文件缺失', 'LC-SPC-009'),
        (9, '场景清单回滚失败', 'LC-SPC-005'),
        (10, '节点 SSH 认证失败', 'LC-SPC-007'),
        (11, '节点命令执行超时', 'LC-SPC-008'),
        (12, '节点 SSH 连接失败', 'LC-SPC-006'),
        (13, '场景清单写入未通过', 'LC-SPC-010'),
        (14, '场景未能写入全部目标节点', 'LC-SPC-010'),
        (15, '收集专项', 'LC-PLUGIN-016'),
        (16, '节点上没有所选专项', 'LC-PLUGIN-016'),
        (17, '未建立采集任务', 'LC-PLUGIN-015');

-- Step 2: 011（未归因占位码）→ 具体码
WITH targets AS (
    SELECT t.task_id, COALESCE(t.error_message, '') AS msg
    FROM log_collect_task t
    WHERE t.error_code = 'LC-PLATFORM-011'
      AND COALESCE(t.error_message, '') <> ''
),
matched AS (
    -- DISTINCT ON 取 ord 最小者 = 规则表里最靠前（最具体）的命中
    SELECT DISTINCT ON (g.task_id) g.task_id, r.code
    FROM targets g
    JOIN lc_error_rules_spc r ON strpos(g.msg, r.needle) > 0
    ORDER BY g.task_id, r.ord
)
UPDATE log_collect_task t
SET error_code = m.code,
    detail = jsonb_set(
        COALESCE(t.detail, '{}'::jsonb),
        '{error_code_derivation}',
        COALESCE(t.detail -> 'error_code_derivation', '{}'::jsonb)
        || jsonb_build_object(
            'before_code', 'LC-PLATFORM-011',
            'code', m.code,
            'source', 'spc_fix_064',
            'trace', 'log-collect-errorcode-spc-20261112'
        )
    ),
    updated_at = now()
FROM matched m
WHERE t.task_id = m.task_id;

-- Step 3: 旧语义 008 → 015：文案已明确是"下发后未建立采集任务"
UPDATE log_collect_task t
SET error_code = 'LC-PLUGIN-015',
    detail = jsonb_set(
        COALESCE(t.detail, '{}'::jsonb),
        '{error_code_derivation}',
        COALESCE(t.detail -> 'error_code_derivation', '{}'::jsonb)
        || jsonb_build_object(
            'before_code', 'LC-PLUGIN-008',
            'code', 'LC-PLUGIN-015',
            'source', 'spc_fix_064',
            'trace', 'log-collect-errorcode-spc-20261112'
        )
    ),
    updated_at = now()
WHERE t.error_code = 'LC-PLUGIN-008'
  AND COALESCE(t.error_message, '') LIKE '%未建立采集任务%';

-- Step 4: 不变量——失败任务不允许留空码。本迁移不补空（062 已负责），
-- 只把"仍有空码"这件事显式暴露出来，避免静默放过。
DO $$
DECLARE blank integer;
BEGIN
    SELECT count(*) INTO blank
    FROM log_collect_task
    WHERE status = 'failed' AND error_code IS NULL;
    IF blank > 0 THEN
        RAISE WARNING '% 个失败任务仍无 error_code（需代码侧补归因，本迁移不猜）', blank;
    END IF;
END $$;

COMMIT;