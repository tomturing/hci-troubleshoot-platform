-- hci-sim Bundle 热激活指针。
--
-- GitOps 只负责 Runtime 基线与安全配置；Bundle 发布后的 active digest
-- 由控制面通过该指针驱动 Runtime 原子切换。Bundle 对象本身仍不可变，
-- 该表只保存当前期望/确认的指针和最后一次调用链，不保存 Manifest 内容。

CREATE TABLE IF NOT EXISTS fixture.bundle_activation (
    support_id varchar(20) PRIMARY KEY,
    desired_digest varchar(71) NOT NULL REFERENCES fixture.bundle(digest) ON DELETE RESTRICT,
    active_digest varchar(71) REFERENCES fixture.bundle(digest) ON DELETE RESTRICT,
    previous_digest varchar(71) REFERENCES fixture.bundle(digest) ON DELETE RESTRICT,
    generation bigint NOT NULL DEFAULT 1 CHECK (generation > 0),
    status varchar(16) NOT NULL DEFAULT 'pending',
    requested_by varchar(128) NOT NULL,
    runtime_id varchar(128) NOT NULL DEFAULT '',
    trace_id varchar(64) NOT NULL,
    failure_code varchar(64) NOT NULL DEFAULT '',
    failure_message varchar(512) NOT NULL DEFAULT '',
    requested_at timestamptz NOT NULL DEFAULT now(),
    acknowledged_at timestamptz,
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT bundle_activation_status CHECK (status IN ('pending', 'active', 'failed')),
    CONSTRAINT bundle_activation_active_consistency CHECK (
        (status = 'active' AND active_digest IS NOT NULL AND acknowledged_at IS NOT NULL)
        OR (status IN ('pending', 'failed'))
    )
);

CREATE INDEX IF NOT EXISTS bundle_activation_pending
    ON fixture.bundle_activation (status, updated_at)
    WHERE status IN ('pending', 'failed');

COMMENT ON TABLE fixture.bundle_activation IS '每个 support_id 的 Bundle active pointer；对象不可变，激活可热切换且可回滚';
