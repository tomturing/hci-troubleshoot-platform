-- 052：通用排障变量包种子（hci_common_vt_diag）
--
-- 唯一调用链标记：sop_variable_pack_seed_v1
-- 幂等：ON CONFLICT (pack_name) DO UPDATE，仅当 version 提升时覆盖，避免回滚旧内容。
--
-- 说明：本包收录 SOP#1/#2/#3 中重复出现的 22 个虚拟机排障常用变量。
-- 三类归属在变量 description 中已写明，便于 SOP 作者按需裁剪：
--   路径类   -> path_template / sop_default（由 task_day、vmid 推导）
--   命令类   -> tool_call:acli_exec（命令取自各 SOP 正文「前置检查」）
--   人工类   -> user_input（SOP 作者未定义采集方式，必须写清取值线索）

BEGIN;

-- ──────────────────────────────────────────────────────────────────────────────
-- 自包含前置：data-migration 在 db-migrate.sh 的 Step 1 执行，而 Atlas 声明式收敛
-- 在 Step 3。存量库升级时（Step 0 跳过），新表尚未创建，直接 INSERT 会失败并
-- 阻塞整个 ArgoCD PreSync（实测：relation "sop_variable_pack" does not exist）。
-- 因此本迁移必须自己保证依赖结构存在（与 desired_schema.sql 保持一致，幂等）。
-- ──────────────────────────────────────────────────────────────────────────────
ALTER TABLE sop_document ADD COLUMN IF NOT EXISTS applied_packs jsonb NOT NULL DEFAULT '[]'::jsonb;

CREATE INDEX IF NOT EXISTS idx_sop_document_applied_packs ON sop_document USING GIN (applied_packs);

CREATE TABLE IF NOT EXISTS sop_variable_pack (
    id serial NOT NULL,
    pack_name varchar(64) NOT NULL UNIQUE,
    version integer NOT NULL DEFAULT 1,
    description text NOT NULL DEFAULT '',
    variables jsonb NOT NULL DEFAULT '[]'::jsonb,
    is_active boolean NOT NULL DEFAULT true,
    created_at timestamptz NOT NULL DEFAULT now(),
    updated_at timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT chk_sop_variable_pack_variables_array CHECK (jsonb_typeof(variables) = 'array'),
    CONSTRAINT sop_variable_pack_pkey PRIMARY KEY (id)
);

COMMENT ON TABLE sop_variable_pack IS 'SOP 变量包：可被多个 SOP 复用的命名变量集合';
COMMENT ON COLUMN sop_variable_pack.variables IS '变量定义数组，结构与 sop_document.variable_schema 一致';
COMMENT ON COLUMN sop_document.applied_packs IS '已应用的变量包引用列表，形如 [{"pack":"hci_common_vt_diag","version":1}]';

INSERT INTO sop_variable_pack (pack_name, version, description, variables, is_active)
VALUES (
    'hci_common_vt_diag',
    1,
    '虚拟机排障通用变量包：日志路径、进程/服务状态、需人工确认的现场标识',
    $json$[
  {"name": "kernel_log_path", "type": "string", "acquisition_strategy": "path_template", "expression": "/sf/log/{task_day}/kernel.log", "depends_on": ["task_day"], "description": "内核日志路径（由 task_day 推导）"},
  {"name": "qemu_log_path", "type": "string", "acquisition_strategy": "path_template", "expression": "/sf/log/{task_day}/sfvt_qemu_{vmid}.log", "depends_on": ["task_day", "vmid"], "description": "QEMU 日志路径（由 task_day + vmid 推导）"},
  {"name": "runc_log_path", "type": "string", "acquisition_strategy": "path_template", "expression": "/sf/log/{task_day}/runc_start_{vmid}.log", "depends_on": ["task_day", "vmid"], "description": "容器启动日志路径（由 task_day + vmid 推导）"},
  {"name": "statuscenterd_log_path", "type": "string", "acquisition_strategy": "path_template", "expression": "/sf/log/{task_day}/statuscenterd.log", "depends_on": ["task_day"], "description": "statuscenterd 日志路径（由 task_day 推导）"},
  {"name": "vn_manager_log_path", "type": "string", "acquisition_strategy": "path_template", "expression": "/sf/log/{task_day}/vn/vn-manager-service-api.log", "depends_on": ["task_day"], "description": "vn-manager-service-api 日志路径（由 task_day 推导）"},
  {"name": "vn_node_api_log_path", "type": "string", "acquisition_strategy": "path_template", "expression": "/sf/log/{task_day}/vn-node-agent-api.log", "depends_on": ["task_day"], "description": "vn-node-agent-api 日志路径（由 task_day 推导）"},
  {"name": "vtpdaemon_log_path", "type": "string", "acquisition_strategy": "sop_default", "default_value": "/sf/log/today/vt/sfvt_vtpdaemon.log", "depends_on": [], "description": "vtpdaemon 日志路径（固定落在 today 目录）"},

  {"name": "apache2_count", "type": "int", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec", "output_path": "stdout", "acquisition_args_template": {"reason": "统计 apache2 进程数量", "command": "acli system ps auxf | grep apache2 | wc -l"}, "depends_on": [], "description": "apache2 进程数量"},
  {"name": "vtpdaemon_count", "type": "int", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec", "output_path": "stdout", "acquisition_args_template": {"reason": "统计 vtpdaemon 进程数量", "command": "acli system ps auxf | grep vtpdaemon | wc -l"}, "depends_on": [], "description": "vtpdaemon 进程数量"},
  {"name": "mysql_status", "type": "string", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec", "output_path": "stdout", "acquisition_args_template": {"reason": "检查 MySQL 服务状态", "command": "acli platform check -p mysql"}, "depends_on": [], "description": "MySQL 服务状态"},
  {"name": "exporter_status", "type": "string", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec", "output_path": "stdout", "acquisition_args_template": {"reason": "检查 exporter 服务状态", "command": "acli service asv exporter status"}, "depends_on": [], "description": "exporter 服务状态"},
  {"name": "prometheus_status", "type": "string", "acquisition_strategy": "tool_call", "acquisition_tool": "acli_exec", "output_path": "stdout", "acquisition_args_template": {"reason": "检查 prometheus 服务状态", "command": "acli service asv prometheus status"}, "depends_on": [], "description": "prometheus 服务状态"},

  {"name": "cert_path", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "证书文件绝对路径；用于 acli system md5sum -p {cert_path} 校验证书一致性"},
  {"name": "db_master_host", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "数据库主库节点信息；用于确认 MySQL 主库身份"},
  {"name": "host_ip", "type": "ip", "acquisition_strategy": "user_input", "depends_on": [], "description": "异常主机 IP；用于 acli system telnet {host_ip} 6379 等连通性检查"},
  {"name": "lock_name", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "残留锁文件名；用于 acli vm lock clean -v {vmid} -n {lock_name}"},
  {"name": "master_host", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "主控主机信息；用于确认集群主控节点"},
  {"name": "pci_address", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "显卡 PCI 地址（形如 0000:xx:00.0）；用于查看 virtfn 直通情况"},
  {"name": "storage_id", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "存储 ID；可从虚拟机磁盘路径中获取，用于 acli system df | grep {storage_id}"},
  {"name": "upid_pid", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "UPID 进程 PID；用于定位残留 UPID 进程"},
  {"name": "vm_disk_file", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "虚拟机磁盘文件名；用于 acli vm disk check -v {vmid} -d {vm_disk_file}"},
  {"name": "vm_disk_path", "type": "string", "acquisition_strategy": "user_input", "depends_on": [], "description": "虚拟机磁盘路径；可从虚拟机配置中获取"}
]$json$::jsonb,
    TRUE
)
ON CONFLICT (pack_name) DO UPDATE
SET version     = EXCLUDED.version,
    description = EXCLUDED.description,
    variables   = EXCLUDED.variables,
    is_active   = EXCLUDED.is_active,
    updated_at  = NOW()
WHERE sop_variable_pack.version < EXCLUDED.version;

-- 标记已发布 SOP 的引用关系（内容已由 051 逐条落地，此处仅登记来源）
UPDATE sop_document
SET applied_packs = jsonb_build_array(jsonb_build_object('pack', 'hci_common_vt_diag', 'version', 1)),
    updated_at = NOW()
WHERE id IN (1, 2, 3)
  AND applied_packs = '[]'::jsonb
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(variable_schema) v
      WHERE v ->> 'name' = 'qemu_log_path' AND v ->> 'acquisition_strategy' = 'path_template'
  );

COMMIT;
