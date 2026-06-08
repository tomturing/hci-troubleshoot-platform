-- ============================================================
-- 迁移：优化 tool_result 表以支持工具事务化，并创建 authorization 表
-- Version : 20260608
-- Issue   : T-RELIABILITY-P1
-- 背景    : 直接增量优化 tool_result 代替新增 tool_execution 表，契合 G-4 规范
-- ============================================================

-- ── UP ───────────────────────────────────────────────────────────────
-- 步骤 1：创建 authorization 表
CREATE TABLE IF NOT EXISTS "authorization" (
    auth_id varchar(36) NOT NULL,
    exec_id varchar(36) NOT NULL,
    actor varchar(100) NOT NULL,
    decision varchar(20) NOT NULL, -- approve/deny
    tool_input_hash varchar(64) NOT NULL,
    expires_at timestamptz,
    created_at timestamptz DEFAULT now(),
    CONSTRAINT authorization_pkey PRIMARY KEY (auth_id)
);

COMMENT ON TABLE "authorization" IS '高危操作人工授权审计表 — 记录每次高危工具调用的人工确认结果';
COMMENT ON COLUMN "authorization".auth_id IS '授权记录 ID，UUID 格式';
COMMENT ON COLUMN "authorization".exec_id IS '关联工具执行记录 ID (对应 tool_result.id)';
COMMENT ON COLUMN "authorization".actor IS '执行授权确认操作的用户名';
COMMENT ON COLUMN "authorization".decision IS '授权决策：approve=批准执行 / deny=拒绝执行';
COMMENT ON COLUMN "authorization".tool_input_hash IS '被授权工具调用输入参数的哈希值，防篡改校验';
COMMENT ON COLUMN "authorization".expires_at IS '授权过期时间，超期未执行则失效';
COMMENT ON COLUMN "authorization".created_at IS '授权创建时间';

-- 步骤 2：在 tool_result 中增量添加字段
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS status varchar(30) NOT NULL DEFAULT 'committed';
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS input_hash varchar(64);
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS authorization_id varchar(36);
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS idempotency_key varchar(100);
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS case_id varchar(20);
ALTER TABLE tool_result ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

-- 步骤 3：添加外键约束
ALTER TABLE tool_result ADD CONSTRAINT fk_tool_result_authorization_id 
    FOREIGN KEY (authorization_id) REFERENCES "authorization" (auth_id) ON DELETE SET NULL;

-- 步骤 4：添加字段注释
COMMENT ON COLUMN tool_result.status IS '工具执行状态：proposed/executing/committed/failed/cancelled等';
COMMENT ON COLUMN tool_result.input_hash IS '工具调用输入参数的哈希值';
COMMENT ON COLUMN tool_result.authorization_id IS '关联高危授权表记录ID';
COMMENT ON COLUMN tool_result.idempotency_key IS '用于防重幂等校验的键';
COMMENT ON COLUMN tool_result.case_id IS '关联工单 ID，方便直接过滤';
COMMENT ON COLUMN tool_result.updated_at IS '记录更新时间';

-- ── DOWN ─────────────────────────────────────────────────────────────
-- 步骤 1：删除外键约束
ALTER TABLE tool_result DROP CONSTRAINT IF EXISTS fk_tool_result_authorization_id;

-- 步骤 2：删除新增的字段
ALTER TABLE tool_result DROP COLUMN IF EXISTS status;
ALTER TABLE tool_result DROP COLUMN IF EXISTS input_hash;
ALTER TABLE tool_result DROP COLUMN IF EXISTS authorization_id;
ALTER TABLE tool_result DROP COLUMN IF EXISTS idempotency_key;
ALTER TABLE tool_result DROP COLUMN IF EXISTS case_id;
ALTER TABLE tool_result DROP COLUMN IF EXISTS updated_at;

-- 步骤 3：删除 authorization 表
DROP TABLE IF EXISTS "authorization";
