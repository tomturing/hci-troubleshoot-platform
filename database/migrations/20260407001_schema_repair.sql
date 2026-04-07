-- migrate:up

-- =============================================================================
-- Schema 修复迁移 — 补齐 ORM 模型与实际 DB 之间的所有漂移
-- 日期：2026-04-07
-- 根因：schema_migrations 记录了 8 次迁移已执行，但实际 DDL 未生效
--       （DB 可能从早期备份恢复，migration 记录被带入但 DDL 未重放）
--
-- 修复内容：
--   §1  新建 customer 表（case.customer_id FK 目标）
--   §2  case 表补列（close_reason, customer_id）
--   §3  conversation 表补列（diagnostic 系列 + pending_resolution）
--   §4  assistant_evaluation 表补列（评分评价体系）
--   §5  新建 tool_result（v6.2 工具执行记录）
--   §6  新建 diagnostic_item（v6.2 诊断子表）
--
-- 注意：
--   本迁移不创建废弃表（kb_document/kb_chunk/kb_sop_node/kb_synonym/
--   raw_cases/knowledge_atoms/prompt_audit），这些表已在
--   20260402003_drop_deprecated_tables.sql 中删除。
--
-- 幂等保证：
--   - 所有 CREATE TABLE 使用 IF NOT EXISTS
--   - 所有 ALTER TABLE ADD COLUMN 使用 IF NOT EXISTS
--   - FK / CHECK 约束通过 DO $$ 块检测后添加
--   - 全量索引使用 IF NOT EXISTS
--   新环境 / 已修复环境可安全重复执行
-- =============================================================================

-- ============================================================================
-- §0. 确保基础扩展和函数存在（防御性，正常环境 init_schema 已创建）
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";
CREATE EXTENSION IF NOT EXISTS "pg_trgm";
CREATE EXTENSION IF NOT EXISTS "vector";

CREATE OR REPLACE FUNCTION update_updated_at_column()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

-- ============================================================================
-- §1. 新建 customer 表
-- ============================================================================

CREATE TABLE IF NOT EXISTS customer (
    customer_id   UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    code          VARCHAR(64)  UNIQUE,                          -- 外部系统客户 ID（幂等键）
    name          VARCHAR(200) NOT NULL,                        -- 客户全称
    short_name    VARCHAR(100),                                 -- 客户简称
    product_version VARCHAR(50),                                -- HCI 产品版本
    region        VARCHAR(100),                                 -- 所在区域
    industry      VARCHAR(100),                                 -- 所属行业
    metadata      JSONB NOT NULL DEFAULT '{}'::jsonb,           -- 扩展元数据
    created_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    updated_at    TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
    trace_id      VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_customer_code ON customer(code);
CREATE INDEX IF NOT EXISTS idx_customer_name ON customer(name);
CREATE INDEX IF NOT EXISTS idx_customer_product_version ON customer(product_version);

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_trigger WHERE tgname = 'update_customer_updated_at'
    ) THEN
        CREATE TRIGGER update_customer_updated_at
            BEFORE UPDATE ON customer
            FOR EACH ROW EXECUTE FUNCTION update_updated_at_column();
    END IF;
END $$;

COMMENT ON TABLE customer IS '客户档案表 — HCI 产品采购方（公司/单位级别），与 user 独立';

-- ============================================================================
-- §2. case 表补列
-- ============================================================================

-- 2.1 close_reason：工单关闭原因（被动信号轨道核心数据）
ALTER TABLE "case"
    ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20);

-- 添加 CHECK 约束（包含 v6.3 新增的 escalated / s0_classification_failed）
DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = '"case"'::regclass
          AND contype = 'c'
          AND pg_get_constraintdef(oid) LIKE '%close_reason%'
    ) THEN
        ALTER TABLE "case" ADD CONSTRAINT case_close_reason_check
            CHECK (close_reason IN (
                'user_command', 'timeout', 'abandon', 'admin_close',
                'escalated', 's0_classification_failed'
            ));
    END IF;
END $$;

COMMENT ON COLUMN "case".close_reason IS '工单关闭原因：user_command/timeout/abandon/admin_close/escalated/s0_classification_failed';

-- 2.2 customer_id：关联客户档案（可选，ON DELETE SET NULL）
ALTER TABLE "case"
    ADD COLUMN IF NOT EXISTS customer_id UUID;

DO $$ BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint WHERE conname = 'case_customer_id_fkey'
    ) THEN
        ALTER TABLE "case"
            ADD CONSTRAINT case_customer_id_fkey
            FOREIGN KEY (customer_id) REFERENCES customer(customer_id)
            ON DELETE SET NULL;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_case_customer_id ON "case"(customer_id);

COMMENT ON COLUMN "case".customer_id IS '关联客户档案（可选），用于按客户维度聚合工单';

-- ============================================================================
-- §3. conversation 表补列
-- ============================================================================

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER NOT NULL DEFAULT 0;

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS diagnostic_stage VARCHAR(8) NOT NULL DEFAULT 'S0';

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS category_l1 VARCHAR(100);

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS category_l2 VARCHAR(100);

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS category_id VARCHAR(32);

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS pending_confirm JSONB;

-- v6.3 新增：S6 验证闭环后等待用户选择的快照（A=已解决/B=未解决/C=升级人工）
ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS pending_resolution JSONB;

CREATE INDEX IF NOT EXISTS idx_conversation_diagnostic_stage ON conversation(diagnostic_stage);

COMMENT ON COLUMN conversation.diagnostic_stage IS 'P4 诊断阶段: S0→S1→S2→S3→S4→S5→S6';
COMMENT ON COLUMN conversation.pending_confirm IS '待用户确认的工具调用（S3/S5 高危工具），断线重连恢复锚点';
COMMENT ON COLUMN conversation.pending_resolution IS 'S6 验证闭环后等待用户选择 A/B/C 的快照';

-- ============================================================================
-- §4. assistant_evaluation 表补列
-- ============================================================================

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS close_reason VARCHAR(20);

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS session_duration_sec INTEGER;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS repeat_question_count INTEGER;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS composite_score SMALLINT;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS score_breakdown JSONB;

ALTER TABLE assistant_evaluation
    ADD COLUMN IF NOT EXISTS calculated_at TIMESTAMPTZ;

CREATE INDEX IF NOT EXISTS idx_eval_composite_score
    ON assistant_evaluation(composite_score) WHERE composite_score IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_eval_close_reason
    ON assistant_evaluation(close_reason) WHERE close_reason IS NOT NULL;

COMMENT ON COLUMN assistant_evaluation.composite_score IS '综合质量分 0-100，QualityScoreService 计算';
COMMENT ON COLUMN assistant_evaluation.score_breakdown IS '各维度分解：{"close_intent":90,"efficiency":70,"user_rating":80,"ai_quality":65}';

-- ============================================================================
-- §5. 新建 tool_result 表（v6.2 — 替代已废弃的 tool_audit_log）
-- ============================================================================

CREATE TABLE IF NOT EXISTS tool_result (
    id                VARCHAR(36)   PRIMARY KEY,
    conversation_id   UUID          NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    tool_name         VARCHAR(100)  NOT NULL,
    tool_type         VARCHAR(20)   NOT NULL,
    step_no           SMALLINT,                                    -- BUG-03 修复：关联 diagnostic_item.seq
    risk_level        SMALLINT      NOT NULL DEFAULT 1,
    policy            VARCHAR(20)   NOT NULL,
    authorized_by     VARCHAR(100),
    input_json        JSONB         NOT NULL DEFAULT '{}',
    output_json       JSONB,
    error             TEXT,
    started_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    completed_at      TIMESTAMPTZ,
    duration_ms       INTEGER,
    trace_id          VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_tool_result_conversation ON tool_result(conversation_id);
CREATE INDEX IF NOT EXISTS idx_tool_result_tool_name    ON tool_result(tool_name);
CREATE INDEX IF NOT EXISTS idx_tool_result_trace_id     ON tool_result(trace_id) WHERE trace_id IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_tool_result_started_at   ON tool_result(started_at DESC);

COMMENT ON TABLE tool_result IS 'ReAct 工具执行记录（v6.2，替代 tool_audit_log），含 step_no 追踪';

-- ============================================================================
-- §6. 新建 diagnostic_item 表（v6.2 — 解 BUG-06 hypothesis blob 问题）
-- ============================================================================

CREATE TABLE IF NOT EXISTS diagnostic_item (
    id                UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id   UUID          NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    stage             VARCHAR(5)    NOT NULL,                      -- S2/S3/S4/S5
    type              VARCHAR(30)   NOT NULL,                      -- hypothesis/verification_step/root_cause/solution
    seq               SMALLINT      NOT NULL DEFAULT 1,
    content           JSONB         NOT NULL DEFAULT '{}',
    probability       FLOAT,                                       -- 仅 type=hypothesis
    status            VARCHAR(20)   NOT NULL DEFAULT 'pending',   -- pending/in_progress/confirmed/rejected/skipped/archived
    created_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    updated_at        TIMESTAMPTZ   NOT NULL DEFAULT NOW(),
    trace_id          VARCHAR(64)
);

CREATE INDEX IF NOT EXISTS idx_diagnostic_item_conversation ON diagnostic_item(conversation_id);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_type         ON diagnostic_item(type);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_status       ON diagnostic_item(status);

COMMENT ON TABLE diagnostic_item IS '诊断结论子表（v6.2）— 替代 conversation.hypothesis JSONB blob';

-- ============================================================================
-- §7. 保留 schema_migrations 历史记录
-- ============================================================================
-- 不在 dbmate migration 中直接 DELETE schema_migrations 记录。
-- 20260401002 若与当前部署内容存在不一致，应通过对齐迁移目录/ConfigMap
-- 或调整 baseline 脚本解决，避免后续 dbmate status/up 反复将其识别为未执行。

-- ============================================================================
-- 完成
-- ============================================================================

SELECT 'Schema repair migration completed — '
    || (SELECT COUNT(*) FROM information_schema.tables WHERE table_schema = 'public' AND table_type = 'BASE TABLE')
    || ' tables now in database'
    AS status;


-- migrate:down
-- 修复迁移不提供自动降级。
-- 如需回滚，请根据本文件中的变更逐项手动逆向处理：
-- 1) 删除本迁移新增的索引、外键、列、表；
-- 2) 评估并保留/清理已写入的新结构数据，避免误删现网数据；
-- 3) 如需恢复 schema_migrations 记录，请在确认版本来源后再手动补回。
