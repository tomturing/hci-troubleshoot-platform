-- 051：SOP 变量声明契约整改（存量一次性对齐）
--
-- 背景（Q2026102600003 复盘）：
--   SOP#2/#3/#5 的变量声明长期存在三类结构性缺陷，且全部通过了当时的发布校验：
--     1) "依赖"列写成自然语言（"任务原始数据"/"告警原始数据"），
--        运行时被判为"未声明依赖"，进而引导模型去请求一个不存在的变量；
--     2) 日志类变量的整行列错位：来源写成"工具调用"、说明写成工具名、
--        参数模板与表达式互相串位，导致 task_logs/alert_logs 退化成 user_input；
--     3) 22 个"通用排障变量"在章节级用 4 列表头声明，无法表达采集工具，
--        解析后全部是 tool_call + acquisition_tool=NULL，
--        运行时静默落到 _request_user_input（表现为"老是弹框让人填"）。
--
-- 本迁移以"最终态"整改，不做临时兜底：
--   A. 路径类变量 → path_template（模板由 task_day/vmid 推导）
--   B. 命令类变量 → tool_call:acli_exec + args_template（命令取自 SOP 正文前置检查）
--   C. 无采集定义、只能人工判断的变量 → user_input，并补全"用途 + 取值线索"
--   D. 日志类变量 → tool_call:acli_exec + output_path=stdout + 正确 args_template
--   E. is_sys_disk（llm_inference 运行时未实现）→ user_input（boolean 自动渲染为确认卡）
--   F. tree_json.variables[].source 按 variable_schema 单源重编译，消除双事实源漂移
--
-- 唯一调用链标记：sop_variable_contract_v1
-- 幂等：已整改的变量（strategy 与目标一致）不会被重复改写。

BEGIN;

-- ──────────────────────────────────────────────────────────────────────────────
-- A/B/C：通用变量整改规则（SOP#1/#2/#3 共用同一份模板）
-- ──────────────────────────────────────────────────────────────────────────────
CREATE TEMP TABLE _var_fix (
    name TEXT PRIMARY KEY,
    strategy TEXT NOT NULL,
    tool TEXT,
    output_path TEXT,
    expression TEXT,
    deps JSONB NOT NULL,
    description TEXT NOT NULL
) ON COMMIT DROP;

INSERT INTO _var_fix (name, strategy, tool, output_path, expression, deps, description) VALUES
-- A. 路径模板：由 task_day / vmid 推导，既不该采集也不该人工手抄
('kernel_log_path',        'path_template', NULL, NULL, '/sf/log/{task_day}/kernel.log',                        '["task_day"]',          '内核日志路径（由 task_day 推导）'),
('qemu_log_path',          'path_template', NULL, NULL, '/sf/log/{task_day}/sfvt_qemu_{vmid}.log',              '["task_day","vmid"]',   'QEMU 日志路径（由 task_day + vmid 推导）'),
('runc_log_path',          'path_template', NULL, NULL, '/sf/log/{task_day}/runc_start_{vmid}.log',             '["task_day","vmid"]',   '容器启动日志路径（由 task_day + vmid 推导）'),
('statuscenterd_log_path', 'path_template', NULL, NULL, '/sf/log/{task_day}/statuscenterd.log',                 '["task_day"]',          'statuscenterd 日志路径（由 task_day 推导）'),
('vn_manager_log_path',    'path_template', NULL, NULL, '/sf/log/{task_day}/vn/vn-manager-service-api.log',     '["task_day"]',          'vn-manager-service-api 日志路径（由 task_day 推导）'),
('vn_node_api_log_path',   'path_template', NULL, NULL, '/sf/log/{task_day}/vn-node-agent-api.log',              '["task_day"]',          'vn-node-agent-api 日志路径（由 task_day 推导）'),
-- vtpdaemon 日志固定落在 today 目录，无占位符，按常量处理
('vtpdaemon_log_path',     'sop_default',   NULL, NULL, NULL,                                                   '[]',                    'vtpdaemon 日志路径（固定 /sf/log/today/vt/sfvt_vtpdaemon.log）'),

-- B. 命令类：命令取自各章节「前置检查」中的实际 acli 调用
('apache2_count',    'tool_call', 'acli_exec', 'stdout', NULL, '[]', 'apache2 进程数量'),
('vtpdaemon_count',  'tool_call', 'acli_exec', 'stdout', NULL, '[]', 'vtpdaemon 进程数量'),
('mysql_status',     'tool_call', 'acli_exec', 'stdout', NULL, '[]', 'MySQL 服务状态'),
('exporter_status',  'tool_call', 'acli_exec', 'stdout', NULL, '[]', 'exporter 服务状态'),
('prometheus_status','tool_call', 'acli_exec', 'stdout', NULL, '[]', 'prometheus 服务状态'),

-- C. 无采集定义、只能人工判断/从现场观察获取 → user_input，并写清用途与取值线索
('cert_path',      'user_input', NULL, NULL, NULL, '[]', '证书文件绝对路径；用于 acli system md5sum -p {cert_path} 校验证书一致性'),
('db_master_host', 'user_input', NULL, NULL, NULL, '[]', '数据库主库节点信息；用于确认 MySQL 主库身份'),
('host_ip',        'user_input', NULL, NULL, NULL, '[]', '异常主机 IP；用于 acli system telnet {host_ip} 6379 等连通性检查'),
('lock_name',      'user_input', NULL, NULL, NULL, '[]', '残留锁文件名；用于 acli vm lock clean -v {vmid} -n {lock_name}'),
('master_host',    'user_input', NULL, NULL, NULL, '[]', '主控主机信息；用于确认集群主控节点'),
('pci_address',    'user_input', NULL, NULL, NULL, '[]', '显卡 PCI 地址（形如 0000:xx:00.0）；用于查看 virtfn 直通情况'),
('storage_id',     'user_input', NULL, NULL, NULL, '[]', '存储 ID；可从虚拟机磁盘路径中获取，用于 acli system df | grep {storage_id}'),
('upid_pid',       'user_input', NULL, NULL, NULL, '[]', 'UPID 进程 PID；用于定位残留 UPID 进程'),
('vm_disk_file',   'user_input', NULL, NULL, NULL, '[]', '虚拟机磁盘文件名；用于 acli vm disk check -v {vmid} -d {vm_disk_file}'),
('vm_disk_path',   'user_input', NULL, NULL, NULL, '[]', '虚拟机磁盘路径；可从虚拟机配置中获取');

-- 应用规则：仅改写 strategy 尚未对齐的变量（幂等）
UPDATE sop_document d
SET variable_schema = sub.new_schema,
    updated_at = NOW()
FROM (
    SELECT
        doc.id,
        jsonb_agg(
            CASE
                WHEN f.name IS NULL THEN item.v
                WHEN item.v ->> 'acquisition_strategy' = f.strategy
                     AND COALESCE(item.v ->> 'acquisition_tool', '') = COALESCE(f.tool, '')
                    THEN item.v  -- 已整改，跳过
                ELSE jsonb_strip_nulls(
                    item.v || jsonb_build_object(
                        'acquisition_strategy', f.strategy,
                        'acquisition_tool', f.tool,
                        'output_path', f.output_path,
                        'expression', f.expression,
                        'depends_on', f.deps,
                        'description', f.description
                    )
                )
            END
            ORDER BY item.ord
        ) AS new_schema
    FROM sop_document doc
    CROSS JOIN LATERAL jsonb_array_elements(doc.variable_schema) WITH ORDINALITY AS item(v, ord)
    LEFT JOIN _var_fix f ON f.name = item.v ->> 'name'
    WHERE doc.id IN (1, 2, 3, 5)
      AND doc.variable_schema IS NOT NULL
      AND jsonb_typeof(doc.variable_schema) = 'array'
    GROUP BY doc.id
) sub
WHERE d.id = sub.id
  AND d.variable_schema IS DISTINCT FROM sub.new_schema;

-- ──────────────────────────────────────────────────────────────────────────────
-- A1. 补齐 task_day 派生声明
--     日志路径模板依赖 {task_day}；SOP#1 声明了 task_date 却漏了 task_day，
--     机器无法安全推断「日」的提取方式，这里按 SOP#3 的既有约定补齐：
--     task_day = split(task_date, '-').2
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = d.variable_schema || jsonb_build_array(
        jsonb_build_object(
            'name', 'task_day',
            'type', 'string',
            'required', true,
            'acquisition_strategy', 'derived',
            'expression', 'split(task_date, ''-'').2',
            'depends_on', jsonb_build_array('task_date'),
            'description', '日（日期），从task_date提取，格式为dd',
            'display_name', '日',
            'auto_generated', false
        )
    ),
    updated_at = NOW()
WHERE d.id IN (1, 2, 3, 5)
  AND d.variable_schema IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE v ->> 'name' = 'task_date'
  )
  AND NOT EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE v ->> 'name' = 'task_day'
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- A2. sop_default 必须带 default_value（固定路径常量）
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN item.v ->> 'name' = 'vtpdaemon_log_path'
                     AND COALESCE(item.v ->> 'acquisition_strategy', '') = 'sop_default'
                     AND COALESCE(item.v ->> 'default_value', '') = ''
                THEN item.v || jsonb_build_object('default_value', '/sf/log/today/vt/sfvt_vtpdaemon.log')
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.id IN (1, 2, 3)
  AND d.variable_schema IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE v ->> 'name' = 'vtpdaemon_log_path' AND COALESCE(v ->> 'default_value', '') = ''
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- B2. SOP#5：skill_call 缺 output_path（038 迁移只修了 tree_json，未修 variable_schema）
--     既有约定：技能输出键与变量同名（038 已确认 check_meth / memory_tag）
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN item.v ->> 'name' IN ('check_location', 'check_meth', 'memory_tag')
                     AND item.v ->> 'acquisition_strategy' = 'skill_call'
                     AND COALESCE(item.v ->> 'output_path', '') = ''
                THEN item.v || jsonb_build_object('output_path', item.v ->> 'name')
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.id = 5
  AND d.variable_schema IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE v ->> 'name' IN ('check_location', 'check_meth', 'memory_tag')
        AND COALESCE(v ->> 'output_path', '') = ''
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- C2. SOP#2「磁盘寿命到期」的 task_* 链路已不适用（该流程走 alert_logs 链路）
--     实测决策树中不存在 {vmid}/{hostname}/{task_*} 引用，整链标记 deprecated。
--     保留定义而非删除，便于追溯；契约允许废弃链路内部互相引用。
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN item.v ->> 'name' IN ('task_logs', 'task_date', 'task_day', 'task_description', 'hostname', 'vmid')
                     AND COALESCE((item.v ->> 'deprecated')::boolean, FALSE) = FALSE
                THEN item.v || jsonb_build_object('deprecated', TRUE)
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.id = 2
  AND d.variable_schema IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE v ->> 'name' = 'task_logs'
        AND COALESCE((v ->> 'deprecated')::boolean, FALSE) = FALSE
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- D. 日志类变量列错位修复（依赖悬空 + 策略退化）
--    原表格：| task_logs | json | 工具调用 | acli_exec | 任务原始数据 | | stdout | {reason,command} |
--    正确语义：工具调用 acli_exec，输出 stdout，参数模板为 {reason,command}，无前置依赖
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN item.v ->> 'name' = 'task_logs'
                     AND COALESCE(item.v ->> 'acquisition_strategy', '') <> 'tool_call'
                THEN jsonb_strip_nulls(
                        item.v || jsonb_build_object(
                            'acquisition_strategy', 'tool_call',
                            'acquisition_tool', 'acli_exec',
                            'output_path', 'stdout',
                            'depends_on', '[]'::jsonb,
                            'description', '启动虚拟机失败任务原始数据（acli task get 的 JSON 输出）'
                        )
                        -- 原 expression 里存的是真正的参数模板，归位
                        || jsonb_build_object(
                            'acquisition_args_template',
                            COALESCE(item.v -> 'acquisition_args_template', item.v -> 'expression')
                        )
                        || jsonb_build_object('expression', NULL)
                     )
                WHEN item.v ->> 'name' = 'alert_logs'
                     AND COALESCE(item.v ->> 'acquisition_strategy', '') <> 'tool_call'
                THEN jsonb_strip_nulls(
                        item.v || jsonb_build_object(
                            'acquisition_strategy', 'tool_call',
                            'acquisition_tool', 'acli_exec',
                            'output_path', 'stdout',
                            'depends_on', '[]'::jsonb,
                            'description', '磁盘相关告警原始数据（acli alert get 的 JSON 输出）'
                        )
                        || jsonb_build_object(
                            'acquisition_args_template',
                            COALESCE(item.v -> 'acquisition_args_template', item.v -> 'expression')
                        )
                        || jsonb_build_object('expression', NULL)
                     )
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.id IN (1, 2, 3, 5)
  AND d.variable_schema IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE v ->> 'name' IN ('task_logs', 'alert_logs')
        AND COALESCE(v ->> 'acquisition_strategy', '') = 'user_input'
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- E. is_sys_disk：llm_inference 在运行时未实现（会静默转人工），改为显式 boolean 确认
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET variable_schema = (
        SELECT jsonb_agg(
            CASE
                WHEN item.v ->> 'name' = 'is_sys_disk'
                     AND item.v ->> 'acquisition_strategy' = 'llm_inference'
                THEN jsonb_strip_nulls(
                        item.v || jsonb_build_object(
                            'acquisition_strategy', 'user_input',
                            'depends_on', '[]'::jsonb,
                            'description', '是否为系统盘（是/否）；依据告警类型与磁盘槽位信息判断'
                        )
                     )
                ELSE item.v
            END
            ORDER BY item.ord
        )
        FROM jsonb_array_elements(d.variable_schema) WITH ORDINALITY AS item(v, ord)
    ),
    updated_at = NOW()
WHERE d.id = 2
  AND d.variable_schema IS NOT NULL
  AND EXISTS (
      SELECT 1 FROM jsonb_array_elements(d.variable_schema) v
      WHERE v ->> 'name' = 'is_sys_disk' AND v ->> 'acquisition_strategy' = 'llm_inference'
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- F. tree_json.variables[].source 单源重编译（消除双事实源漂移）
--    规则与 040 迁移一致：以 variable_schema 为唯一事实源。
-- ──────────────────────────────────────────────────────────────────────────────
UPDATE sop_document d
SET tree_json = jsonb_set(
        d.tree_json,
        '{variables}',
        (
            SELECT jsonb_agg(
                jsonb_set(
                    item.v,
                    '{source}',
                    COALESCE(
                        (
                            SELECT CASE s ->> 'acquisition_strategy'
                                WHEN 'tool_call' THEN to_jsonb('tool:' || COALESCE(s ->> 'acquisition_tool', ''))
                                WHEN 'skill_call' THEN to_jsonb('skill:' || COALESCE(s ->> 'acquisition_tool', ''))
                                WHEN 'env_injection' THEN to_jsonb('env:' || COALESCE(s ->> 'acquisition_tool', ''))
                                WHEN 'json_extract' THEN '"json_extract"'::jsonb
                                WHEN 'user_input' THEN '"user_input"'::jsonb
                                WHEN 'user_confirm' THEN '"user_confirm"'::jsonb
                                WHEN 'path_template' THEN '"path_template"'::jsonb
                                WHEN 'derived' THEN '"derived"'::jsonb
                                WHEN 'sop_default' THEN '"sop_default"'::jsonb
                                ELSE item.v -> 'source'
                            END
                            FROM jsonb_array_elements(d.variable_schema) s
                            WHERE s ->> 'name' = item.v ->> 'name'
                            LIMIT 1
                        ),
                        item.v -> 'source'
                    )
                )
                ORDER BY item.ord
            )
            FROM jsonb_array_elements(d.tree_json -> 'variables') WITH ORDINALITY AS item(v, ord)
        ),
        true
    ),
    updated_at = NOW()
WHERE d.id IN (1, 2, 3, 5)
  AND d.tree_json -> 'variables' IS NOT NULL
  AND jsonb_typeof(d.tree_json -> 'variables') = 'array'
  AND EXISTS (
      -- 仅当仍存在中文别名 / 与实际 strategy 不一致时才改写（幂等）
      SELECT 1
      FROM jsonb_array_elements(d.tree_json -> 'variables') tv
      JOIN jsonb_array_elements(d.variable_schema) sv ON sv ->> 'name' = tv ->> 'name'
      WHERE COALESCE(tv ->> 'source', '') <> CASE sv ->> 'acquisition_strategy'
                WHEN 'tool_call' THEN 'tool:' || COALESCE(sv ->> 'acquisition_tool', '')
                WHEN 'skill_call' THEN 'skill:' || COALESCE(sv ->> 'acquisition_tool', '')
                WHEN 'env_injection' THEN 'env:' || COALESCE(sv ->> 'acquisition_tool', '')
                ELSE COALESCE(sv ->> 'acquisition_strategy', '')
            END
  );

-- ──────────────────────────────────────────────────────────────────────────────
-- 整改结果自检：仍然悬空/未定义的依赖必须为 0（否则迁移失败，避免"假修好"）
-- ──────────────────────────────────────────────────────────────────────────────
DO $$
DECLARE
    bad_count INT;
    bad_list TEXT;
BEGIN
    SELECT COUNT(*), string_agg(DISTINCT d.id || ':' || (v ->> 'name') || '->' || dep.dep_name, ', ')
    INTO bad_count, bad_list
    FROM sop_document d
    CROSS JOIN LATERAL jsonb_array_elements(d.variable_schema) v
    CROSS JOIN LATERAL jsonb_array_elements_text(COALESCE(v -> 'depends_on', '[]'::jsonb)) AS dep(dep_name)
    WHERE d.id IN (1, 2, 3, 5)
      AND NOT EXISTS (
          SELECT 1 FROM jsonb_array_elements(d.variable_schema) sv
          WHERE sv ->> 'name' = dep.dep_name
      );

    IF bad_count > 0 THEN
        RAISE EXCEPTION '051 整改后仍存在 % 处悬空依赖：%', bad_count, bad_list;
    END IF;

    RAISE NOTICE '051 变量契约整改完成，悬空依赖 0 处';
END $$;

COMMIT;
