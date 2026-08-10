-- hci-sim 独立数据库首个迁移。
--
-- 该文件只在 hci_sim 数据库执行，绝不能被主库 Atlas Job 读取。表按领域
-- schema 分类，跨数据库只保存不可变 support/KBD/bundle 标识，不建立跨库外键。
-- 原始 Artifact 和 Lease 明文不进入 PostgreSQL。

CREATE SCHEMA IF NOT EXISTS control_plane;
CREATE SCHEMA IF NOT EXISTS fixture;
CREATE SCHEMA IF NOT EXISTS artifact;
CREATE SCHEMA IF NOT EXISTS audit;

CREATE TABLE IF NOT EXISTS control_plane.scenario (
    id uuid PRIMARY KEY,
    support_id varchar(20) NOT NULL,
    kbd_revision bigint NOT NULL,
    variant varchar(64) NOT NULL,
    input_fingerprint varchar(71) NOT NULL UNIQUE,
    status varchar(20) NOT NULL,
    capability_gap jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT scenario_status CHECK (status IN ('draft', 'validated', 'approved', 'published', 'stale', 'retired', 'gap'))
);

CREATE TABLE IF NOT EXISTS control_plane.run (
    id uuid PRIMARY KEY,
    support_id varchar(20) NOT NULL,
    kbd_revision bigint NOT NULL,
    scenario_id uuid NOT NULL REFERENCES control_plane.scenario(id) ON DELETE RESTRICT,
    bundle_digest varchar(71) NOT NULL,
    variant varchar(64) NOT NULL,
    execution_mode varchar(16) NOT NULL,
    status varchar(20) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    idempotency_key varchar(256) NOT NULL UNIQUE,
    request_digest varchar(71) NOT NULL,
    deadline_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT run_mode CHECK (execution_mode = 'sim-ssh'),
    CONSTRAINT run_status CHECK (status IN ('requested', 'preparing', 'leased', 'running', 'passed', 'failed', 'inconclusive', 'cancelled', 'expired'))
);

CREATE TABLE IF NOT EXISTS control_plane.run_attempt (
    id uuid PRIMARY KEY,
    run_id uuid NOT NULL REFERENCES control_plane.run(id) ON DELETE RESTRICT,
    attempt_no integer NOT NULL CHECK (attempt_no > 0),
    runtime_id varchar(128),
    lease_jti_hash varchar(71),
    status varchar(20) NOT NULL,
    started_at timestamptz,
    ended_at timestamptz,
    failure_type varchar(64),
    UNIQUE (run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS control_plane.run_event (
    run_id uuid NOT NULL REFERENCES control_plane.run(id) ON DELETE RESTRICT,
    attempt_no integer NOT NULL,
    seq integer NOT NULL,
    event_type varchar(64) NOT NULL,
    payload_digest varchar(71) NOT NULL,
    trace_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, attempt_no, seq)
);

CREATE TABLE IF NOT EXISTS control_plane.run_result (
    run_id uuid NOT NULL REFERENCES control_plane.run(id) ON DELETE RESTRICT,
    attempt_no integer NOT NULL,
    oracle_version varchar(64) NOT NULL,
    outcome varchar(20) NOT NULL,
    report_uri text NOT NULL,
    report_digest varchar(71) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (run_id, attempt_no)
);

CREATE TABLE IF NOT EXISTS control_plane.runtime_instance (
    id varchar(128) PRIMARY KEY,
    shard varchar(128),
    schema_versions jsonb NOT NULL,
    capacity integer NOT NULL CHECK (capacity > 0),
    heartbeat_at timestamptz NOT NULL,
    status varchar(20) NOT NULL CHECK (status IN ('ready', 'draining', 'unavailable'))
);

CREATE TABLE IF NOT EXISTS fixture.bundle (
    id uuid PRIMARY KEY,
    scenario_id uuid NOT NULL REFERENCES control_plane.scenario(id) ON DELETE RESTRICT,
    revision integer NOT NULL,
    digest varchar(71) NOT NULL UNIQUE,
    schema_version varchar(16) NOT NULL,
    object_uri text NOT NULL,
    object_digest varchar(71) NOT NULL,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 67108864),
    signature text,
    status varchar(20) NOT NULL,
    created_by varchar(128) NOT NULL,
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bundle_status CHECK (status IN ('draft', 'validated', 'approved', 'published', 'stale', 'retired')),
    CONSTRAINT bundle_revision UNIQUE (scenario_id, revision)
);

CREATE TABLE IF NOT EXISTS fixture.dependency (
    bundle_id uuid NOT NULL REFERENCES fixture.bundle(id) ON DELETE RESTRICT,
    dependency_type varchar(32) NOT NULL,
    dependency_id varchar(128) NOT NULL,
    revision varchar(128) NOT NULL,
    digest varchar(128) NOT NULL,
    PRIMARY KEY (bundle_id, dependency_type, dependency_id)
);

CREATE TABLE IF NOT EXISTS fixture.provenance (
    bundle_id uuid NOT NULL REFERENCES fixture.bundle(id) ON DELETE RESTRICT,
    route_id varchar(128) NOT NULL,
    artifact_id varchar(128) NOT NULL,
    artifact_digest varchar(128) NOT NULL,
    transform_digest varchar(128) NOT NULL,
    PRIMARY KEY (bundle_id, route_id)
);

CREATE TABLE IF NOT EXISTS fixture.approval (
    id bigserial PRIMARY KEY,
    bundle_id uuid NOT NULL REFERENCES fixture.bundle(id) ON DELETE RESTRICT,
    stage varchar(32) NOT NULL,
    actor_id varchar(128) NOT NULL,
    actor_role varchar(32) NOT NULL,
    decision varchar(16) NOT NULL,
    comment text NOT NULL DEFAULT '',
    decided_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT approval_decision CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT approval_role UNIQUE (bundle_id, stage, actor_role)
);

CREATE TABLE IF NOT EXISTS fixture.stale_outbox (
    id bigserial PRIMARY KEY,
    dependency_type varchar(32) NOT NULL,
    dependency_id varchar(128) NOT NULL,
    dependency_revision varchar(128) NOT NULL,
    dependency_digest varchar(128) NOT NULL,
    reason_code varchar(64) NOT NULL,
    trace_id varchar(64),
    status varchar(16) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    processed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT stale_status CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
    CONSTRAINT stale_dependency UNIQUE (dependency_type, dependency_id, dependency_revision, dependency_digest, reason_code)
);

CREATE TABLE IF NOT EXISTS artifact.metadata (
    id varchar(128) PRIMARY KEY,
    digest varchar(71) NOT NULL UNIQUE,
    size_bytes bigint NOT NULL CHECK (size_bytes > 0 AND size_bytes <= 67108864),
    media_type varchar(128) NOT NULL,
    schema_version varchar(64) NOT NULL,
    source_type varchar(64) NOT NULL,
    source_ref_digest varchar(71) NOT NULL,
    redaction_digest varchar(71) NOT NULL,
    collection_policy varchar(128) NOT NULL,
    collector_id varchar(128) NOT NULL,
    collected_at timestamptz NOT NULL,
    status varchar(20) NOT NULL,
    ingested_by varchar(128) NOT NULL,
    trace_id varchar(64),
    version integer NOT NULL DEFAULT 1 CHECK (version > 0),
    revoke_reason varchar(128),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT artifact_status CHECK (status IN ('staged', 'scanned', 'approved', 'revoked'))
);

CREATE TABLE IF NOT EXISTS artifact.scan (
    id bigserial PRIMARY KEY,
    artifact_id varchar(128) NOT NULL REFERENCES artifact.metadata(id) ON DELETE RESTRICT,
    scanner_revision varchar(128) NOT NULL,
    secret_scan_passed boolean NOT NULL,
    pii_scan_passed boolean NOT NULL,
    license_scan_passed boolean NOT NULL,
    schema_valid boolean NOT NULL,
    trace_id varchar(64),
    scanned_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT scan_passed CHECK (secret_scan_passed AND pii_scan_passed AND license_scan_passed AND schema_valid)
);

CREATE TABLE IF NOT EXISTS artifact.approval (
    id bigserial PRIMARY KEY,
    artifact_id varchar(128) NOT NULL REFERENCES artifact.metadata(id) ON DELETE RESTRICT,
    actor_id varchar(128) NOT NULL,
    actor_role varchar(32) NOT NULL,
    decision varchar(16) NOT NULL,
    comment text NOT NULL DEFAULT '',
    trace_id varchar(64),
    decided_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT artifact_approval_role CHECK (actor_role IN ('expert', 'security')),
    CONSTRAINT artifact_approval_decision CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT artifact_approval_unique_role UNIQUE (artifact_id, actor_role),
    CONSTRAINT artifact_approval_unique_actor UNIQUE (artifact_id, actor_id)
);

CREATE TABLE IF NOT EXISTS audit.entity_event (
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

CREATE INDEX IF NOT EXISTS scenario_support_revision ON control_plane.scenario (support_id, kbd_revision, status);
CREATE INDEX IF NOT EXISTS run_status_deadline ON control_plane.run (status, deadline_at);
CREATE INDEX IF NOT EXISTS bundle_runnable ON fixture.bundle (scenario_id, status, digest) WHERE status = 'published';
CREATE INDEX IF NOT EXISTS dependency_reverse ON fixture.dependency (dependency_type, dependency_id, revision);
CREATE INDEX IF NOT EXISTS stale_outbox_pending ON fixture.stale_outbox (available_at, id) WHERE status = 'pending';
CREATE INDEX IF NOT EXISTS artifact_status ON artifact.metadata (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS artifact_scan_artifact ON artifact.scan (artifact_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS audit_entity ON audit.entity_event (entity_type, entity_id, created_at);

COMMENT ON TABLE control_plane.run IS '不可变 KBD/Bundle 绑定的仿真 TestRun；不保存 Lease 明文';
COMMENT ON TABLE fixture.bundle IS '已编译 Fixture Bundle metadata；原始对象只在受控对象存储';
COMMENT ON TABLE artifact.metadata IS 'Artifact metadata/digest；禁止保存客户原始字节、URL 或命令输出';
