-- migrate:up

-- HCI 智能排障平台 — 工具调用审计日志表迁移
-- Version: Phase 3 — ReAct 引擎工具接入
-- Date: 2026-03-23
-- 说明：新增 tool_audit_log 表，记录 ReAct 执行器每次工具调用的完整审计信息

CREATE TABLE IF NOT EXISTS tool_audit_log (
    -- 主键：由 ReactExecutor 预生成的 UUID
    id              VARCHAR(36)     PRIMARY KEY,

    -- 会话关联（对应 conversation.conversation_id）
    session_id      VARCHAR(36)     NOT NULL,

    -- 工具调用信息
    tool_name       VARCHAR(100)    NOT NULL,
    tool_args       JSONB           NULL,           -- 工具调用参数

    -- 风险控制字段
    risk_level      INTEGER         NOT NULL,       -- 1=只读, 2=写操作需确认, 3=高危
    policy          VARCHAR(20)     NOT NULL,       -- auto|notify|confirm|block
    authorized_by   VARCHAR(100)    NULL,           -- risk_level>=2 时记录确认用户

    -- 执行结果
    result          JSONB           NULL,           -- 执行结果摘要（截断到 2000 字符）
    error           TEXT            NULL,           -- 执行异常信息

    -- 时间统计
    started_at      TIMESTAMPTZ     NOT NULL,
    completed_at    TIMESTAMPTZ     NOT NULL,
    duration_ms     INTEGER         NULL,           -- 执行耗时（毫秒）

    -- 链路追踪
    trace_id        VARCHAR(64)     NULL,

    -- 记录创建时间
    created_at      TIMESTAMPTZ     NOT NULL DEFAULT NOW()
);

-- 索引：按会话 ID 查询（管理后台展示）
CREATE INDEX IF NOT EXISTS idx_tool_audit_log_session_id
    ON tool_audit_log(session_id);

-- 索引：按工具名称查询（工具使用统计）
CREATE INDEX IF NOT EXISTS idx_tool_audit_log_tool_name
    ON tool_audit_log(tool_name);

-- 索引：按链路追踪 ID 查询（问题排查）
CREATE INDEX IF NOT EXISTS idx_tool_audit_log_trace_id
    ON tool_audit_log(trace_id) WHERE trace_id IS NOT NULL;

-- 索引：按时间排序（最新记录查询）
CREATE INDEX IF NOT EXISTS idx_tool_audit_log_started_at
    ON tool_audit_log(started_at DESC);

COMMENT ON TABLE tool_audit_log IS 'ReAct 执行器工具调用审计日志，记录不可删除';
COMMENT ON COLUMN tool_audit_log.risk_level IS '风险等级：1=只读自动执行, 2=写操作需用户确认, 3=高危禁止执行';
COMMENT ON COLUMN tool_audit_log.policy IS '执行策略：auto=自动, notify=通知, confirm=确认, block=禁止';
COMMENT ON COLUMN tool_audit_log.result IS '执行结果 JSONB，截断至 2000 字符防止超大响应';


-- migrate:down
-- 不提供自动降级，手动回滚
