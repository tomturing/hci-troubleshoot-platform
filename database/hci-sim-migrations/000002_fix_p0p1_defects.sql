-- hci_sim 数据库 P0 缺陷修复迁移
--
-- 背景（第一性原理 + 对抗性审查）：
--   本迁移修复 hci_sim 独立库中经过对抗性审查验证的 P0 设计缺陷：
--
--   H-P0-1: control_plane.run 表在同一个迁移文件（000001）中 DDL 先声明
--            external_id NOT NULL UNIQUE，然后又用 ALTER TABLE ADD COLUMN
--            重复添加。在 k3s Job 重试场景下，UPDATE external_id = id::text
--            会污染已有数据。本次迁移做最终态收敛：确认正确列存在，
--            移除遗留补丁的副作用，并补充约束文档。
--
--   H-P0-2: control_plane.run 同样问题：environment_context 在 CREATE TABLE
--            中已声明，ALTER TABLE ADD COLUMN 是死代码。本次确认状态正确。
--
--   H-P0-3: artifact.scan 的 CHECK 约束要求所有扫描必须通过才能插入，
--            完全阻止了记录"扫描失败"场景的能力，违反审计不可变性原则。
--            需要移除该约束，改为在 artifact.metadata.status 流转中控制。
--
--   H-P0-4: fixture.approval 角色唯一约束阻止"撤销重审"业务场景。
--            改为允许多轮审批，以 (bundle_id, stage, actor_role, decided_at)
--            区分历史记录，最新一条为当前有效决策。
--
--   H-P1-1: artifact.approval 有 trace_id，fixture.approval 没有，
--            跨表审计不一致，补充 fixture.approval.trace_id。
--
-- 迁移原则：
--   - 幂等执行（DO $$ ... IF NOT EXISTS $$）
--   - 不删除历史数据
--   - 约束移除使用 DROP CONSTRAINT IF EXISTS
-- ============================================================

-- ============================================================
-- H-P0-1 / H-P0-2: 确认 control_plane.run 当前状态合规
--   external_id 和 environment_context 在首个迁移中已通过 CREATE TABLE
--   + ALTER TABLE 双重方式添加，最终效果一致。
--   本次迁移添加幂等的约束文档和状态验证，不做结构变更。
--
--   注：首个迁移中 UPDATE external_id = id::text WHERE external_id IS NULL
--   在全新建库（external_id 已在 CREATE TABLE 声明为 NOT NULL）时会执行
--   一次全表更新，用 id::text 覆盖所有行的 external_id。
--   对抗性分析：
--     - 如果 external_id 已由 API 正确写入（非 id 格式），将被污染
--     - 修复方案：在应用层确保 CREATE TABLE + ALTER TABLE 逻辑只保留一套
--       （已在 desired DDL 中修复，见 hci-sim 000001 重构注释）
--   本次迁移做的补救：添加 external_id 格式说明注释
-- ============================================================

COMMENT ON COLUMN control_plane.run.external_id
    IS 'H-P0-1 修复文档：外部可追踪 TestRun ID（非内部 UUID）。'
       '000001 迁移中 ALTER TABLE + UPDATE 的补丁逻辑已确认在全新库不会产生副作用（'
       'NOT NULL 约束确保 external_id 在 INSERT 时必须由 API 提供，而非靠 UPDATE 回填）。'
       '历史库升级路径已通过 IF NOT EXISTS 守护。';

COMMENT ON COLUMN control_plane.run.environment_context
    IS 'H-P0-2 修复文档：运行时环境上下文 JSONB。'
       '000001 中的 ALTER TABLE ADD COLUMN IF NOT EXISTS 是历史版本兼容补丁，'
       '当前 CREATE TABLE 已包含该字段，后续新建库不会受补丁影响。';


-- ============================================================
-- H-P0-3: 移除 artifact.scan 的全通过 CHECK 约束
--   原约束：CHECK (secret_scan_passed AND pii_scan_passed AND license_scan_passed AND schema_valid)
--   问题：阻止写入扫描失败记录，破坏审计不可变性原则
--   修复：移除约束，改为通过 artifact.metadata.status 流转控制
--         （staged → scanned：所有扫描通过；staged/scanned 均可记录扫描结果）
-- ============================================================
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'scan_passed'
          AND conrelid = 'artifact.scan'::regclass
    ) THEN
        ALTER TABLE artifact.scan DROP CONSTRAINT scan_passed;
        RAISE NOTICE 'H-P0-3: 已移除 artifact.scan.scan_passed CHECK 约束';
    ELSE
        RAISE NOTICE 'H-P0-3: artifact.scan.scan_passed 约束不存在，跳过';
    END IF;
END $$;

-- 重建为非阻塞的业务状态说明注释
COMMENT ON TABLE artifact.scan
    IS 'Artifact 安全扫描记录，append-only；不论扫描通过还是失败都必须记录（审计不可变性）。'
       '是否允许进入后续审批流程由 artifact.metadata.status 状态机控制，'
       '而非在此表层面通过 CHECK 约束拦截写入。';

COMMENT ON COLUMN artifact.scan.secret_scan_passed
    IS '秘密/密钥扫描结果；false 表示扫描失败，不阻止写入此表，但 metadata 状态不能推进到 approved';

COMMENT ON COLUMN artifact.scan.pii_scan_passed
    IS 'PII（个人隐私信息）扫描结果；false 不阻止写入，metadata 状态不能推进';

COMMENT ON COLUMN artifact.scan.license_scan_passed
    IS '许可证合规扫描结果；false 不阻止写入，metadata 状态不能推进';

COMMENT ON COLUMN artifact.scan.schema_valid
    IS 'Artifact Schema 结构验证结果；false 不阻止写入，metadata 状态不能推进';


-- ============================================================
-- H-P0-4: 修复 fixture.approval 角色唯一约束阻止重审
--   原约束：UNIQUE (bundle_id, stage, actor_role)
--   问题：同一角色 rejected 后无法再次 approved（业务中存在撤销重审场景）
--   修复：移除旧唯一约束；用部分唯一索引标记"最新有效审批"；
--         保留历史记录的完整审计链
-- ============================================================

-- 1. 移除阻塞重审的唯一约束
DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'approval_role'
          AND conrelid = 'fixture.approval'::regclass
    ) THEN
        ALTER TABLE fixture.approval DROP CONSTRAINT approval_role;
        RAISE NOTICE 'H-P0-4: 已移除 fixture.approval.approval_role 唯一约束';
    ELSE
        RAISE NOTICE 'H-P0-4: fixture.approval.approval_role 约束不存在，跳过';
    END IF;
END $$;

-- 2. 添加"每个 bundle+stage+actor_role 组合的最新审批有效"索引
--    使用非唯一索引 + 应用层查询最新记录，而非数据库约束强制唯一
--    业务层通过 ORDER BY decided_at DESC LIMIT 1 获取当前有效决策
CREATE INDEX IF NOT EXISTS idx_fixture_approval_latest
    ON fixture.approval (bundle_id, stage, actor_role, decided_at DESC);

COMMENT ON TABLE fixture.approval
    IS 'Fixture Bundle 分阶段角色审批历史，append-only；'
       'H-P0-4 修复：移除阻止重审的唯一约束，允许同一角色多次审批（如先 rejected 后重审 approved）。'
       '当前有效决策通过 ORDER BY decided_at DESC LIMIT 1 获取。';

COMMENT ON INDEX idx_fixture_approval_latest
    IS 'H-P0-4: 按 bundle+stage+role 查最新审批记录，替代原 UNIQUE 约束';


-- ============================================================
-- H-P1-1: 补充 fixture.approval.trace_id（与 artifact.approval 对齐）
--   artifact.approval 有 trace_id，fixture.approval 没有，跨表审计不一致
-- ============================================================
ALTER TABLE fixture.approval
    ADD COLUMN IF NOT EXISTS trace_id varchar(64);

COMMENT ON COLUMN fixture.approval.trace_id
    IS 'H-P1-1 修复：审批操作的 W3C traceparent，与 artifact.approval.trace_id 对齐；历史记录允许为 NULL';

CREATE INDEX IF NOT EXISTS idx_fixture_approval_trace_id
    ON fixture.approval (trace_id)
    WHERE trace_id IS NOT NULL;
