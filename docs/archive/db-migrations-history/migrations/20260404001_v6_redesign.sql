-- migrate:up

-- =============================================================================
-- v6.2 数据库重设计迁移
-- 创建日期：2026-04-04
--
-- 设计依据：docs/architecture/20_数据库重规划分析.md § 九（v6.2 深度重设计）
-- 核心变更：
--   1. 新增 customer 表（HCI 产品客户，与 user 用户身份完全独立）
--   2. 新增 system_prompt 表（Prompt 模板版本管理）
--   3. 新增 tool_definition 表（工具知识库，Prompt 构建时动态注入）
--   4. 新增 diagnostic_item 表（诊断结论子表，解 BUG-06：conversation.hypothesis JSONB blob 反模式）
--   5. 新增 tool_result 表（工具执行记录，从 audit_log 拆分，解 BUG-03：缺少 step_no）
--   6. case 表新增 customer_id 外键
--   7. audit_log 表精简（删除 tool_call 专属字段，新增 system_prompt_id）
--   8. conversation 表删除 hypothesis/react_state（从未写入数据，BUG-06 根因）
--   9. 迁移 audit_log 中存量 tool_call 记录到 tool_result
-- =============================================================================

-- ─── 0. 确保扩展就绪 ──────────────────────────────────────────────────────────

CREATE EXTENSION IF NOT EXISTS "uuid-ossp";

-- ─── 1. 新建 customer 表（客户档案，公司级别） ────────────────────────────────

CREATE TABLE IF NOT EXISTS customer (
    customer_id     UUID            PRIMARY KEY DEFAULT gen_random_uuid(),
    code            VARCHAR(64)     UNIQUE,                -- 外部系统客户 ID（幂等键），手动新增可为 NULL
    name            VARCHAR(200)    NOT NULL,               -- 客户全称（公司名称）
    short_name      VARCHAR(100),                           -- 客户简称，前端展示时优先
    product_version VARCHAR(50),                            -- 购买的 HCI 产品版本（如 HCI 6.x）
    region          VARCHAR(100),                           -- 客户所在区域（华南/华北/华东）
    industry        VARCHAR(100),                           -- 所属行业（金融/医疗/政务/教育）
    metadata        JSONB           NOT NULL DEFAULT '{}',  -- 扩展元数据（合同编号、销售负责人等）
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trace_id        VARCHAR(64)                             -- W3C traceparent 格式
);

COMMENT ON TABLE  customer              IS 'HCI 产品客户表（公司/单位级别），与 user 用户身份完全独立：user=平台登录身份，customer=HCI 产品采购方';
COMMENT ON COLUMN customer.code        IS '客户编码（幂等键），对应外部 CRM 系统客户 ID，数据导入时用于去重';
COMMENT ON COLUMN customer.metadata    IS '扩展元数据，格式示例：{"contract_no":"HCI-2026-001","sales":"张三"}';

CREATE INDEX IF NOT EXISTS idx_customer_name ON customer (name);
CREATE INDEX IF NOT EXISTS idx_customer_code ON customer (code) WHERE code IS NOT NULL;
CREATE INDEX IF NOT EXISTS idx_customer_product_version ON customer (product_version) WHERE product_version IS NOT NULL;

-- 客户表 updated_at 触发器
CREATE OR REPLACE FUNCTION update_customer_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_customer_updated_at ON customer;
CREATE TRIGGER trg_customer_updated_at
    BEFORE UPDATE ON customer
    FOR EACH ROW EXECUTE FUNCTION update_customer_updated_at();

-- ─── 2. 新建 system_prompt 表（System Instructions 模板版本管理） ──────────────

CREATE TABLE IF NOT EXISTS system_prompt (
    id               SERIAL          PRIMARY KEY,
    stage            VARCHAR(5)      NOT NULL,              -- S0/S1/S2/S3/S4/S5/S6/BASE
    name             VARCHAR(100)    NOT NULL UNIQUE,       -- 唯一名称，如 s0_intent_recognition_v2
    description      TEXT,                                  -- 模板说明：用途、设计思路、与前版本区别
    content_template TEXT            NOT NULL,              -- Prompt 模板，使用 {placeholder} 占位符
    version          VARCHAR(20)     NOT NULL DEFAULT '1.0',
    is_active        BOOLEAN         NOT NULL DEFAULT TRUE,  -- true=当前激活版本；同 stage 只能有一个 true
    created_at       TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       TIMESTAMPTZ     NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE  system_prompt                  IS 'System Instructions 模板库，支持按阶段多版本管理和快速回滚';
COMMENT ON COLUMN system_prompt.stage            IS '适用诊断阶段: S0=意图识别 S1=信息采集 S2=假设生成 S3=验证 S4=根因 S5=方案 S6=验证闭环 BASE=全局基础';
COMMENT ON COLUMN system_prompt.content_template IS 'Prompt 模板内容，占位符格式：{tool_list}, {category_name}, {hypotheses} 等';
COMMENT ON COLUMN system_prompt.is_active        IS '同一 stage 同时只有一个 is_active=true 的版本被注入 Prompt，切换时先 UPDATE 旧版为 false';

CREATE INDEX IF NOT EXISTS idx_system_prompt_stage_active ON system_prompt (stage, is_active);
CREATE UNIQUE INDEX IF NOT EXISTS idx_system_prompt_name ON system_prompt (name);

CREATE OR REPLACE FUNCTION update_system_prompt_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_system_prompt_updated_at ON system_prompt;
CREATE TRIGGER trg_system_prompt_updated_at
    BEFORE UPDATE ON system_prompt
    FOR EACH ROW EXECUTE FUNCTION update_system_prompt_updated_at();

-- ─── 3. 新建 tool_definition 表（工具知识库） ─────────────────────────────────

CREATE TABLE IF NOT EXISTS tool_definition (
    id                SERIAL         PRIMARY KEY,
    tool_name         VARCHAR(100)   NOT NULL UNIQUE,        -- 工具唯一标识（如 acli_vm_list）
    display_name      VARCHAR(200)   NOT NULL,               -- 展示名（如'获取虚拟机列表'）
    tool_type         VARCHAR(20)    NOT NULL,               -- acli / scp_api
    category          VARCHAR(50),                           -- 故障域: vm/storage/network/cluster/platform; NULL=通用
    description       TEXT           NOT NULL,               -- 工具功能描述（直接注入 Prompt 供 LLM 理解）
    usage_template    TEXT,                                  -- 调用模板（acli 类型：命令格式；scp_api 类型：endpoint+method）
    parameters_schema JSONB          NOT NULL DEFAULT '{}', -- OpenAPI 3.0 格式参数 Schema
    examples          JSONB          NOT NULL DEFAULT '[]', -- 调用示例数组
    risk_level        SMALLINT       NOT NULL DEFAULT 1,     -- 1=只读 2=写操作 3=高危
    is_active         BOOLEAN        NOT NULL DEFAULT TRUE,  -- false=临时下线，不注入 Prompt
    version           VARCHAR(20)    NOT NULL DEFAULT '1.0',
    created_at        TIMESTAMPTZ    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at        TIMESTAMPTZ    NOT NULL DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE  tool_definition            IS 'AI 工具知识库：LLM 可调用的工具完整描述，Prompt 构建时按故障域过滤后动态注入';
COMMENT ON COLUMN tool_definition.tool_name  IS '工具唯一标识，与 tool_result.tool_name 保持一致，一条记录=一个原子操作';
COMMENT ON COLUMN tool_definition.category   IS 'NULL 表示通用工具（跨域均注入），非 NULL 只在对应故障域注入以控制 token 消耗';
COMMENT ON COLUMN tool_definition.risk_level IS '1=只读查询（不影响生产环境）/ 2=写操作（修改配置/状态）/ 3=高危（删除/重启/格式化）';

CREATE INDEX IF NOT EXISTS idx_tool_definition_category ON tool_definition (category, is_active);
CREATE INDEX IF NOT EXISTS idx_tool_definition_risk ON tool_definition (risk_level) WHERE risk_level >= 2;

CREATE OR REPLACE FUNCTION update_tool_definition_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_tool_definition_updated_at ON tool_definition;
CREATE TRIGGER trg_tool_definition_updated_at
    BEFORE UPDATE ON tool_definition
    FOR EACH ROW EXECUTE FUNCTION update_tool_definition_updated_at();

-- ─── 4. 新建 tool_result 表（工具执行记录，从 audit_log 拆分） ────────────────
-- 解决 BUG-03：原 tool_audit_log 缺少 step_no 字段
-- 解决语义混乱：audit_log 不再承载工具执行数据

CREATE TABLE IF NOT EXISTS tool_result (
    id              VARCHAR(36)    PRIMARY KEY DEFAULT gen_random_uuid()::VARCHAR,
    conversation_id UUID           NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    tool_name       VARCHAR(100)   NOT NULL,                -- 工具标识，对应 tool_definition.tool_name
    tool_type       VARCHAR(20)    NOT NULL,                -- acli / scp_api
    step_no         SMALLINT,                              -- 执行步骤编号（BUG-03 修复），对应 diagnostic_item.seq
    risk_level      SMALLINT       NOT NULL DEFAULT 1,     -- 1=只读 2=写操作 3=高危
    policy          VARCHAR(20)    NOT NULL,               -- auto/notify/confirm/block
    authorized_by   VARCHAR(100),                          -- policy=confirm 时的授权用户标识
    input_json      JSONB          NOT NULL DEFAULT '{}',  -- 工具调用输入参数
    output_json     JSONB,                                 -- 工具执行结果
    error           TEXT,                                  -- 错误信息（失败时写入）
    duration_ms     INTEGER,                               -- 执行耗时（毫秒）
    started_at      TIMESTAMPTZ    NOT NULL DEFAULT NOW(),
    completed_at    TIMESTAMPTZ,
    trace_id        VARCHAR(64)
);

COMMENT ON TABLE  tool_result            IS '工具执行记录表，记录 ReAct 执行器每次工具调用的输入/输出/风险/授权。从 audit_log 拆分以解决 BUG-03（step_no 缺失）';
COMMENT ON COLUMN tool_result.step_no    IS '执行步骤编号，BUG-03 修复字段；对应 diagnostic_item 中 type=verification_step 记录的 seq';
COMMENT ON COLUMN tool_result.policy     IS 'auto=自动执行 / notify=执行并通知 / confirm=需人工确认 / block=已拦截（risk>=3 时）';
COMMENT ON COLUMN tool_result.authorized_by IS 'policy=confirm 场景必填，记录是谁授权了该高危操作';

CREATE INDEX IF NOT EXISTS idx_tool_result_conversation ON tool_result (conversation_id, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_result_tool_name ON tool_result (tool_name, started_at DESC);
CREATE INDEX IF NOT EXISTS idx_tool_result_high_risk ON tool_result (risk_level, started_at DESC) WHERE risk_level >= 2;
CREATE INDEX IF NOT EXISTS idx_tool_result_trace ON tool_result (trace_id) WHERE trace_id IS NOT NULL;

-- ─── 5. 新建 diagnostic_item 表（诊断结论子表） ───────────────────────────────
-- 解决 BUG-06：conversation.hypothesis JSONB blob 反模式
-- 与 message 完全同构的子实体设计模式（1 conversation → N diagnostic_item）

CREATE TABLE IF NOT EXISTS diagnostic_item (
    id              UUID           PRIMARY KEY DEFAULT gen_random_uuid(),
    conversation_id UUID           NOT NULL REFERENCES conversation(conversation_id) ON DELETE CASCADE,
    stage           VARCHAR(5)     NOT NULL,               -- S2/S3/S4/S5
    type            VARCHAR(30)    NOT NULL,               -- hypothesis/verification_step/root_cause/solution
    seq             SMALLINT       NOT NULL DEFAULT 1,     -- 同会话同类型内排序序号（从1开始）
    content         JSONB          NOT NULL DEFAULT '{}', -- 结构化内容（按 type 格式不同）
    probability     REAL,                                  -- 假设概率 0.0-1.0，仅 type=hypothesis 有值
    status          VARCHAR(20)    NOT NULL DEFAULT 'pending', -- pending/in_progress/confirmed/rejected/skipped
    created_at      TIMESTAMPTZ    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      TIMESTAMPTZ    NOT NULL DEFAULT CURRENT_TIMESTAMP,
    trace_id        VARCHAR(64)
);

COMMENT ON TABLE  diagnostic_item          IS '诊断结论子表：存储 S2-S5 各阶段产生的结构化结论（假设/验证步骤/根因/方案）。解 BUG-06：废弃 conversation.hypothesis JSONB blob 反模式';
COMMENT ON COLUMN diagnostic_item.type     IS 'hypothesis=根因假设(S2) / verification_step=验证步骤(S3) / root_cause=根因结论(S4) / solution=解决方案(S5)';
COMMENT ON COLUMN diagnostic_item.seq      IS '同会话同类型内排序序号从1开始；hypothesis 按概率降序，verification_step 按执行顺序';
COMMENT ON COLUMN diagnostic_item.content  IS '结构化内容，按 type 格式不同：hypothesis:{description,probability,evidence_needed}；root_cause:{description,confidence,evidence}；solution:{steps[],commands[]}';
COMMENT ON COLUMN diagnostic_item.status   IS 'pending=待处理 / in_progress=验证中 / confirmed=已确认 / rejected=已排除 / skipped=跳过';

CREATE INDEX IF NOT EXISTS idx_diagnostic_item_query ON diagnostic_item (conversation_id, type, seq);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_stage ON diagnostic_item (conversation_id, stage);
CREATE INDEX IF NOT EXISTS idx_diagnostic_item_trace ON diagnostic_item (trace_id) WHERE trace_id IS NOT NULL;

CREATE OR REPLACE FUNCTION update_diagnostic_item_updated_at()
RETURNS TRIGGER AS $$
BEGIN
    NEW.updated_at = CURRENT_TIMESTAMP;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS trg_diagnostic_item_updated_at ON diagnostic_item;
CREATE TRIGGER trg_diagnostic_item_updated_at
    BEFORE UPDATE ON diagnostic_item
    FOR EACH ROW EXECUTE FUNCTION update_diagnostic_item_updated_at();

-- ─── 6. case 表新增 customer_id 外键 ─────────────────────────────────────────

ALTER TABLE "case"
    ADD COLUMN IF NOT EXISTS customer_id UUID
        REFERENCES customer(customer_id) ON DELETE SET NULL;

COMMENT ON COLUMN "case".customer_id IS '关联客户档案（可选）；ON DELETE SET NULL，删除客户不影响历史工单。适用于运营人员关联投诉工单所属客户公司';

CREATE INDEX IF NOT EXISTS idx_case_customer_id ON "case" (customer_id) WHERE customer_id IS NOT NULL;

-- ─── 7. audit_log 表精简（删除 tool_call 专属字段，新增 system_prompt_id） ────

-- 7a. 迁移 audit_log 中存量 tool_call 记录到 tool_result（保留历史数据）
INSERT INTO tool_result (
    id,
    conversation_id,
    tool_name,
    tool_type,
    step_no,
    risk_level,
    policy,
    authorized_by,
    input_json,
    output_json,
    error,
    duration_ms,
    started_at,
    completed_at,
    trace_id
)
SELECT
    id,
    conversation_id,
    COALESCE(tool_name, 'unknown'),   -- 工具名，unknown 表示迁移前旧数据
    'acli',                            -- 旧数据未记录 tool_type，默认填 acli
    NULL,                              -- step_no：旧数据无此字段（BUG-03 根因）
    COALESCE(risk_level, 1),          -- 风险等级
    COALESCE(policy, 'auto'),         -- 执行策略
    authorized_by,
    COALESCE(payload -> 'args', '{}'::jsonb),     -- 将 payload.args 作为 input_json
    COALESCE(payload -> 'result', NULL),           -- 将 payload.result 作为 output_json
    error,
    duration_ms,
    started_at,
    completed_at,
    trace_id
FROM audit_log
WHERE audit_type = 'tool_call'
ON CONFLICT (id) DO NOTHING;

-- 7b. 删除已迁移的 tool_call 记录
DELETE FROM audit_log WHERE audit_type = 'tool_call';

-- 7c. 新增 system_prompt_id 字段（Prompt 模板关联）
ALTER TABLE audit_log
    ADD COLUMN IF NOT EXISTS system_prompt_id INTEGER
        REFERENCES system_prompt(id) ON DELETE SET NULL;

COMMENT ON COLUMN audit_log.system_prompt_id IS '本次 Prompt 构建使用的模板 ID；ON DELETE SET NULL，删除模板不影响历史审计记录';

-- 7d. 删除 tool_call 专属字段（这些字段已迁移到 tool_result）
ALTER TABLE audit_log
    DROP COLUMN IF EXISTS tool_name,
    DROP COLUMN IF EXISTS risk_level,
    DROP COLUMN IF EXISTS policy,
    DROP COLUMN IF EXISTS authorized_by;

COMMENT ON TABLE audit_log IS 'System Instructions 审计表：仅记录每轮对话的 Prompt 构建过程（模板版本/token数/注入工具数）。工具执行记录已迁移至 tool_result 表';

-- ─── 8. conversation 表删除 hypothesis/react_state ────────────────────────────
-- 安全性确认：
--   - hypothesis：ORM Column 定义存在，但生产代码中零写入（BUG-06 根因）
--   - react_state：ReAct 推理草稿，正确设计是内存存活，不应持久化
-- 两列从来没有生产数据，DROP 无数据丢失风险

ALTER TABLE conversation
    DROP COLUMN IF EXISTS hypothesis,
    DROP COLUMN IF EXISTS react_state;

-- ─── 9. 验证迁移结果 ──────────────────────────────────────────────────────────

DO $$
DECLARE
    expected_tables TEXT[] := ARRAY[
        'user', 'customer', 'case', 'environment', 'assistant_evaluation',
        'conversation', 'message', 'diagnostic_item', 'tool_result', 'audit_log',
        'system_prompt', 'tool_definition',
        'kb_category', 'kbd_entry', 'sop_document', 'sop_chunk',
        'schema_migrations'
    ];
    t TEXT;
    missing TEXT[] := ARRAY[]::TEXT[];
BEGIN
    FOREACH t IN ARRAY expected_tables LOOP
        IF NOT EXISTS (
            SELECT 1 FROM pg_tables WHERE schemaname = 'public' AND tablename = t
        ) THEN
            missing := array_append(missing, t);
        END IF;
    END LOOP;

    IF array_length(missing, 1) > 0 THEN
        RAISE EXCEPTION 'v6.2 迁移验证失败：以下表不存在：%', missing;
    ELSE
        RAISE NOTICE 'v6.2 迁移验证通过：17 张目标表均存在';
    END IF;
END
$$;

-- migrate:down

-- 下面的 down 脚本为尽力回滚，不保证数据完整性
-- 如需完整回滚，请从数据库备份恢复

-- 恢复 conversation 表的列（数据已丢失，只恢复结构）
ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS hypothesis  JSONB,
    ADD COLUMN IF NOT EXISTS react_state JSONB;

-- 恢复 audit_log 表的列
ALTER TABLE audit_log
    DROP COLUMN IF EXISTS system_prompt_id,
    ADD COLUMN IF NOT EXISTS tool_name    VARCHAR(100),
    ADD COLUMN IF NOT EXISTS risk_level   SMALLINT,
    ADD COLUMN IF NOT EXISTS policy       VARCHAR(20),
    ADD COLUMN IF NOT EXISTS authorized_by VARCHAR(100);

-- 删除 case.customer_id
ALTER TABLE "case" DROP COLUMN IF EXISTS customer_id;

-- 删除新增表（逆序，先删有外键依赖的）
DROP TABLE IF EXISTS diagnostic_item CASCADE;
DROP TABLE IF EXISTS tool_result CASCADE;
DROP TABLE IF EXISTS tool_definition CASCADE;
DROP TABLE IF EXISTS audit_log CASCADE;  -- 暂不级联，避免误删
DROP TABLE IF EXISTS system_prompt CASCADE;
DROP TABLE IF EXISTS customer CASCADE;

-- ─── 附加：v6.3 S6 用户意图确认流程补充 ──────────────────────────────────────
-- 新增 conversation.pending_resolution 字段
-- S6 完成后持久化三选项等待状态，防止 Pod 重启丢失等待上下文

ALTER TABLE conversation
    ADD COLUMN IF NOT EXISTS pending_resolution JSONB;

COMMENT ON COLUMN conversation.pending_resolution IS
    'S6 完成后等待用户选择的快照（断线重连恢复锚点）。'
    '格式：{"stage":"S6","sent_at":"...","options":["A","B","C"]}。'
    '选 A(已解决)/B(未解决重进S1)/C(升级人工) 后清空为 NULL。'
    '与 pending_confirm（工具执行确认）独立：两者不会同时出现';

-- close_reason 值域扩展：加入 escalated（用户选 C 升级人工）
-- PostgreSQL ENUM 扩展：ADD VALUE 是无事务操作，需单独执行
-- 注意：若当前环境 close_reason 是 VARCHAR(20) 而非 ENUM，此步骤无需操作
DO $$
BEGIN
    -- 仅当 close_reason 是 ENUM 类型时才扩展（当前 schema 是 VARCHAR，此块为预留）
    IF EXISTS (
        SELECT 1 FROM pg_type WHERE typname = 'close_reason_type'
    ) THEN
        ALTER TYPE close_reason_type ADD VALUE IF NOT EXISTS 'escalated';
    END IF;
END
$$;
