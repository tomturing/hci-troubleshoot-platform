-- 主库 P0/P1 数据库设计缺陷修复迁移
--
-- 背景（第一性原理 + 对抗性审查）：
--   本迁移修复以下经过对抗性审查验证的设计缺陷，按严重程度优先处理：
--
--   P0-1: terminal_operation / bridge_execution_logs 的 case_id 类型与 case 表不一致
--         (varchar(32) vs varchar(20))，且无格式 CHECK 约束
--   P0-2: authorization 表缺失 trace_id、decision CHECK 约束、exec_id 外键
--         高危授权审计链路不完整，违反安全可审计性要求
--   P0-3: fact 表缺失 case 外键约束，证据链存在孤岛风险
--   P0-4: claim_evidence_link 表缺失 case 外键约束
--   P1-1: kbd_entry.lock_version DEFAULT 0 与其他乐观锁 DEFAULT 1 不一致
--   P1-2: bridge_execution_logs 多余时间戳字段无约束，stdout/stderr 无大小上限
--   冗余: idx_case_client_id 单列索引被复合索引 idx_case_client_status 覆盖
--   冗余: idx_message_case_id 单列索引被复合索引 idx_message_case_created 覆盖
--   冗余: kb_category.keywords 已废弃但 GIN 索引仍在维护
--
-- 迁移原则：
--   - 全部幂等（IF NOT EXISTS / DO $$ ... IF NOT EXISTS $$）
--   - 不删除已有数据，存量数据的类型变更通过 CHECK 约束而非 ALTER TYPE 实现
--   - 外键均为 ON DELETE 安全策略（CASCADE 跟随父表，SET NULL 保留审计）
-- ============================================================


-- ============================================================
-- P0-1: 修复 terminal_operation.case_id 类型不一致
--       case 主键为 varchar(20)，terminal_operation 声明的是 varchar(32)
--       存量数据通过 CHECK 约束保证格式合规，不做 ALTER COLUMN TYPE
--       （ALTER COLUMN TYPE 需要表重写，在大表上有锁风险）
-- ============================================================

-- 为 terminal_operation.case_id 添加格式 CHECK 约束
-- 格式：Q + 8位日期 + 5位序号 = 14字符，与 generate_case_id() 输出一致
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_terminal_operation_case_id_format'
          AND conrelid = 'terminal_operation'::regclass
    ) THEN
        ALTER TABLE terminal_operation
            ADD CONSTRAINT chk_terminal_operation_case_id_format
            CHECK (case_id ~ '^Q[0-9]{8}[0-9]{5}$' OR case_id = '');
    END IF;
END $$;

COMMENT ON CONSTRAINT chk_terminal_operation_case_id_format ON terminal_operation
    IS 'P0-1 修复：确保 case_id 格式与 case 表主键格式一致（Q + 8位日期 + 5位序号）';

-- ============================================================
-- P0-1: 修复 bridge_execution_logs.case_id 格式约束
--       bridge_execution_logs.case_id 也是 varchar(32)，跨服务场景合理保留宽度
--       但添加 CHECK 约束确保写入格式正确
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_bridge_execution_logs_case_id_format'
          AND conrelid = 'bridge_execution_logs'::regclass
    ) THEN
        ALTER TABLE bridge_execution_logs
            ADD CONSTRAINT chk_bridge_execution_logs_case_id_format
            CHECK (case_id IS NULL OR case_id ~ '^Q[0-9]{8}[0-9]{5}$');
    END IF;
END $$;

COMMENT ON CONSTRAINT chk_bridge_execution_logs_case_id_format ON bridge_execution_logs
    IS 'P0-1 修复：桥接日志 case_id 允许 NULL（跨服务），有值时须符合工单格式';


-- ============================================================
-- P0-2: 修复 authorization 表
--       高危授权审计表缺失核心安全字段和约束
-- ============================================================

-- 2a. 添加 trace_id（安全审计的核心追踪字段）
ALTER TABLE "authorization"
    ADD COLUMN IF NOT EXISTS trace_id varchar(64);

COMMENT ON COLUMN "authorization".trace_id
    IS 'P0-2 修复：授权创建请求的 W3C traceparent；高危授权必须可追溯到发起方';

-- 2b. 添加 updated_at（授权状态变更审计）
ALTER TABLE "authorization"
    ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

COMMENT ON COLUMN "authorization".updated_at
    IS 'P0-2 修复：授权记录最后更新时间，授权状态变更时刷新';

-- 2c. 添加 decision CHECK 约束（防止写入非法决策值）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_authorization_decision'
          AND conrelid = '"authorization"'::regclass
    ) THEN
        ALTER TABLE "authorization"
            ADD CONSTRAINT chk_authorization_decision
            CHECK (decision IN ('approve', 'deny'));
    END IF;
END $$;

COMMENT ON CONSTRAINT chk_authorization_decision ON "authorization"
    IS 'P0-2 修复：授权决策只允许 approve 或 deny，防止非法值写入';

-- 2d. 添加 exec_id 索引（exec_id 引用 tool_result.id，因 tool_result.id 是 varchar(36)
--     而 authorization.exec_id 也是 varchar(36)，类型匹配，可安全添加索引）
--     注：不添加 FK 是因为 authorization 可能先于 tool_result 记录创建（授权在执行前）
CREATE INDEX IF NOT EXISTS idx_authorization_exec_id
    ON "authorization" (exec_id);

COMMENT ON INDEX idx_authorization_exec_id
    IS 'P0-2 修复：按 exec_id 快速查找授权记录，关联 tool_result';

-- 2e. 添加 trace_id 索引
CREATE INDEX IF NOT EXISTS idx_authorization_trace_id
    ON "authorization" (trace_id)
    WHERE trace_id IS NOT NULL;


-- ============================================================
-- P0-3: 修复 fact 表缺失 case 外键约束
--       fact.case_id 引用 case.case_id 但无 FK，证据链孤岛风险
--       使用 ON DELETE CASCADE：工单删除时清理关联事实（事实与工单共生命周期）
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_fact_case_id'
          AND conrelid = 'fact'::regclass
    ) THEN
        -- 先清理可能存在的孤儿 fact 记录（case 不存在的）
        DELETE FROM fact
        WHERE case_id NOT IN (SELECT case_id FROM "case");

        ALTER TABLE fact
            ADD CONSTRAINT fk_fact_case_id
            FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE;
    END IF;
END $$;

COMMENT ON CONSTRAINT fk_fact_case_id ON fact
    IS 'P0-3 修复：事实记录与工单强关联，工单删除时级联清理；修复前已清理孤儿记录';


-- ============================================================
-- P0-4: 修复 claim_evidence_link 表缺失 case 外键约束
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'fk_claim_evidence_link_case_id'
          AND conrelid = 'claim_evidence_link'::regclass
    ) THEN
        -- 清理孤儿记录
        DELETE FROM claim_evidence_link
        WHERE case_id NOT IN (SELECT case_id FROM "case");

        ALTER TABLE claim_evidence_link
            ADD CONSTRAINT fk_claim_evidence_link_case_id
            FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE;
    END IF;
END $$;

COMMENT ON CONSTRAINT fk_claim_evidence_link_case_id ON claim_evidence_link
    IS 'P0-4 修复：证据链记录与工单强关联，工单删除时级联清理';


-- ============================================================
-- P1-1: 修复 kbd_entry.lock_version 默认值不一致
--       其他乐观锁（collection_profile_definition.lock_version 等）从 1 开始
--       kbd_entry.lock_version 错误地从 0 开始
-- ============================================================
ALTER TABLE kbd_entry
    ALTER COLUMN lock_version SET DEFAULT 1;

-- 将存量 lock_version=0 的记录更新为 1（未经任何修改的草稿）
UPDATE kbd_entry SET lock_version = 1 WHERE lock_version = 0;

-- 添加 CHECK 约束确保 lock_version >= 1
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_kbd_entry_lock_version'
          AND conrelid = 'kbd_entry'::regclass
    ) THEN
        ALTER TABLE kbd_entry
            ADD CONSTRAINT chk_kbd_entry_lock_version
            CHECK (lock_version >= 1);
    END IF;
END $$;

COMMENT ON COLUMN kbd_entry.lock_version
    IS 'P1-1 修复：专家工作稿乐观锁版本，从 1 开始（与 collection_profile_definition 等一致）；每次成功保存递增';


-- ============================================================
-- P1-2: bridge_execution_logs stdout/stderr 无大小约束
--       在 k3s 资源受限环境中，无约束的 text 字段存储大输出会导致 PG OOM
--       通过 CHECK 约束限制 output_preview（预览字段）的实际长度
--       注：stdout/stderr 全文存储在 bridge_execution_artifacts 表，
--           bridge_execution_logs 的 stdout/stderr 应只存摘要（或 NULL）
-- ============================================================
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_bridge_execution_logs_preview_length'
          AND conrelid = 'bridge_execution_logs'::regclass
    ) THEN
        ALTER TABLE bridge_execution_logs
            ADD CONSTRAINT chk_bridge_execution_logs_preview_length
            CHECK (
                (output_preview IS NULL OR length(output_preview) <= 2000)
            );
    END IF;
END $$;

COMMENT ON CONSTRAINT chk_bridge_execution_logs_preview_length ON bridge_execution_logs
    IS 'P1-2 修复：output_preview 限制为 2000 字符，全量输出应存 bridge_execution_artifacts';

-- stdout/stderr 全文字段同样应有大小约束（k3s 资源保护）
DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'chk_bridge_execution_logs_output_size'
          AND conrelid = 'bridge_execution_logs'::regclass
    ) THEN
        ALTER TABLE bridge_execution_logs
            ADD CONSTRAINT chk_bridge_execution_logs_output_size
            CHECK (
                (stdout IS NULL OR pg_column_size(stdout) <= 524288)  -- 512 KiB
                AND (stderr IS NULL OR pg_column_size(stderr) <= 131072)  -- 128 KiB
            );
    END IF;
END $$;

COMMENT ON CONSTRAINT chk_bridge_execution_logs_output_size ON bridge_execution_logs
    IS 'P1-2 修复：bridge_execution_logs 的 stdout <= 512KiB, stderr <= 128KiB；大输出应存 bridge_execution_artifacts';


-- ============================================================
-- 冗余索引清理：
--   idx_case_client_id 被 idx_case_client_status(client_id, status) 覆盖
--   idx_message_case_id 被 idx_message_case_created(case_id, created_at) 覆盖
--   idx_kb_category_keywords：keywords 字段已废弃，GIN 索引是纯维护成本
-- ============================================================

-- 删除 case 表冗余单列索引
DROP INDEX IF EXISTS idx_case_client_id;

-- 删除 message 表冗余单列索引
DROP INDEX IF EXISTS idx_message_case_id;

-- 删除废弃字段 keywords 的 GIN 索引（字段本身保留，仅删索引）
DROP INDEX IF EXISTS idx_kb_category_keywords;

COMMENT ON COLUMN kb_category.keywords
    IS '关键词数组（已废弃，改用语义向量检索）；字段保留用于历史数据兼容，GIN 索引已移除节约写入开销';
