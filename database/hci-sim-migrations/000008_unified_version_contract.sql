-- KBD/Bundle 统一版本身份最终切换。
-- 旧 kbd_revision/scenario/revision 字段保留兼容读取；新写入必须携带 digest 身份。

ALTER TABLE control_plane.scenario
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS knowledge_snapshot_digest varchar(71);

ALTER TABLE control_plane.run
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS knowledge_release_id varchar(128),
    ADD COLUMN IF NOT EXISTS bundle_build_id varchar(128);

ALTER TABLE fixture.bundle
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS knowledge_release_id varchar(128),
    ADD COLUMN IF NOT EXISTS bundle_input_digest varchar(71),
    ADD COLUMN IF NOT EXISTS compiler_revision varchar(128);

ALTER TABLE fixture.bundle
    ADD COLUMN IF NOT EXISTS workspace_id uuid,
    ADD COLUMN IF NOT EXISTS source_knowledge_revision_no bigint;

UPDATE fixture.bundle b
SET workspace_id = md5('hci:bundle-workspace:' || s.support_id)::uuid,
    source_knowledge_revision_no = b.revision
FROM control_plane.scenario s
WHERE s.id = b.scenario_id
  AND (b.workspace_id IS NULL OR b.source_knowledge_revision_no IS NULL);

ALTER TABLE fixture.bundle
    ALTER COLUMN workspace_id SET NOT NULL,
    ALTER COLUMN source_knowledge_revision_no SET NOT NULL;

ALTER TABLE fixture.approval
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS knowledge_release_id varchar(128),
    ADD COLUMN IF NOT EXISTS trace_id varchar(64);

ALTER TABLE fixture.bundle_activation
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71),
    ADD COLUMN IF NOT EXISTS knowledge_release_id varchar(128);

ALTER TABLE fixture.asset_revision
    ADD COLUMN IF NOT EXISTS package_snapshot_digest varchar(71);

CREATE UNIQUE INDEX IF NOT EXISTS bundle_input_digest_unique
    ON fixture.bundle (bundle_input_digest)
    WHERE bundle_input_digest IS NOT NULL;
CREATE INDEX IF NOT EXISTS run_package_snapshot_idx
    ON control_plane.run (package_snapshot_digest)
    WHERE package_snapshot_digest IS NOT NULL;
CREATE INDEX IF NOT EXISTS bundle_package_snapshot_idx
    ON fixture.bundle (package_snapshot_digest)
    WHERE package_snapshot_digest IS NOT NULL;

DO $$ BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'run_unified_identity_pair') THEN
        ALTER TABLE control_plane.run ADD CONSTRAINT run_unified_identity_pair CHECK (
            (package_snapshot_digest IS NULL) = (knowledge_release_id IS NULL)
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bundle_unified_identity_pair') THEN
        ALTER TABLE fixture.bundle ADD CONSTRAINT bundle_unified_identity_pair CHECK (
            (package_snapshot_digest IS NULL) = (knowledge_release_id IS NULL)
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bundle_package_digest_format') THEN
        ALTER TABLE fixture.bundle ADD CONSTRAINT bundle_package_digest_format CHECK (
            package_snapshot_digest IS NULL OR package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'bundle_input_digest_format') THEN
        ALTER TABLE fixture.bundle ADD CONSTRAINT bundle_input_digest_format CHECK (
            bundle_input_digest IS NULL OR bundle_input_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'run_package_digest_format') THEN
        ALTER TABLE control_plane.run ADD CONSTRAINT run_package_digest_format CHECK (
            package_snapshot_digest IS NULL OR package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'scenario_package_digest_format') THEN
        ALTER TABLE control_plane.scenario ADD CONSTRAINT scenario_package_digest_format CHECK (
            package_snapshot_digest IS NULL OR package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'activation_package_digest_format') THEN
        ALTER TABLE fixture.bundle_activation ADD CONSTRAINT activation_package_digest_format CHECK (
            package_snapshot_digest IS NULL OR package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'asset_revision_package_digest_format') THEN
        ALTER TABLE fixture.asset_revision ADD CONSTRAINT asset_revision_package_digest_format CHECK (
            package_snapshot_digest IS NULL OR package_snapshot_digest ~ '^sha256:[0-9a-f]{64}$'
        );
    END IF;
END $$;

DO $$
BEGIN
    IF to_regclass('control_plane.run_outbox') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM control_plane.run_outbox WHERE status <> 'processed') THEN
            RAISE EXCEPTION 'legacy run_outbox 仍有未完成事件，拒绝退役';
        END IF;
    END IF;
    IF to_regclass('fixture.stale_outbox') IS NOT NULL THEN
        IF EXISTS (SELECT 1 FROM fixture.stale_outbox WHERE status <> 'processed') THEN
            RAISE EXCEPTION 'legacy stale_outbox 仍有未完成事件，拒绝退役';
        END IF;
    END IF;
    IF to_regclass('fixture.provenance') IS NOT NULL THEN
        IF EXISTS (
            SELECT 1
            FROM fixture.provenance p
            JOIN fixture.bundle b ON b.id = p.bundle_id
            WHERE b.compile_input IS NULL
               OR NOT (b.compile_input->'route_sources' @> jsonb_build_array(jsonb_build_object('route_id', p.route_id)))
        ) THEN
            RAISE EXCEPTION 'fixture.provenance 尚未完整回填到 compile_input.route_sources，拒绝退役';
        END IF;
    END IF;
END $$;

-- 统一 outbox 已完成历史事件迁移且上述门禁通过；以下三张表不再是事实源。
DROP TABLE IF EXISTS fixture.provenance;
DROP TABLE IF EXISTS control_plane.run_outbox;
DROP TABLE IF EXISTS fixture.stale_outbox;
DROP FUNCTION IF EXISTS control_plane.mirror_legacy_run_outbox();
DROP FUNCTION IF EXISTS control_plane.mirror_legacy_stale_outbox();

COMMENT ON COLUMN control_plane.run.package_snapshot_digest IS '新协议中的完整业务快照身份；旧 kbd_revision 仅兼容读取';
COMMENT ON COLUMN fixture.bundle.bundle_input_digest IS '规范化编译输入 digest；相同输入最多一个 BundleBuild';
COMMENT ON TABLE control_plane.outbox IS 'Run、Bundle stale、Activation 和投影重建共用的唯一可靠事件事实源';
