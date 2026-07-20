-- ============================================================
-- 数据迁移：terminal_bridge 执行日志回采表
-- Version : 005
-- Issue   : OBS-TERMINAL-BRIDGE-001
-- 说明    : terminal_bridge 将全部执行日志结构化为 JSON，经 Custom-UI 浏览器
--           统一回采到本表，按工单(case_id)与端到端链路(trace_id)关联，
--           供平台统一分析与排障（见工单 Q2026071923606 复盘）。
-- 幂等    : CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS，可重复执行。
-- ============================================================

CREATE TABLE IF NOT EXISTS bridge_execution_logs (
    id          BIGSERIAL PRIMARY KEY,
    case_id     TEXT          NOT NULL,                  -- 关联工单 ID
    trace_id    TEXT,                                     -- 端到端链路 ID（Custom-UI→Bridge→Agent 统一）
    custom_ui   TEXT,                                     -- 来源 Custom-UI（自动按 Origin 关联，如 hci.local / acli.sangfor.com.cn:4443）
    node_ip     TEXT,                                     -- 目标节点 IP（多节点路由）
    level       TEXT          NOT NULL DEFAULT 'INFO',    -- 日志级别 INFO/WARNING/ERROR
    event       TEXT,                                     -- 事件名（如 exec.start / exec.output / exec.done）
    message     TEXT,                                     -- 人类可读消息
    extra       JSONB,                                    -- 结构化附加字段（命令/输出预览/退出码等）
    user_id     VARCHAR(36),                              -- P1-4: 操作用户 ID（关联用户身份）
    created_at  TIMESTAMPTZ  NOT NULL DEFAULT now()
);

-- P1-4: 增加索引优化查询性能
CREATE INDEX IF NOT EXISTS idx_bridge_logs_case_id    ON bridge_execution_logs (case_id);
CREATE INDEX IF NOT EXISTS idx_bridge_logs_trace_id   ON bridge_execution_logs (trace_id);
CREATE INDEX IF NOT EXISTS idx_bridge_logs_custom_ui  ON bridge_execution_logs (custom_ui);
CREATE INDEX IF NOT EXISTS idx_bridge_logs_created_at ON bridge_execution_logs (created_at);
CREATE INDEX IF NOT EXISTS idx_bridge_logs_event      ON bridge_execution_logs (event);  -- P1-4: 新增事件类型索引

-- P1-4: 日志保留策略 - 90 天自动清理（通过 pg_cron 或定时任务）
-- 注释：建议在生产环境配置 pg_cron 定时任务：
-- SELECT cron.schedule('cleanup_bridge_logs', '0 2 * * *',
--   $$DELETE FROM bridge_execution_logs WHERE created_at < NOW() - INTERVAL '90 days'$$);

COMMENT ON TABLE bridge_execution_logs IS 'terminal_bridge 执行日志回采表 — 按 case_id 关联工单，支持端到端 trace 分析';
COMMENT ON COLUMN bridge_execution_logs.event IS '事件类型：exec.start（开始执行） / exec.output（输出回采） / exec.done（执行完成） / ssh.connected（SSH 连接成功）等';
COMMENT ON COLUMN bridge_execution_logs.extra IS '结构化附加字段（JSONB）：包含 command（命令）、output_preview（输出预览）、exit_code（退出码）等';
COMMENT ON COLUMN bridge_execution_logs.user_id IS '操作用户 ID（预留字段，当前 MVP 阶段暂未填充）';
