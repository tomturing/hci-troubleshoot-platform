-- ===========================================================================
-- 迁移: 20261022000000_add_log_collect_task.sql
-- 说明: 新增日志收集任务表（工单附属），承载 acli log_collect 任务的控制面状态。
-- 背景: 采集执行发生在 terminal_bridge（云端不可达客户私网 HCI），平台只做
--       任务登记、状态机、进度与制品元数据；工单号（ticket_no）用于 acli -s。
-- 要点:
--   1. 同一工单只允许一个活跃任务（部分唯一索引），防止并发采集互相 kill；
--   2. 进度与状态由 bridge 轮询结果回写，平台不自行推断；
--   3. 日期跨度 ≤30 天与 acli 插件约束一致；
--   4. 工单号白名单与 bridge 二次校验保持一致（插件不防注入）。
-- 幂等: CREATE TABLE IF NOT EXISTS / CREATE INDEX IF NOT EXISTS
-- ===========================================================================

CREATE TABLE IF NOT EXISTS log_collect_task (
    task_id uuid NOT NULL DEFAULT gen_random_uuid(),
    case_id varchar(20) NOT NULL,
    ticket_no varchar(32) NOT NULL,
    origin_node_ip inet NOT NULL,
    modules text[] NOT NULL,
    date_from date NOT NULL,
    date_to date NOT NULL,
    ips text[] NOT NULL,
    extra_paths text[],
    remote_url text,
    status varchar(16) NOT NULL DEFAULT 'pending',
    progress smallint NOT NULL DEFAULT 0,
    arch varchar(16),
    acli_version varchar(32),
    upload_mode varchar(16),
    archive_path text,
    archive_size bigint,
    archive_sha256 char(64),
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    error_code varchar(64),
    error_message text,
    trace_id varchar(64),
    created_by varchar(255) NOT NULL,
    created_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at timestamptz NOT NULL DEFAULT CURRENT_TIMESTAMP,
    finished_at timestamptz,
    CONSTRAINT log_collect_task_pkey PRIMARY KEY (task_id),
    CONSTRAINT fk_log_collect_task_case FOREIGN KEY (case_id) REFERENCES "case" (case_id) ON DELETE CASCADE,
    CONSTRAINT ck_log_collect_ticket CHECK (ticket_no ~ '^[A-Za-z0-9_-]{1,32}$'),
    CONSTRAINT ck_log_collect_range CHECK (date_to >= date_from AND date_to - date_from <= 30),
    CONSTRAINT ck_log_collect_progress CHECK (progress BETWEEN 0 AND 100),
    -- 表达式按 Atlas 规范化形式书写，保证声明式 schema apply 后 schema diff 为空（幂等）。
    CONSTRAINT ck_log_collect_status CHECK ((status)::text = ANY ((ARRAY['pending'::character varying, 'probing'::character varying, 'installing'::character varying, 'collecting'::character varying, 'packaging'::character varying, 'uploading'::character varying, 'completed'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[])),
    CONSTRAINT ck_log_collect_upload_mode CHECK ((upload_mode IS NULL) OR ((upload_mode)::text = ANY ((ARRAY['acli_direct'::character varying, 'platform_direct'::character varying, 'bridge_relay'::character varying, 'none'::character varying])::text[])))
);

COMMENT ON TABLE log_collect_task IS '日志收集任务表 — 承载 acli log_collect 的控制面状态，执行发生在 terminal_bridge';
COMMENT ON COLUMN log_collect_task.task_id IS '任务主键';
COMMENT ON COLUMN log_collect_task.case_id IS '关联工单，ON DELETE CASCADE';
COMMENT ON COLUMN log_collect_task.ticket_no IS '用户输入的 ICare 单号，作为 acli log_collect -s，直接成为产物名';
COMMENT ON COLUMN log_collect_task.origin_node_ip IS '发起采集的 HCI 节点；acli progress 只在该节点有效，轮询必须路由回此节点';
COMMENT ON COLUMN log_collect_task.modules IS '收集专项（acli -m 枚举）';
COMMENT ON COLUMN log_collect_task.date_from IS '收集起始日期（acli -d 左值）';
COMMENT ON COLUMN log_collect_task.date_to IS '收集结束日期（acli -d 右值），跨度 ≤30 天';
COMMENT ON COLUMN log_collect_task.ips IS '目标主机；["all"] 表示所有在线节点';
COMMENT ON COLUMN log_collect_task.extra_paths IS '额外收集路径（acli -p）';
COMMENT ON COLUMN log_collect_task.remote_url IS '上传目标（acli -u），仅 http(s) 且只认官方日志服务器';
COMMENT ON COLUMN log_collect_task.status IS '任务状态机：pending/probing/installing/collecting/packaging/uploading/completed/failed/cancelled';
COMMENT ON COLUMN log_collect_task.progress IS '总进度 0-100，来自 acli progress 的"总进度"字段';
COMMENT ON COLUMN log_collect_task.detail IS '主机明细与上传状态等结构化进度快照';
COMMENT ON COLUMN log_collect_task.archive_sha256 IS '压缩包 SHA-256（制品完整性校验）';

CREATE INDEX IF NOT EXISTS idx_log_collect_task_case
    ON log_collect_task (case_id, created_at DESC);

-- 同一工单只允许一个活跃任务：避免同节点并发 create 互相覆盖 progress 文件与产物目录。
CREATE UNIQUE INDEX IF NOT EXISTS uq_log_collect_task_active_case
    ON log_collect_task (case_id)
    WHERE ((status)::text <> ALL ((ARRAY['completed'::character varying, 'failed'::character varying, 'cancelled'::character varying])::text[]));
