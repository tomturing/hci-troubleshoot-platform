-- KBD 与仿真资产统一版本治理：阶段一至阶段三基础表。
-- 原则：新事实只追加、历史字段只兼容读取；所有跨服务身份使用 digest，禁止裸 revision。

CREATE TABLE IF NOT EXISTS kbd_package (
    package_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    support_id varchar(20) NOT NULL UNIQUE,
    working_snapshot_digest varchar(71),
    active_release_id integer,
    workspace_version bigint NOT NULL DEFAULT 1 CHECK (workspace_version > 0),
    status varchar(20) NOT NULL DEFAULT 'draft_editing',
    trace_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT kbd_package_status_check CHECK (
        status::text = ANY (ARRAY['draft_editing'::varchar, 'published'::varchar]::text[])
    )
);

CREATE TABLE IF NOT EXISTS verification_asset (
    asset_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    asset_digest varchar(71) NOT NULL UNIQUE,
    support_id varchar(20) NOT NULL,
    signal_id varchar(128) NOT NULL,
    processing_index integer NOT NULL CHECK (processing_index >= 0),
    dataset_id varchar(128) NOT NULL,
    input_digest varchar(71) NOT NULL,
    deterministic_input jsonb NOT NULL DEFAULT '{}'::jsonb,
    ai_input jsonb NOT NULL DEFAULT '{}'::jsonb,
    raw_response_hash varchar(128),
    output_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    evidence_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    downstream_result jsonb NOT NULL DEFAULT '{}'::jsonb,
    model varchar(128) NOT NULL,
    prompt_revision varchar(128) NOT NULL,
    contract_version varchar(128) NOT NULL,
    run_id varchar(128),
    trace_id varchar(64) NOT NULL,
    result_status varchar(20) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT verification_asset_status_check CHECK (
        result_status::text = ANY (
            ARRAY['pass'::varchar, 'fail'::varchar, 'inconclusive'::varchar]::text[]
        )
    )
);

CREATE INDEX IF NOT EXISTS idx_verification_asset_support_signal
    ON verification_asset (support_id, signal_id, processing_index, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_verification_asset_trace
    ON verification_asset (trace_id);

CREATE TABLE IF NOT EXISTS package_snapshot (
    package_snapshot_id uuid PRIMARY KEY DEFAULT gen_random_uuid(),
    package_snapshot_digest varchar(71) NOT NULL UNIQUE,
    support_id varchar(20) NOT NULL,
    parent_snapshot_digest varchar(71),
    knowledge_snapshot_digest varchar(71) NOT NULL,
    signal_spec_digest varchar(71) NOT NULL,
    simulation_spec_digest varchar(71) NOT NULL,
    verification_assets jsonb NOT NULL DEFAULT '[]'::jsonb,
    prompt_revision varchar(128) NOT NULL,
    tool_contract_revision varchar(128) NOT NULL,
    policy_revision varchar(128) NOT NULL,
    compiler_revision varchar(128) NOT NULL,
    manifest_json jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_by varchar(128) NOT NULL,
    trace_id varchar(64) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT package_snapshot_parent_fk
        FOREIGN KEY (parent_snapshot_digest) REFERENCES package_snapshot(package_snapshot_digest) ON DELETE RESTRICT
);

CREATE INDEX IF NOT EXISTS idx_package_snapshot_support_created
    ON package_snapshot (support_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_package_snapshot_parent
    ON package_snapshot (parent_snapshot_digest) WHERE parent_snapshot_digest IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kbd_package_working_snapshot_fk') THEN
        ALTER TABLE kbd_package ADD CONSTRAINT kbd_package_working_snapshot_fk
            FOREIGN KEY (working_snapshot_digest) REFERENCES package_snapshot(package_snapshot_digest) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kbd_package_active_release_fk') THEN
        ALTER TABLE kbd_package ADD CONSTRAINT kbd_package_active_release_fk
            FOREIGN KEY (active_release_id) REFERENCES dynamic_resource_revision(id) ON DELETE RESTRICT;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'package_snapshot_digest_format') THEN
        ALTER TABLE package_snapshot ADD CONSTRAINT package_snapshot_digest_format CHECK (
            package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'verification_asset_digest_format') THEN
        ALTER TABLE verification_asset ADD CONSTRAINT verification_asset_digest_format CHECK (
            asset_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'verification_asset_input_digest_format') THEN
        ALTER TABLE verification_asset ADD CONSTRAINT verification_asset_input_digest_format CHECK (
            input_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'package_snapshot_component_digest_format') THEN
        ALTER TABLE package_snapshot ADD CONSTRAINT package_snapshot_component_digest_format CHECK (
            knowledge_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
            AND signal_spec_digest ~ '^sha256:[0-9a-f]{64}$'
            AND simulation_spec_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
END $$;

ALTER TABLE kbd_entry
    ADD COLUMN IF NOT EXISTS working_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS active_release_id integer;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kbd_entry_working_snapshot_fk') THEN
        ALTER TABLE kbd_entry ADD CONSTRAINT kbd_entry_working_snapshot_fk
            FOREIGN KEY (working_snapshot_digest) REFERENCES package_snapshot(package_snapshot_digest) ON DELETE SET NULL;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'kbd_entry_active_release_fk') THEN
        ALTER TABLE kbd_entry ADD CONSTRAINT kbd_entry_active_release_fk
            FOREIGN KEY (active_release_id) REFERENCES dynamic_resource_revision(id) ON DELETE SET NULL;
    END IF;
END $$;

ALTER TABLE dynamic_resource_revision
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS knowledge_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS release_id uuid;

CREATE UNIQUE INDEX IF NOT EXISTS uq_dynamic_resource_release_id
    ON dynamic_resource_revision (release_id) WHERE release_id IS NOT NULL;

ALTER TABLE dynamic_resource_active
    ADD COLUMN IF NOT EXISTS generation bigint NOT NULL DEFAULT 1,
    ADD COLUMN IF NOT EXISTS desired_revision integer,
    ADD COLUMN IF NOT EXISTS desired_checksum varchar(128);

ALTER TABLE dynamic_resource_usage_audit
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS knowledge_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS release_id uuid,
    ADD COLUMN IF NOT EXISTS bundle_digest varchar(71);

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dynamic_resource_revision_version_digest_format') THEN
        ALTER TABLE dynamic_resource_revision ADD CONSTRAINT dynamic_resource_revision_version_digest_format CHECK (
            (package_snapshot_digest IS NULL OR package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$')
            AND (knowledge_snapshot_digest IS NULL OR knowledge_snapshot_digest ~ '^sha256:[0-9a-f]{64}$')
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'dynamic_resource_usage_version_digest_format') THEN
        ALTER TABLE dynamic_resource_usage_audit ADD CONSTRAINT dynamic_resource_usage_version_digest_format CHECK (
            (package_snapshot_digest IS NULL OR package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$')
            AND (knowledge_snapshot_digest IS NULL OR knowledge_snapshot_digest ~ '^sha256:[0-9a-f]{64}$')
            AND (bundle_digest IS NULL OR bundle_digest ~ '^sha256:[0-9a-f]{64}$')
        );
    END IF;
END $$;

-- 历史 KBD 一次性收敛为可追溯快照。jsonb 文本在 PostgreSQL 内具有稳定键序，
-- 因此同一行重复执行会得到同一 digest；后续变更由应用层规范化 JSON 算法接管。
WITH source AS (
    SELECT
        e.id,
        e.support_id,
        'sha256:' || encode(digest(jsonb_build_object(
            'title', e.title,
            'problem_description', e.problem_description,
            'alert_info', e.alert_info,
            'steps_text', e.steps_text,
            'root_cause', e.root_cause,
            'solution', e.solution,
            'operational_impact', e.operational_impact,
            'is_temporary', e.is_temporary,
            'recommendations', e.recommendations,
            'signals_json', e.signals_json,
            'images_json', e.images_json
        )::text, 'sha256'), 'hex') AS knowledge_digest,
        'sha256:' || encode(digest(COALESCE(e.signals_json, '[]'::jsonb)::text, 'sha256'), 'hex') AS signal_digest,
        'sha256:' || encode(digest(jsonb_build_object('support_id', e.support_id, 'assets', '[]'::jsonb)::text, 'sha256'), 'hex') AS simulation_digest
    FROM kbd_entry e
), identity AS (
    SELECT *,
        'sha256:' || encode(digest(jsonb_build_object(
            'support_id', support_id,
            'knowledge_snapshot_digest', knowledge_digest,
            'signal_spec_digest', signal_digest,
            'simulation_spec_digest', simulation_digest,
            'prompt_revision', 'legacy-unversioned',
            'tool_contract_revision', 'legacy-unversioned',
            'policy_revision', 'legacy-unversioned',
            'compiler_revision', 'legacy-unversioned'
        )::text, 'sha256'), 'hex') AS package_digest
    FROM source
)
INSERT INTO package_snapshot (
    package_snapshot_digest, support_id, knowledge_snapshot_digest, signal_spec_digest,
    simulation_spec_digest, prompt_revision, tool_contract_revision, policy_revision,
    compiler_revision, manifest_json, created_by, trace_id
)
SELECT package_digest, support_id, knowledge_digest, signal_digest, simulation_digest,
       'legacy-unversioned', 'legacy-unversioned', 'legacy-unversioned', 'legacy-unversioned',
       jsonb_build_object('source', 'migration', 'kbd_id', id, 'content_digest', knowledge_digest),
       'system:migration', 'migration:20260901000000'
FROM identity
ON CONFLICT (package_snapshot_digest) DO NOTHING;

WITH snapshot_by_support AS (
    SELECT DISTINCT ON (support_id) support_id, package_snapshot_digest
    FROM package_snapshot
    WHERE manifest_json->>'source' = 'migration'
    ORDER BY support_id, created_at DESC
)
INSERT INTO kbd_package (support_id, working_snapshot_digest, workspace_version, status, trace_id)
SELECT e.support_id, s.package_snapshot_digest, 1,
       CASE WHEN e.status = 'published' THEN 'published' ELSE 'draft_editing' END,
       'migration:20260901000000'
FROM kbd_entry e
JOIN snapshot_by_support s ON s.support_id = e.support_id
ON CONFLICT (support_id) DO UPDATE SET
    working_snapshot_digest = COALESCE(kbd_package.working_snapshot_digest, EXCLUDED.working_snapshot_digest),
    updated_at = now();

UPDATE kbd_entry e
SET working_snapshot_digest = p.working_snapshot_digest
FROM kbd_package p
WHERE p.support_id = e.support_id AND e.working_snapshot_digest IS NULL;

-- 只为当前 active KBD 建立 KnowledgeRelease 身份；历史 revision 保持原 checksum 与契约，
-- 避免伪造其原始内容。新发布由应用在同一事务写入完整 version_identity contract。
UPDATE dynamic_resource_revision r
SET package_snapshot_digest = p.working_snapshot_digest,
    knowledge_snapshot_digest = s.knowledge_snapshot_digest,
    release_id = COALESCE(r.release_id, gen_random_uuid())
FROM dynamic_resource_active a
JOIN kbd_entry e ON e.id::text = a.resource_name
JOIN kbd_package p ON p.support_id = e.support_id
JOIN package_snapshot s ON s.package_snapshot_digest = p.working_snapshot_digest
WHERE a.resource_type = 'kbd'
  AND r.resource_type = a.resource_type
  AND r.resource_name = a.resource_name
  AND r.revision = a.active_revision;

UPDATE kbd_package p
SET active_release_id = r.id,
    status = 'published',
    updated_at = now()
FROM kbd_entry e
JOIN dynamic_resource_active a ON a.resource_type = 'kbd' AND a.resource_name = e.id::text
JOIN dynamic_resource_revision r
  ON r.resource_type = a.resource_type AND r.resource_name = a.resource_name AND r.revision = a.active_revision
WHERE p.support_id = e.support_id AND r.release_id IS NOT NULL;

UPDATE kbd_entry e
SET active_release_id = p.active_release_id
FROM kbd_package p
WHERE p.support_id = e.support_id AND p.active_release_id IS NOT NULL;

COMMENT ON TABLE kbd_package IS 'KBD 业务聚合根：工作快照、发布指针与 CAS 工作区版本';
COMMENT ON TABLE package_snapshot IS 'KBD/Signal/仿真/验证的一致业务快照；只保存 manifest 和 digest 引用';
COMMENT ON TABLE verification_set IS '不可变验证资产集合；集合变化生成新的 digest';
COMMENT ON TABLE verification_asset IS '单次试运行不可变证据；失败结果同样保留用于审计';
