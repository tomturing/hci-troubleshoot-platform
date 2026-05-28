-- ============================================================
-- Seed 数据：tool_definition 表（Agent 工具体系 v2.0）
-- Version : 20260528
-- Issue   : T-TOOL-10
-- 说明    : 插入 13 条工具定义记录（SCP 4个 + acli 6个 + SOP 3个）
-- 幂等键  : tool_name（ON CONFLICT DO UPDATE）
-- ============================================================

-- ─── SCP 工具（4个，云端直接调用 SCP REST API）────────────────────────────

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'get_active_alerts',
    '查询活跃告警',
    'scp',
    '查询 HCI 平台当前活跃告警列表。用于了解平台当前是否有告警事件，是意图识别阶段（S0）的必要信息收集步骤。',
    '{
        "type": "object",
        "properties": {
            "limit": {
                "type": "integer",
                "description": "返回告警数量，默认 10，最大 50",
                "default": 10
            }
        },
        "required": []
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'get_failed_tasks',
    '查询失败任务',
    'scp',
    '查询 HCI 平台最近的失败操作任务。包含虚拟机开关机失败、存储操作失败等，是定位故障原因的关键信息来源。',
    '{
        "type": "object",
        "properties": {
            "task_type": {
                "type": "string",
                "description": "任务类型关键词，如''启动虚拟机''、''关闭虚拟机''"
            },
            "begin_time": {
                "type": "string",
                "description": "开始时间，格式 YYYY-MM-DD HH:MM:SS，默认 24 小时内"
            },
            "limit": {
                "type": "integer",
                "default": 10
            }
        },
        "required": []
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'get_vm_list',
    '查询虚拟机列表',
    'scp',
    '查询 HCI 平台上的虚拟机列表，可按名称过滤。用于确认虚拟机是否存在、当前状态和所在节点。',
    '{
        "type": "object",
        "properties": {
            "name_filter": {
                "type": "string",
                "description": "虚拟机名称关键词（支持模糊匹配）"
            },
            "limit": {
                "type": "integer",
                "default": 20
            }
        },
        "required": []
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'get_cluster_detail',
    '查询集群详情',
    'scp',
    '查询指定集群的详细信息，包括架构类型、许可模式、可用区等。',
    '{
        "type": "object",
        "properties": {
            "cluster_id": {
                "type": "string",
                "description": "集群 ID"
            }
        },
        "required": ["cluster_id"]
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- ─── acli 工具（6个，通过 bridge relay 中转执行）──────────────────────────

-- acli_exec：通用命令执行器（主力工具）
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'acli_exec',
    '执行 acli 命令',
    'acli',
    '在 HCI 节点执行 acli 命令（深圳桑福 HCI 平台专有 CLI，命令格式：acli [全局参数] {命名空间}+ {命令} [命令参数]）。

可用命名空间：
  vm        虚拟机：list / config get / status get / start / shutdown / disk list/check 等
  storage   存储：asan volume list / disk list / fc host list 等
  network   网络：nic list/up/down / bond list / anet vrouter list 等
  system    系统：top / free / df / ps / netstat / ping / iostat 等
  service   服务：<subsystem> <service> start/stop/restart/status
  alert     告警：get / list
  task      任务：get / list
  log       日志：get（--lines N）
  platform  平台：node list / version get / info get
  hardware  硬件：cpu info / gpu config list
  plugins   诊断插件：vm_start / vm_suspend / netdoctor / asys / performance_tools

使用约定：
  1. 不确定命令时，先执行 acli {namespace} --help 探索
  2. 不确定参数时，先执行 acli {namespace} {cmd} --help
  3. 优先使用 --formatter json 获得结构化输出
  4. 集群级操作使用 --cluster 参数
  5. 根据执行结果（成功/错误）判断下一步（ReAct 自探索）',
    '{
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "完整 acli 命令，必须以 ''acli'' 开头，例如 ''acli vm list --formatter json''"
            },
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选），不填时从 context_variables 中读取 node_ip"
            },
            "reason": {
                "type": "string",
                "description": "执行该命令的诊断原因（审计必填）"
            }
        },
        "required": ["command", "reason"]
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- bash_exec：通用 Linux Bash 执行工具（补充通道）
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'bash_exec',
    '执行 Bash 命令',
    'acli',
    '在 HCI 节点执行通用 Linux Bash 命令并返回输出。
优先使用 acli_exec；仅当 acli 无法满足时使用本工具（如分析特定日志文件、检查底层进程、读取内核参数等）。
注意：禁止执行 acli 命令（请使用 acli_exec）；执行路径限于 /sf/、/var/log/、/etc/（只读）等安全目录。',
    '{
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "bash 命令，例如 ''grep ERROR /sf/log/vtpdaemon.log | tail -50''"
            },
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            },
            "reason": {
                "type": "string",
                "description": "执行该命令的诊断原因（审计必填）"
            }
        },
        "required": ["command", "reason"]
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- acli 插件诊断工具（4个独立封装）

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, risk_level, is_active
) VALUES (
    'acli_plugin_vm_start',
    'VM 开机失败诊断',
    'acli',
    'VM 开机失败全链路检测（20+ 检查项）。诊断插件，一键执行，产出结构化诊断报告。',
    'acli plugins vm_start vm_start',
    '{
        "type": "object",
        "properties": {
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            }
        },
        "required": []
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, risk_level, is_active
) VALUES (
    'acli_plugin_vm_suspend',
    'VM 异常挂起诊断',
    'acli',
    'VM 异常挂起根因诊断。诊断插件，一键执行，产出结构化诊断报告。',
    'acli plugins vm_suspend vm_suspend',
    '{
        "type": "object",
        "properties": {
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            }
        },
        "required": []
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, risk_level, is_active
) VALUES (
    'acli_plugin_netdoctor',
    '网络全面诊断',
    'acli',
    '节点网络全面检测（需确认，有网络负载）。诊断插件，一键执行，产出结构化诊断报告。',
    'acli plugins netdoctor netdoctor',
    '{
        "type": "object",
        "properties": {
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（必填）"
            }
        },
        "required": ["node_ip"]
    }',
    2,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, risk_level, is_active
) VALUES (
    'acli_plugin_asys',
    '主机系统健康检查',
    'acli',
    '主机系统全面健康检查。诊断插件，一键执行，产出结构化诊断报告。',
    'acli plugins asys asys',
    '{
        "type": "object",
        "properties": {
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            }
        },
        "required": []
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- ─── SOP 导航工具（3个，本地执行，无 SSH）──────────────────────────────

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'get_sop_node',
    '获取 SOP 节点',
    'sop',
    '获取 SOP 决策树指定节点的内容。返回节点标题、判断方法、执行命令和子节点列表。用于 SOP 排障流程的分步导航，避免一次性注入完整 SOP 文档。注意：node_id 格式为 ''n-1''、''n-1-2'' 等，从根节点 ''n-1'' 开始。重要：此工具仅在 SOP 命中后可用，由系统自动注入 sop_document_id。',
    '{
        "type": "object",
        "properties": {
            "node_id": {
                "type": "string",
                "description": "节点 ID，如 ''n-1''（根节点）、''n-1-2''（二级节点）"
            },
            "sop_document_id": {
                "type": "integer",
                "description": "SOP 文档 ID（由系统自动注入，无需填写）",
                "default": 0
            }
        },
        "required": ["node_id"]
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'sop_advance',
    '推进 SOP 决策树',
    'sop',
    '推进 SOP 决策树到指定子节点。记录推理路径，更新当前节点位置，若到达叶节点则标记 SOP 执行完成。重要：target_node_id 必须是当前节点的子节点（通过 get_sop_node 获取 children 列表）。此工具仅在 SOP 命中后可用，由系统自动注入 conversation_id 和 sop_document_id。',
    '{
        "type": "object",
        "properties": {
            "target_node_id": {
                "type": "string",
                "description": "目标子节点 ID，必须是当前节点的子节点"
            },
            "reasoning": {
                "type": "string",
                "description": "推进理由（解释为何选择此分支，写入执行日志）"
            },
            "node_type": {
                "type": "string",
                "description": "目标节点类型（branch/diagnosis/solution，可选）"
            },
            "conversation_id": {
                "type": "string",
                "description": "会话 ID（由系统自动注入，无需填写）",
                "default": ""
            },
            "sop_document_id": {
                "type": "integer",
                "description": "SOP 文档 ID（由系统自动注入，无需填写）",
                "default": 0
            }
        },
        "required": ["target_node_id", "reasoning"]
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    parameters_schema, risk_level, is_active
) VALUES (
    'sop_request_variable',
    '请求 SOP 变量',
    'sop',
    '请求获取 SOP 执行所需的变量值（Just-In-Time 懒加载）。当 SOP 步骤需要某变量（如 vm_name、node_ip）且该变量尚未填充时调用。系统会根据变量的 acquisition_strategy 决定获取方式：user_input（向用户询问输入）/ user_confirm（展示候选值让用户确认）/ tool（自动调用指定工具获取）/ env_context（从环境上下文直接取值，无需调用此工具）。此工具仅在 SOP 命中后可用，由系统自动注入 conversation_id 和 sop_document_id。',
    '{
        "type": "object",
        "properties": {
            "variable_name": {
                "type": "string",
                "description": "需要获取的变量名（如 vm_name、node_ip、disk_id）"
            },
            "reason": {
                "type": "string",
                "description": "为什么需要此变量（用于向用户解释，可选）"
            },
            "conversation_id": {
                "type": "string",
                "description": "会话 ID（由系统自动注入，无需填写）",
                "default": ""
            },
            "sop_document_id": {
                "type": "integer",
                "description": "SOP 文档 ID（由系统自动注入，无需填写）",
                "default": 0
            }
        },
        "required": ["variable_name"]
    }',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    parameters_schema = EXCLUDED.parameters_schema,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- ============================================================
-- 说明：
-- 1. 共 13 条工具定义：SCP 4个 / acli 6个 / SOP 3个
-- 2. 幂等键为 tool_name（ON CONFLICT DO UPDATE），支持重复执行
-- 3. risk_level 说明：
--    - 1 = 只读查询（auto），自动执行
--    - 2 = 写操作需确认（confirm），需用户确认后执行
--    - 3 = 高危拦截（block），直接拒绝
--    注意：acli_exec/bash_exec 的 risk_level=1 为静态兜底值，运行时 RiskClassifier 动态覆盖
-- 4. category 说明：
--    - scp：SCP 平台 REST API（云端直接调用）
--    - acli：HCI 节点执行（通过 bridge relay 中转）
--    - sop：SOP 导航工具（本地执行，无 SSH）
-- ============================================================