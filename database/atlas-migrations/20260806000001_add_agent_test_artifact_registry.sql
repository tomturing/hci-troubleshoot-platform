-- Atlas schema diff 从 C1 desired schema 到 C2 desired schema 生成后，按项目幂等规则
-- 补充 IF NOT EXISTS。该变更只保存 metadata/digest，禁止保存原始客户 Artifact。

ALTER TABLE agent_test_fixture_bundle
    ADD COLUMN IF NOT EXISTS object_digest varchar(71) NOT NULL,
    ADD COLUMN IF NOT EXISTS version integer NOT NULL DEFAULT 1;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'agent_test_fixture_bundle_version_check'
    ) THEN
        ALTER TABLE agent_test_fixture_bundle
            ADD CONSTRAINT agent_test_fixture_bundle_version_check CHECK (version > 0);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS agent_test_artifact (
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
    CONSTRAINT ck_agent_test_artifact_status CHECK (status IN ('staged', 'scanned', 'approved', 'revoked'))
);

CREATE TABLE IF NOT EXISTS agent_test_artifact_scan (
    id bigserial PRIMARY KEY,
    artifact_id varchar(128) NOT NULL REFERENCES agent_test_artifact(id) ON DELETE RESTRICT,
    scanner_revision varchar(128) NOT NULL,
    secret_scan_passed boolean NOT NULL,
    pii_scan_passed boolean NOT NULL,
    license_scan_passed boolean NOT NULL,
    schema_valid boolean NOT NULL,
    trace_id varchar(64),
    scanned_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_test_artifact_scan_passed CHECK (secret_scan_passed AND pii_scan_passed AND license_scan_passed AND schema_valid)
);

CREATE TABLE IF NOT EXISTS agent_test_artifact_approval (
    id bigserial PRIMARY KEY,
    artifact_id varchar(128) NOT NULL REFERENCES agent_test_artifact(id) ON DELETE RESTRICT,
    actor_id varchar(128) NOT NULL,
    actor_role varchar(32) NOT NULL,
    decision varchar(16) NOT NULL,
    comment text NOT NULL DEFAULT '',
    trace_id varchar(64),
    decided_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_agent_test_artifact_approval_role CHECK (actor_role IN ('expert', 'security')),
    CONSTRAINT ck_agent_test_artifact_approval_decision CHECK (decision IN ('approved', 'rejected')),
    CONSTRAINT uq_agent_test_artifact_approval_role UNIQUE (artifact_id, actor_role),
    CONSTRAINT uq_agent_test_artifact_approval_actor UNIQUE (artifact_id, actor_id)
);

CREATE TABLE IF NOT EXISTS agent_test_fixture_stale_outbox (
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
    CONSTRAINT ck_agent_test_fixture_stale_outbox_status CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
    CONSTRAINT uq_agent_test_fixture_stale_outbox_dependency UNIQUE (dependency_type, dependency_id, dependency_revision, dependency_digest, reason_code)
);

CREATE INDEX IF NOT EXISTS idx_agent_test_artifact_status
    ON agent_test_artifact (status, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_test_artifact_scan_artifact
    ON agent_test_artifact_scan (artifact_id, scanned_at DESC);
CREATE INDEX IF NOT EXISTS idx_agent_test_fixture_stale_outbox_pending
    ON agent_test_fixture_stale_outbox (available_at, id) WHERE status = 'pending';

COMMENT ON TABLE agent_test_artifact IS 'hci-sim Artifact 的不可变 metadata；不得保存原始客户字节、URL 或命令输出';
COMMENT ON TABLE agent_test_fixture_stale_outbox IS 'Bundle 依赖变化的持久化 stale 事件；reconciliation 可安全重放';
