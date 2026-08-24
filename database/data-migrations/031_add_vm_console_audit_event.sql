-- qkv_vm_console append-only 审计事件流（设计文档 §10.1）。
-- 覆盖：请求、目标校验、基线截图、上传、质量判定、确认请求、确认/拒绝、
--       唤醒、重截、识图、查看制品、删除/过期。detail 只存哈希与元数据。
-- 幂等：CREATE TABLE IF NOT EXISTS + 索引 IF NOT EXISTS。

CREATE TABLE IF NOT EXISTS vm_console_audit_event (
    event_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    capture_id uuid REFERENCES vm_console_capture (capture_id) ON DELETE CASCADE,
    tenant_id varchar(64),
    case_id varchar(32),
    conversation_id uuid,
    mode varchar(16) NOT NULL DEFAULT 'online',
    event_type varchar(48) NOT NULL,
    actor varchar(128),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    trace_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT ck_vm_console_audit_event_type CHECK (
        (event_type)::text = ANY (
            (ARRAY['requested'::varchar, 'target_verified'::varchar, 'target_rejected'::varchar,
                   'baseline_capturing'::varchar, 'baseline_captured'::varchar, 'upload_completed'::varchar,
                   'quality_checked'::varchar, 'wake_confirm_requested'::varchar, 'wake_confirmed'::varchar,
                   'wake_declined'::varchar, 'wake_timed_out'::varchar, 'waking'::varchar,
                   'recaptured'::varchar, 'vision_completed'::varchar, 'artifact_read'::varchar,
                   'failed'::varchar, 'deleted'::varchar, 'expired'::varchar])::text[]
        )
    ),
    CONSTRAINT ck_vm_console_audit_mode
        CHECK ((mode)::text = ANY ((ARRAY['online'::varchar, 'offline'::varchar])::text[]))
);

CREATE INDEX IF NOT EXISTS idx_vm_console_audit_capture ON vm_console_audit_event (capture_id, created_at);
CREATE INDEX IF NOT EXISTS idx_vm_console_audit_case ON vm_console_audit_event (case_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_vm_console_audit_type ON vm_console_audit_event (event_type);
