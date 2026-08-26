-- hci_sim 最小化控制面第一阶段。
--
-- 目标：
-- 1. 将 Run 与 Bundle stale 事件收敛到 control_plane.outbox；
-- 2. 保留旧 outbox 并以触发器镜像，支持滚动发布和历史事件回放；
-- 3. 将 scenario 降级为编译输入索引，Bundle 成为唯一生命周期状态源。
--
-- 本迁移不删除任何历史表。fixture.provenance、artifact.*、runtime_instance
-- 的物理删除必须在 production inventory、备份恢复演练和无调用方窗口完成后另行执行。

CREATE TABLE IF NOT EXISTS control_plane.outbox (
    id bigserial PRIMARY KEY,
    topic varchar(32) NOT NULL,
    aggregate_type varchar(32) NOT NULL,
    aggregate_id varchar(128) NOT NULL,
    run_id uuid REFERENCES control_plane.run(id) ON DELETE RESTRICT,
    event_type varchar(64) NOT NULL,
    payload_digest varchar(71) NOT NULL,
    trace_id varchar(64),
    status varchar(16) NOT NULL DEFAULT 'pending',
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    available_at timestamptz NOT NULL DEFAULT now(),
    processing_at timestamptz,
    processed_at timestamptz,
    created_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT outbox_status CHECK (status IN ('pending', 'processing', 'processed', 'failed')),
    CONSTRAINT outbox_identity UNIQUE (topic, aggregate_type, aggregate_id, event_type, payload_digest)
);

CREATE INDEX IF NOT EXISTS outbox_pending
    ON control_plane.outbox (available_at, id)
    WHERE status = 'pending';

CREATE INDEX IF NOT EXISTS outbox_run
    ON control_plane.outbox (run_id, created_at DESC)
    WHERE run_id IS NOT NULL;

-- 迁移已有 Run 事件。aggregate_id 使用对外可追踪的 external_id，避免把内部 UUID
-- 暴露给下游 webhook。
INSERT INTO control_plane.outbox
    (topic, aggregate_type, aggregate_id, run_id, event_type, payload_digest, status, attempts,
     available_at, processing_at, processed_at, created_at)
SELECT
    'run', 'run', r.external_id, legacy.run_id, legacy.event_type, legacy.payload_digest,
    legacy.status, legacy.attempts, legacy.available_at, legacy.processing_at, legacy.processed_at,
    legacy.created_at
FROM control_plane.run_outbox legacy
JOIN control_plane.run r ON r.id = legacy.run_id
ON CONFLICT (topic, aggregate_type, aggregate_id, event_type, payload_digest) DO NOTHING;

-- stale 旧表只表达“依赖变化”，并不精确对应某条 Route。先保留其原语事件；新代码会按
-- 具体 Bundle digest 写入 fixture_stale 事件。
INSERT INTO control_plane.outbox
    (topic, aggregate_type, aggregate_id, event_type, payload_digest, trace_id, status, attempts,
     available_at, processed_at, created_at)
SELECT
    'fixture_stale', 'dependency',
    legacy.dependency_type || ':' || legacy.dependency_id,
    legacy.reason_code, left(legacy.dependency_digest, 71), legacy.trace_id, legacy.status, legacy.attempts,
    legacy.available_at, legacy.processed_at, legacy.created_at
FROM fixture.stale_outbox legacy
ON CONFLICT (topic, aggregate_type, aggregate_id, event_type, payload_digest) DO NOTHING;

-- 旧 Runtime 在滚动发布期间仍可能写入两张旧表。镜像触发器确保统一消费者不会遗漏。
CREATE OR REPLACE FUNCTION control_plane.mirror_legacy_run_outbox()
RETURNS trigger
LANGUAGE plpgsql
AS $$
DECLARE
    external_run_id varchar(128);
BEGIN
    SELECT external_id INTO external_run_id FROM control_plane.run WHERE id = NEW.run_id;
    IF external_run_id IS NULL THEN
        RAISE EXCEPTION 'run_outbox references missing run %', NEW.run_id;
    END IF;
    INSERT INTO control_plane.outbox
        (topic, aggregate_type, aggregate_id, run_id, event_type, payload_digest, status, attempts,
         available_at, processing_at, processed_at, created_at)
    VALUES
        ('run', 'run', external_run_id, NEW.run_id, NEW.event_type, NEW.payload_digest, NEW.status,
         NEW.attempts, NEW.available_at, NEW.processing_at, NEW.processed_at, NEW.created_at)
    ON CONFLICT (topic, aggregate_type, aggregate_id, event_type, payload_digest) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mirror_legacy_run_outbox ON control_plane.run_outbox;
CREATE TRIGGER trg_mirror_legacy_run_outbox
AFTER INSERT ON control_plane.run_outbox
FOR EACH ROW EXECUTE FUNCTION control_plane.mirror_legacy_run_outbox();

CREATE OR REPLACE FUNCTION control_plane.mirror_legacy_stale_outbox()
RETURNS trigger
LANGUAGE plpgsql
AS $$
BEGIN
    INSERT INTO control_plane.outbox
        (topic, aggregate_type, aggregate_id, event_type, payload_digest, trace_id, status, attempts,
         available_at, processed_at, created_at)
    VALUES
        ('fixture_stale', 'dependency', NEW.dependency_type || ':' || NEW.dependency_id,
         NEW.reason_code, left(NEW.dependency_digest, 71), NEW.trace_id, NEW.status, NEW.attempts,
         NEW.available_at, NULL, NEW.created_at)
    ON CONFLICT (topic, aggregate_type, aggregate_id, event_type, payload_digest) DO NOTHING;
    RETURN NEW;
END;
$$;

DROP TRIGGER IF EXISTS trg_mirror_legacy_stale_outbox ON fixture.stale_outbox;
CREATE TRIGGER trg_mirror_legacy_stale_outbox
AFTER INSERT ON fixture.stale_outbox
FOR EACH ROW EXECUTE FUNCTION control_plane.mirror_legacy_stale_outbox();

-- Scenario 是编译输入索引，不再承载与 fixture.bundle 重复的发布生命周期。
-- 保留旧状态值是为了兼容仍在滚动中的旧 Runtime；新代码一律写 indexed。
ALTER TABLE control_plane.scenario
    DROP CONSTRAINT IF EXISTS scenario_status;

ALTER TABLE control_plane.scenario
    ADD CONSTRAINT scenario_status CHECK (
        status IN ('indexed', 'gap', 'draft', 'validated', 'approved', 'published', 'stale', 'retired')
    );

COMMENT ON COLUMN control_plane.scenario.status IS
    '已废弃的兼容字段；新写入固定为 indexed 或 gap，Bundle 生命周期以 fixture.bundle.status 为唯一事实源';
COMMENT ON TABLE control_plane.outbox IS
    '统一可靠事件投递表；Run、fixture stale 和 activation 等 topic 共享同一 claim/retry/recovery 语义';
