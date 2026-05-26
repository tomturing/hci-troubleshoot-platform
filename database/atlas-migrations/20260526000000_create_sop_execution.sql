-- Atlas 迁移：创建 sop_execution 表
-- 生成时间：2026-05-26
-- 对应 desired_schema.sql 变更：新增 SOP 执行状态表
--
-- 用途：S1 阶段命中 SOP 后创建执行实例，Agent 按决策树遍历执行
-- 设计：conversation_id 唯一约束（1 个 conversation 只能有 1 个活跃 SOP 执行实例）
--
-- 幂等性：所有变更均使用 IF NOT EXISTS，可安全重复执行

-- 创建 SOP 执行状态表
CREATE TABLE IF NOT EXISTS sop_execution (
    id uuid NOT NULL DEFAULT gen_random_uuid(),
    conversation_id uuid NOT NULL,
    sop_document_id integer NOT NULL,
    current_node_id varchar(64) NOT NULL,
    status varchar(16) NOT NULL DEFAULT 'active',
    context_variables jsonb NOT NULL DEFAULT '{}'::jsonb,
    completed_steps jsonb NOT NULL DEFAULT '[]'::jsonb,
    pending_variable_name varchar(64) DEFAULT NULL,
    execution_log jsonb NOT NULL DEFAULT '[]'::jsonb,
    trace_id varchar(64),
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fk_sop_execution_conversation
        FOREIGN KEY (conversation_id) REFERENCES conversation (conversation_id) ON DELETE CASCADE,
    CONSTRAINT fk_sop_execution_sop_document
        FOREIGN KEY (sop_document_id) REFERENCES sop_document (id),
    CONSTRAINT uq_sop_execution_conversation
        UNIQUE (conversation_id),
    CONSTRAINT chk_sop_execution_status
        CHECK (status IN ('active', 'completed', 'interrupted', 'aborted')),
    CONSTRAINT sop_execution_pkey PRIMARY KEY (id)
);

-- 表注释
COMMENT ON TABLE sop_execution IS 'SOP 执行状态表 — SOP 执行引擎状态持久化，存储 Agent 执行 SOP 决策树的运行状态';
COMMENT ON COLUMN sop_execution.id IS '执行实例主键，UUID 格式';
COMMENT ON COLUMN sop_execution.conversation_id IS '关联会话，ON DELETE CASCADE；唯一约束确保一个 conversation 只有一个活跃执行实例';
COMMENT ON COLUMN sop_execution.sop_document_id IS '关联 SOP 文档 ID，FK → sop_document.id';
COMMENT ON COLUMN sop_execution.current_node_id IS '当前决策树节点 ID（对应 tree_json 中的节点标识）';
COMMENT ON COLUMN sop_execution.status IS '执行状态：active（执行中）/ completed（已完成）/ interrupted（中断等待变量）/ aborted（已中止）';
COMMENT ON COLUMN sop_execution.context_variables IS '执行上下文变量池，JSONB 格式；存储决策树遍历过程中收集的环境变量、用户输入等';
COMMENT ON COLUMN sop_execution.completed_steps IS '已完成步骤列表，JSONB 数组格式；按执行顺序记录已通过的节点';
COMMENT ON COLUMN sop_execution.pending_variable_name IS '待收集变量名；interrupted 状态时记录需要用户输入的变量名，收到变量后恢复执行';
COMMENT ON COLUMN sop_execution.execution_log IS '执行日志，JSONB 数组格式；记录每步决策的详情（节点 ID、决策结果、时间戳）';
COMMENT ON COLUMN sop_execution.trace_id IS '创建执行实例的请求 trace ID（W3C traceparent）';
COMMENT ON COLUMN sop_execution.created_at IS '执行实例创建时间（S1 阶段命中 SOP 时）';
COMMENT ON COLUMN sop_execution.updated_at IS '最后更新时间（节点推进/状态变更时），触发器自动维护';

-- 索引：SOP 文档维度统计（按文档查执行实例）
CREATE INDEX IF NOT EXISTS idx_sop_execution_sop_document_id ON sop_execution (sop_document_id);

-- 索引：活跃执行实例快速过滤（Agent 重连恢复）
CREATE INDEX IF NOT EXISTS idx_sop_execution_active ON sop_execution (status) WHERE status = 'active';