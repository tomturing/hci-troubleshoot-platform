-- 落地 qkv_effect（效果验证，条件型效果验证生产者信号）。
--
-- 设计来源：docs/solution/agent/效果验证生产者信号设计与需求.md §10.1。
-- 1. 新增 effect_verification（效果验证会话，不可变期望快照 + 状态机 + 调度锚点）
--    与 effect_verification_check（每次观测判定一条，append-only 时间线）两张表
--    （conversation-service 模块区，与 vm_console_capture 相邻）；
-- 2. effect_verification.next_check_at 是复核调度的持久化锚点：进程重启后由
--    conversation-service lifespan 扫描恢复，不依赖内存态定时器；
-- 3. diagnostic_item.type 无 CHECK 约束，新增 effect_verification 类型值只需注释
--    更新（stage 沿用 S5：复核的是 S5 修复动作的效果）。
-- 全部语句幂等，可重复执行。

CREATE TABLE IF NOT EXISTS effect_verification (
    verification_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    tenant_id varchar(64),
    case_id varchar(32) NOT NULL,
    conversation_id uuid,
    diagnosis_run_id varchar(64),
    signal_id varchar(128),
    -- remediation_verify=修复后复核（默认）；symptom_confirm=S1 症状确认
    usage varchar(32) NOT NULL DEFAULT 'remediation_verify',
    -- 被复核动作的关联键（工具执行 exec_id），remediation_verify 模式必填于业务层
    action_exec_id varchar(64),
    source_kbd_id varchar(64),
    source_kbd_revision varchar(64),
    tool_catalog_revision varchar(128),
    -- 冻结的期望锚点快照（观测通道 + 封闭 matcher + 时序窗口）；
    -- KBD 事后修订不能篡改进行中的判定依据
    expectation_snapshot jsonb NOT NULL,
    -- 目标变量解析快照（HOST 等）与验证时间
    target_verification jsonb NOT NULL DEFAULT '{}'::jsonb,
    -- 状态机（设计文档 §5.4）：created → expectation_resolved → settle_pending →
    -- observing → verdict_* | recheck_scheduled → … → window_expired →
    -- verdict_inconclusive；任意阶段 → failed/cancelled
    status varchar(32) NOT NULL DEFAULT 'created',
    -- 三态判定词表（effect-verdict-v1）：achieved/not_achieved/inconclusive
    verdict varchar(32),
    verdict_vocabulary_revision varchar(64) NOT NULL DEFAULT 'effect-verdict-v1',
    recheck_count integer NOT NULL DEFAULT 0,
    -- 复核调度持久化锚点：调度器扫描 status 未终结且 next_check_at <= now() 的记录
    next_check_at timestamptz,
    error_code varchar(64),
    error_summary text,
    trace_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    CONSTRAINT ck_effect_verification_usage CHECK (
        (usage)::text = ANY ((ARRAY['remediation_verify'::varchar, 'symptom_confirm'::varchar])::text[])
    ),
    CONSTRAINT ck_effect_verification_verdict CHECK (
        verdict IS NULL
        OR (verdict)::text = ANY ((ARRAY['achieved'::varchar, 'not_achieved'::varchar, 'inconclusive'::varchar])::text[])
    )
);

COMMENT ON TABLE effect_verification IS '效果验证（qkv_effect）会话记录：冻结期望快照、三态判定、复核调度锚点与状态机';
COMMENT ON COLUMN effect_verification.expectation_snapshot IS '编译期冻结的期望锚点；运行时只读回放，禁止随 KBD 修订篡改';
COMMENT ON COLUMN effect_verification.next_check_at IS '复核调度持久化锚点；进程重启后由 lifespan 扫描恢复';
COMMENT ON COLUMN effect_verification.trace_id IS 'W3C Trace ID（全链路追踪）';

CREATE INDEX IF NOT EXISTS idx_effect_verification_case ON effect_verification (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_effect_verification_conversation ON effect_verification (conversation_id);
CREATE INDEX IF NOT EXISTS idx_effect_verification_schedule
    ON effect_verification (next_check_at)
    WHERE status NOT IN ('verdict_achieved', 'verdict_not_achieved', 'verdict_inconclusive', 'failed', 'cancelled');
CREATE INDEX IF NOT EXISTS idx_effect_verification_trace_id ON effect_verification (trace_id);

CREATE TABLE IF NOT EXISTS effect_verification_check (
    check_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    verification_id uuid NOT NULL REFERENCES effect_verification (verification_id) ON DELETE CASCADE,
    check_seq integer NOT NULL,
    checked_at timestamptz NOT NULL DEFAULT now(),
    -- scheduler=复核调度触发；manual=人工复核
    trigger_source varchar(16) NOT NULL DEFAULT 'scheduler',
    -- valid=观测有效；error=观测失败/通道不可用；insufficient=负证据观测域有效性不足
    observation_status varchar(32) NOT NULL,
    observation_summary text,
    -- evaluate_matcher 的人类可读证据串（期望/命中/最终判定）
    matcher_evidence text,
    check_verdict varchar(32),
    error_code varchar(64),
    trace_id varchar(64),
    CONSTRAINT uq_effect_check_seq UNIQUE (verification_id, check_seq),
    CONSTRAINT ck_effect_check_trigger CHECK (
        (trigger_source)::text = ANY ((ARRAY['scheduler'::varchar, 'manual'::varchar])::text[])
    ),
    CONSTRAINT ck_effect_check_observation CHECK (
        (observation_status)::text = ANY ((ARRAY['valid'::varchar, 'error'::varchar, 'insufficient'::varchar])::text[])
    ),
    CONSTRAINT ck_effect_check_verdict CHECK (
        check_verdict IS NULL
        OR (check_verdict)::text = ANY ((ARRAY['achieved'::varchar, 'not_achieved'::varchar, 'inconclusive'::varchar])::text[])
    )
);

COMMENT ON TABLE effect_verification_check IS '效果验证每次观测判定记录（append-only 时间线）；与 effect_verification 一对多';

CREATE INDEX IF NOT EXISTS idx_effect_check_verification ON effect_verification_check (verification_id, check_seq);

-- diagnostic_item.type 无 CHECK 约束；新增 effect_verification 类型值仅更新注释。
COMMENT ON COLUMN diagnostic_item."type" IS '条目类型：hypothesis（S2）/ verification_step（S3）/ root_cause（S4）/ solution（S5）/ effect_verification（S5 修复动作效果复核）';
