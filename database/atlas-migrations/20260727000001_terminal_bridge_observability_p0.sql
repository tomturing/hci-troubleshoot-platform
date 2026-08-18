-- Terminal Bridge P0：幂等日志、受控 Artifact 与 Agent 调优关联字段。
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS exec_id varchar(64);
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS artifact_id uuid;
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS output_sha256 varchar(64);
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS error_type varchar(64);
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS bridge_trace_id varchar(64);
CREATE INDEX IF NOT EXISTS idx_tool_result_exec_id ON tool_result (exec_id) WHERE exec_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_result_artifact_id ON tool_result (artifact_id) WHERE artifact_id IS NOT NULL;

ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS event_id uuid;
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS bridge_instance_id varchar(128);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS seq bigint;
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS event_time timestamptz;
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS observed_time timestamptz DEFAULT CURRENT_TIMESTAMP;
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS span_id varchar(16);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS trace_flags varchar(2);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS conversation_id uuid;
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS tool_call_id varchar(128);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS service_name varchar(128);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS service_version varchar(64);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS deployment_environment varchar(32);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS command_sha256 varchar(64);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS stdout_sha256 varchar(64);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS stderr_sha256 varchar(64);
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS stdout_truncated boolean;
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS stderr_truncated boolean;
ALTER TABLE bridge_execution_logs ADD COLUMN IF NOT EXISTS artifact_id uuid;

CREATE UNIQUE INDEX IF NOT EXISTS uq_bridge_execution_logs_event_id
    ON bridge_execution_logs (event_id) WHERE event_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS uq_bridge_execution_logs_instance_seq
    ON bridge_execution_logs (bridge_instance_id, seq)
    WHERE bridge_instance_id IS NOT NULL AND seq IS NOT NULL;

CREATE TABLE IF NOT EXISTS bridge_execution_artifacts (
    artifact_id uuid PRIMARY KEY,
    exec_id varchar(64) NOT NULL UNIQUE,
    case_id varchar(32),
    conversation_id uuid,
    tool_name varchar(100),
    trace_id varchar(64),
    node_ip varchar(64),
    container varchar(128),
    command_redacted text,
    stdout text,
    stderr text,
    exit_code integer,
    stdout_bytes bigint NOT NULL DEFAULT 0,
    stderr_bytes bigint NOT NULL DEFAULT 0,
    stdout_sha256 varchar(64),
    stderr_sha256 varchar(64),
    stdout_truncated boolean NOT NULL DEFAULT false,
    stderr_truncated boolean NOT NULL DEFAULT false,
    duration_ms bigint,
    timed_out boolean NOT NULL DEFAULT false,
    cancelled boolean NOT NULL DEFAULT false,
    status varchar(32) NOT NULL,
    error_type varchar(64),
    access_classification varchar(32) NOT NULL DEFAULT 'restricted',
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    expires_at timestamptz
);
CREATE INDEX IF NOT EXISTS idx_bridge_artifacts_trace_id
    ON bridge_execution_artifacts (trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_bridge_artifacts_case_created
    ON bridge_execution_artifacts (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_bridge_artifacts_expires_at
    ON bridge_execution_artifacts (expires_at) WHERE expires_at IS NOT NULL;
