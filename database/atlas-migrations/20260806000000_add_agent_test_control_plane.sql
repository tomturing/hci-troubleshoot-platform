-- hci-sim 阶段 C/D 控制面：不可变 Fixture Bundle 与 TestRun 生命周期。
-- 大对象仅保存 URI/digest，原始 Artifact 和 Lease 明文不得落入 PostgreSQL。

CREATE TABLE IF NOT EXISTS agent_test_scenario (
    id uuid PRIMARY KEY,
    support_id varchar(20) NOT NULL,
    kbd_revision bigint NOT NULL,
    variant varchar(64) NOT NULL,
    input_fingerprint varchar(71) NOT NULL,
    status varchar(20) NOT NULL,
    capability_gap jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_test_scenario_status CHECK (status IN ('draft', 'validated', 'approved', 'published', 'stale', 'retired', 'gap')),
    CONSTRAINT uq_agent_test_scenario_fingerprint UNIQUE (input_fingerprint)
);

CREATE INDEX IF NOT EXISTS idx_agent_test_scenario_support_revision
    ON agent_test_scenario (support_id, kbd_revision, status);

CREATE TABLE IF NOT EXISTS agent_test_fixture_bundle (
    id uuid PRIMARY KEY,
    scenario_id uuid NOT NULL REFERENCES agent_test_scenario(id) ON DELETE RESTRICT,
    revision integer NOT NULL,
    digest varchar(71) NOT NULL UNIQUE,
    schema_version varchar(16) NOT NULL,
    object_uri text NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 67108864),
    signature text,
    status varchar(20) NOT NULL,
    created_by varchar(128) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_test_fixture_bundle_status CHECK (status IN ('draft', 'validated', 'approved', 'published', 'stale', 'retired')),
    CONSTRAINT uq_agent_test_fixture_bundle_revision UNIQUE (scenario_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_agent_test_fixture_bundle_runnable
    ON agent_test_fixture_bundle (scenario_id, status, digest)
    WHERE status = 'published';

CREATE TABLE IF NOT EXISTS agent_test_fixture_dependency (
    bundle_id uuid NOT NULL REFERENCES agent_test_fixture_bundle(id) ON DELETE RESTRICT,
    dependency_type varchar(32) NOT NULL,
    dependency_id varchar(128) NOT NULL,
    revision varchar(128) NOT NULL,
    digest varchar(128) NOT NULL,
    PRIMARY KEY (bundle_id, dependency_type, dependency_id)
);

CREATE INDEX IF NOT EXISTS idx_agent_test_fixture_dependency_reverse
    ON agent_test_fixture_dependency (dependency_type, dependency_id, revision);

CREATE TABLE IF NOT EXISTS agent_test_fixture_provenance (
    bundle_id uuid NOT NULL REFERENCES agent_test_fixture_bundle(id) ON DELETE RESTRICT,
    route_id varchar(128) NOT NULL,
    artifact_id varchar(128) NOT NULL,
    artifact_digest varchar(128) NOT NULL,
    transform_digest varchar(128) NOT NULL,
    PRIMARY KEY (bundle_id, route_id)
);

CREATE TABLE IF NOT EXISTS agent_test_fixture_approval (
    id bigserial PRIMARY KEY,
    bundle_id uuid NOT NULL REFERENCES agent_test_fixture_bundle(id) ON DELETE RESTRICT,
    stage varchar(32) NOT NULL,
    actor_id varchar(128) NOT NULL,
    actor_role varchar(32) NOT NULL,
    decision varchar(16) NOT NULL,
    comment text NOT NULL DEFAULT '',
    decided_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_test_fixture_approval_decision CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT uq_agent_test_fixture_approval_role UNIQUE (bundle_id, stage, actor_role)
);

CREATE TABLE IF NOT EXISTS agent_test_fixture_audit (
    id bigserial PRIMARY KEY,
    entity_type varchar(32) NOT NULL,
    entity_id uuid NOT NULL,
    action varchar(64) NOT NULL,
    actor_id varchar(128) NOT NULL,
    trace_id varchar(64),
    before_state jsonb,
    after_state jsonb,
    created_at timestamptz NOT NULL DEFAULT now()
);

CREATE INDEX IF NOT EXISTS idx_agent_test_fixture_audit_entity
    ON agent_test_fixture_audit (entity_type, entity_id, created_at);

CREATE TABLE IF NOT EXISTS agent_test_run (
    id uuid PRIMARY KEY,
    support_id varchar(20) NOT NULL,
    kbd_revision bigint NOT NULL,
    scenario_id uuid NOT NULL REFERENCES agent_test_scenario(id) ON DELETE RESTRICT,
    bundle_digest varchar(71) NOT NULL REFERENCES agent_test_fixture_bundle(digest) ON DELETE RESTRICT,
    variant varchar(64) NOT NULL,
    execution_mode varchar(16) NOT NULL,
    status varchar(20) NOT NULL,
    version integer NOT NULL DEFAULT 1,
    idempotency_key varchar(256) NOT NULL,
    request_digest varchar(71) NOT NULL,
    deadline_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_test_run_mode CHECK (execution_mode = 'sim-ssh'),
    CONSTRAINT ck_agent_test_run_status CHECK (status IN ('requested', 'preparing', 'leased', 'running', 'passed', 'failed', 'inconclusive', 'cancelled', 'expired')),
    CONSTRAINT uq_agent_test_run_idempotency UNIQUE (idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_agent_test_run_status_deadline
    ON agent_test_run (status, deadline_at);

CREATE TABLE IF NOT EXISTS agent_test_run_attempt (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES agent_test_run(id) ON DELETE RESTRICT,
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    runtime_id varchar(128),
    lease_jti_hash varchar(71),
    status varchar(20) NOT NULL,
    started_at timestamptz,
    ended_at timestamptz,
    failure_type varchar(64),
    UNIQUE (run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS agent_test_run_event (
    run_id uuid NOT NULL REFERENCES agent_test_run(id) ON DELETE RESTRICT,
    attempt_no integer NOT NULL,
    seq integer NOT NULL,
    event_type varchar(64) NOT NULL,
    payload_digest varchar(71) NOT NULL,
    trace_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, attempt_no, seq)
);

CREATE TABLE IF NOT EXISTS agent_test_run_result (
    run_id uuid NOT NULL REFERENCES agent_test_run(id) ON DELETE RESTRICT,
    attempt_no integer NOT NULL,
    oracle_version varchar(64) NOT NULL,
    outcome varchar(20) NOT NULL,
    report_uri text NOT NULL,
    report_digest varchar(71) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS agent_test_runtime_instance (
    id varchar(128) PRIMARY KEY,
    shard varchar(128),
    schema_versions jsonb NOT NULL,
    capacity integer NOT NULL CHECK (capacity > 0),
    heartbeat_at timestamptz NOT NULL,
    status varchar(20) NOT NULL,
    CONSTRAINT ck_agent_test_runtime_instance_status CHECK (status IN ('ready', 'draining', 'unavailable'))
);

COMMENT ON TABLE agent_test_fixture_bundle IS 'hci-sim 已编译 Bundle 的不可变元数据；Runtime 只能读取 published 状态和受控对象 URI';
COMMENT ON TABLE agent_test_run IS '按 support ID 解析为精确 KBD revision、Scenario、Bundle digest 的逻辑 TestRun；不得存储 Lease 明文';
