-- hci-sim 独立库：控制面 Bundle 生命周期持久化补列（T2 生产化接线）。
--
-- 背景：fixture.bundle 原为 Runtime 数据面同步（SyncPublishedBundles）设计，只保存
-- published 终态。controlplane.Registry 需要 draft->validated->approved->published->stale
-- 全生命周期与冻结编译输入。本迁移只加可空列，不回填、不改既有约束，
-- 对现有 GitOps 同步路径零影响（未设置新列时语义不变）。

ALTER TABLE fixture.bundle
    ADD COLUMN IF NOT EXISTS input_fingerprint varchar(71);

ALTER TABLE fixture.bundle
    ADD COLUMN IF NOT EXISTS compile_input jsonb;

ALTER TABLE fixture.bundle
    ADD COLUMN IF NOT EXISTS stale_reason varchar(256);

-- 按冻结输入查找 Bundle 的索引；不设唯一约束，唯一性由 digest 与应用层指纹检查保证。
CREATE INDEX IF NOT EXISTS bundle_input_fingerprint_idx
    ON fixture.bundle (input_fingerprint);
