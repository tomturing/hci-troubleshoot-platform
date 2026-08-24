-- 落地 qkv_vm_console（虚拟机控制台截图，条件型实时视觉生产者信号）。
--
-- 设计来源：docs/solution/agent/虚拟机控制台视觉生产者信号设计与需求.md §10.1。
-- 1. 新增 vm_console_capture / vm_console_capture_artifact 两张不可变记录表
--    （conversation-service 模块区，与 bridge_execution_artifacts 相邻）；
-- 2. 原地扩展 collector_definition 三处安全约束：executor 词表新增
--    vm_console_capture；risk_level 允许 controlled_interaction（仅限该执行器，
--    近黑唤醒 sendkey down 属受控 Guest 交互）；输出上限对该执行器放宽到 16MB。
--    放宽仅作用于新执行器，既有 shell/http/manual 采集器约束保持原样。
-- 全部语句幂等，可重复执行。

CREATE TABLE IF NOT EXISTS vm_console_capture (
    capture_id uuid PRIMARY KEY,
    tenant_id varchar(64),
    case_id varchar(32) NOT NULL,
    diagnosis_run_id varchar(64),
    conversation_id uuid,
    signal_id varchar(128),
    mode varchar(16) NOT NULL DEFAULT 'online',
    host_node_id varchar(128) NOT NULL,
    vm_id varchar(32) NOT NULL,
    target_verification jsonb NOT NULL DEFAULT '{}'::jsonb,
    source_kbd_id varchar(64),
    source_kbd_revision varchar(64),
    tool_catalog_revision varchar(128),
    adapter_version varchar(64),
    status varchar(32) NOT NULL DEFAULT 'created',
    error_code varchar(64),
    error_summary text,
    baseline_artifact_id uuid,
    recapture_artifact_id uuid,
    effective_artifact_id uuid,
    quality_metrics jsonb NOT NULL DEFAULT '{}'::jsonb,
    wake_state varchar(32) NOT NULL DEFAULT 'not_needed',
    wake_token_hash varchar(64),
    wake_confirmed_by varchar(128),
    wake_confirmed_at timestamptz,
    wake_executed_at timestamptz,
    wake_result varchar(32),
    vision_result jsonb,
    vision_model_revision varchar(64),
    vision_prompt_revision varchar(64),
    vision_vocabulary_revision varchar(64),
    vision_confidence numeric(4,3),
    trace_id varchar(64) NOT NULL,
    exec_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    completed_at timestamptz,
    expires_at timestamptz,
    CONSTRAINT ck_vm_console_capture_mode
        CHECK ((mode)::text = ANY ((ARRAY['online'::varchar, 'offline'::varchar])::text[])),
    CONSTRAINT ck_vm_console_capture_wake_state CHECK (
        (wake_state)::text = ANY (
            (ARRAY['not_needed'::varchar, 'confirmation_pending'::varchar, 'confirmed'::varchar,
                   'declined'::varchar, 'non_interactive'::varchar, 'timed_out'::varchar,
                   'failed'::varchar])::text[]
        )
    )
);

-- 每个 case_id + vm_id + diagnosis_run_id 最多一次截图会话（含最多一次唤醒）。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'uq_vmc_one_wake_per_run'
    ) THEN
        ALTER TABLE vm_console_capture
            ADD CONSTRAINT uq_vmc_one_wake_per_run UNIQUE (case_id, vm_id, diagnosis_run_id);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_vm_console_capture_case ON vm_console_capture (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vm_console_capture_trace_id ON vm_console_capture (trace_id);
CREATE INDEX IF NOT EXISTS idx_vm_console_capture_status ON vm_console_capture (status);
CREATE INDEX IF NOT EXISTS idx_vm_console_capture_expires_at ON vm_console_capture (expires_at) WHERE expires_at IS NOT NULL;

CREATE TABLE IF NOT EXISTS vm_console_capture_artifact (
    artifact_id uuid PRIMARY KEY,
    capture_id uuid NOT NULL REFERENCES vm_console_capture (capture_id) ON DELETE CASCADE,
    kind varchar(8) NOT NULL,
    sha256 varchar(64) NOT NULL,
    media_type varchar(64) NOT NULL,
    size_bytes bigint NOT NULL,
    width integer,
    height integer,
    storage_ref text NOT NULL,
    sensitivity varchar(32) NOT NULL DEFAULT 'confidential',
    source varchar(16) NOT NULL DEFAULT 'online',
    bundle_id uuid,
    trace_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz,
    last_read_at timestamptz,
    last_read_by varchar(128),
    CONSTRAINT ck_vm_console_artifact_kind CHECK ((kind)::text = ANY ((ARRAY['ppm'::varchar, 'png'::varchar])::text[])),
    CONSTRAINT ck_vm_console_artifact_source
        CHECK ((source)::text = ANY ((ARRAY['online'::varchar, 'offline_bundle'::varchar])::text[]))
);

-- 存量环境补齐读取审计列（幂等）。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_name = 'vm_console_capture_artifact' AND column_name = 'last_read_at'
    ) THEN
        ALTER TABLE vm_console_capture_artifact ADD COLUMN last_read_at timestamptz;
        ALTER TABLE vm_console_capture_artifact ADD COLUMN last_read_by varchar(128);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_vm_console_artifact_capture ON vm_console_capture_artifact (capture_id);
CREATE INDEX IF NOT EXISTS idx_vm_console_artifact_sha256 ON vm_console_capture_artifact (sha256);

-- collector_definition 三约束同步放宽（仅限 vm_console_capture 执行器）。
-- 仅在 diagnosis-service 模块表已存在的环境执行（全新 DB 由 desired_schema.sql 直接携带新约束）。
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = 'collector_definition'
    ) THEN
        RETURN;
    END IF;

    ALTER TABLE collector_definition DROP CONSTRAINT IF EXISTS ck_collector_definition_executor;
    ALTER TABLE collector_definition ADD CONSTRAINT ck_collector_definition_executor
        CHECK ((executor)::text = ANY ((ARRAY['shell'::varchar, 'http'::varchar, 'manual'::varchar, 'vm_console_capture'::varchar])::text[]));

    ALTER TABLE collector_definition DROP CONSTRAINT IF EXISTS ck_collector_definition_risk;
    ALTER TABLE collector_definition ADD CONSTRAINT ck_collector_definition_risk CHECK (
        risk_level = 'read_only'
        OR (risk_level = 'controlled_interaction' AND executor = 'vm_console_capture')
    );

    ALTER TABLE collector_definition DROP CONSTRAINT IF EXISTS ck_collector_definition_output_size;
    ALTER TABLE collector_definition ADD CONSTRAINT ck_collector_definition_output_size CHECK (
        (executor = 'vm_console_capture' AND max_output_mb > 0 AND max_output_mb <= 16)
        OR (executor <> 'vm_console_capture' AND max_output_mb > 0 AND max_output_mb <= 4)
    );
END $$;
