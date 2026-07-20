-- ============================================================
-- 数据迁移：新增 terminal_bridge 结构化日志回采表
-- Version : 005
-- Issue   : OBS-TERMINAL-BRIDGE-001
-- 说明    : terminal_bridge 执行日志（含 SSH 会话生命周期、命令执行成败、trace_id /
--           custom_ui / case_id 标签）统一回采落库，供端到端可观测性分析与工单复盘。
--          表结构需与 database/desired_schema.sql 的声明式 Schema 保持一致。
-- 幂等    : CREATE TABLE IF NOT EXISTS，可安全重复执行。
-- ============================================================

CREATE TABLE IF NOT EXISTS bridge_execution_logs (
    id          bigserial PRIMARY KEY,
    case_id     varchar(32),
    trace_id    varchar(64),
    custom_ui   varchar(255),
    user_id     varchar(64),
    node_ip     varchar(64),
    level       varchar(16) NOT NULL,
    event       varchar(64),
    message     text,
    extra       jsonb,
    created_at  timestamptz DEFAULT CURRENT_TIMESTAMP
);

COMMENT ON TABLE bridge_execution_logs IS 'terminal_bridge 结构化执行日志回采表 — 统一收集 SSH 会话生命周期与命令执行结果，关联工单与端到端 trace';
COMMENT ON COLUMN bridge_execution_logs.case_id IS '工单 ID，关联 case 表（非外键，工单可能跨服务）';
COMMENT ON COLUMN bridge_execution_logs.trace_id IS '端到端链路 ID（Custom-UI → Bridge → Agent 统一），用于问题溯源';
COMMENT ON COLUMN bridge_execution_logs.custom_ui IS '来源 Custom-UI 标识（按连接 Origin 自动归属，如 hci.local / acli.sangfor.com.cn:4443）';
COMMENT ON COLUMN bridge_execution_logs.user_id IS '触发回采的用户会话 ID（真实 Session 鉴权提取），用于审计';
COMMENT ON COLUMN bridge_execution_logs.node_ip IS '目标节点 IP（多节点路由）';
COMMENT ON COLUMN bridge_execution_logs.level IS '日志级别：INFO / WARNING / ERROR';
COMMENT ON COLUMN bridge_execution_logs.event IS '结构化事件名（如 ssh.connected / exec.start / exec.done / exec.session_missing）';
COMMENT ON COLUMN bridge_execution_logs.extra IS '附加结构化上下文（exec_id / key / exit_code / timeout 等）';

CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_case_time ON bridge_execution_logs (case_id, created_at);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_trace ON bridge_execution_logs (trace_id);
CREATE INDEX IF NOT EXISTS idx_bridge_execution_logs_custom_ui ON bridge_execution_logs (custom_ui);
