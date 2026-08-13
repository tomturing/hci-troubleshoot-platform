-- ============================================================
-- Seed 数据：QKV/QFK 关键信号工具定义
-- Version : 20260730
-- Issue   : T-TOOL-QKV-QFK-001
-- 说明    : 插入 11 条工具定义记录（QKV 3个 + QFK 8个）
-- 幂等键  : tool_name（ON CONFLICT DO NOTHING，禁止覆盖管理员治理结果）
--
-- 统一定义（display_name 标准命名，勿擅自修改以免造成误解）：
--   qkv_alert    - 前端信号-告警查询
--   qkv_task     - 前端信号-任务查询
--   qkv_dialog   - 前端信号-弹框查询
--   qfk_log      - 后端信号-日志检查和操作
--   qfk_service  - 后端信号-服务检查和操作
--   qfk_system   - 后端信号-系统检查和操作
--   qfk_vm       - 后端信号-虚拟机相关操作
--   qfk_network  - 后端信号-网络相关操作
--   qfk_storage  - 后端信号-存储相关操作
--   qfk_hardware - 后端信号-硬件相关操作
--   qfk_platform - 后端信号-平台相关操作
-- ============================================================

-- ─── QKV 前端信号（生产者）─────────────────────────────────────

-- QKV.alert: 前端信号-告警查询
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qkv_alert',
    '前端信号-告警查询',
    'qkv',
    '前端信号（生产者）：查询 HCI 平台当前活跃告警列表，产出变量供后续信号消费。支持 produces 自定义输出字段，通过 acli alert get 执行。',
    'acli --formatter json alert get -k {{keyword}} -l {{limit}}',
    '{
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "告警关键字过滤，用于搜索告警描述、对象名称等。例如：磁盘被拔出、镜像忙、超时"
            },
            "limit": {
                "type": "integer",
                "description": "最大返回告警数量，范围 1-200",
                "default": 100,
                "minimum": 1,
                "maximum": 200
            },
            "alert_type": {
                "type": "string",
                "description": "告警类型过滤（可选）"
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 10},
            "instruction": {"type": "string", "description": "信号语义说明"},
            "produces": {
                "type": "array",
                "description": "产出变量规格列表。定义要从告警结果中提取的字段，每个元素包含 name（输出变量名）和 path（JSON字段路径）。路径支持 | 分隔的多路径容错，如 host|hostname|hostid 表示依次尝试这三个字段。产出变量以大写命名（如 HOST），后续信号通过 {{HOST}} 引用。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "输出变量名，建议使用大写下划线格式（如 HOST、VM_ID、TARGET），用于后续信号通过 {{变量名}} 引用"
                        },
                        "path": {
                            "type": "string",
                            "description": "JSON 字段路径（acli 返回值中的 key）。支持 | 分隔的多路径容错，如 host|hostname|hostid"
                        }
                    },
                    "required": ["name", "path"]
                },
                "default": []
            }
        },
        "required": ["keyword"],
        "additionalProperties": false
    }',
    '[{"keyword": "磁盘被拔出", "limit": 50, "produces": [{"name": "HOST", "path": "host|hostname"}, {"name": "DISK_SN", "path": "target|object_name"}]}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QKV.task: 前端信号-任务查询
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qkv_task',
    '前端信号-任务查询',
    'qkv',
    '前端信号（生产者）：查询 HCI 平台操作任务，主要过滤失败任务。产出变量供后续信号消费。支持 is_failed 过滤失败任务，produces 自定义输出字段。',
    'acli --formatter json task get -k {{keyword}} {{#if is_failed}}-s failed{{/if}} -l {{limit}}',
    '{
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "任务关键字过滤，搜索行为、主机、对象和描述。例如：登录、启动、镜像忙"
            },
            "is_failed": {
                "type": "boolean",
                "description": "是否只查失败任务。true=仅返回失败任务，false=返回所有任务",
                "default": false
            },
            "limit": {
                "type": "integer",
                "description": "最大返回任务数量，范围 1-200",
                "default": 100,
                "minimum": 1,
                "maximum": 200
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 10},
            "instruction": {"type": "string", "description": "信号语义说明"},
            "produces": {
                "type": "array",
                "description": "产出变量规格列表。定义要从任务结果中提取的字段，每个元素包含 name（输出变量名）和 path（JSON字段路径）。常用字段：vm（虚拟机ID）、host（主机名）、errcode_tracing（错误码）、request_id（请求ID）。",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {
                            "type": "string",
                            "description": "输出变量名，建议大写格式（如 VM_ID、HOST、ERROR_CODE）"
                        },
                        "path": {
                            "type": "string",
                            "description": "JSON 字段路径，支持 | 分隔多路径容错"
                        }
                    },
                    "required": ["name", "path"]
                },
                "default": []
            }
        },
        "required": ["keyword"],
        "additionalProperties": false
    }',
    '[{"keyword": "虚拟机镜像忙", "is_failed": true, "limit": 20, "produces": [{"name": "VM_ID", "path": "vm|object_id"}, {"name": "HOST", "path": "host|hostname"}]}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QKV.dialog: 前端信号-弹框查询
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qkv_dialog',
    '前端信号-弹框日志定位',
    'qkv',
    '无任务/告警承载的页面弹框复合取值能力：在当前主控 /sf/log/today 与 /sf/log/today/vt 检索弹框原文，过滤探针自身后从命中日志提取 END、REQUEST_ID、HOST。存在对应失败任务时优先使用 qkv_task。',
    NULL,
    '{
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "页面弹框原文或可唯一定位的稳定片段"
            },
            "limit": {
                "type": "integer",
                "description": "结构化候选结果上限",
                "default": 100,
                "minimum": 1,
                "maximum": 200
            },
            "paths": {
                "type": "array",
                "items": {"type": "string", "enum": ["/sf/log/today", "/sf/log/today/vt"]},
                "default": ["/sf/log/today", "/sf/log/today/vt"],
                "description": "固定弹框日志搜索域"
            },
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 10, "default": 2},
            "produces": {
                "type": "array",
                "default": [
                    {"name": "END", "path": "end"},
                    {"name": "REQUEST_ID", "path": "request_id"},
                    {"name": "HOST", "path": "host"}
                ]
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 10},
            "instruction": {"type": "string", "description": "历史信号语义说明"}
        },
        "required": ["keyword"],
        "additionalProperties": false
    }',
    '[{"keyword":"编辑显卡核心失败","paths":["/sf/log/today","/sf/log/today/vt"],"context_lines":2,"produces":[{"name":"END","path":"end"},{"name":"REQUEST_ID","path":"request_id"},{"name":"HOST","path":"host"}]}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- ─── QFK 后端信号（消费者）─────────────────────────────────────

-- QFK.log: 后端信号-日志检查和操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_log',
    '后端信号-日志检查和操作',
    'qfk',
    '统一日志消费者：常规日志根只有 /sf/log，whitebox、blackbox、vn-blackbox 与 pods 统一通过 qfk_log + acli log get 获取。/sf/data/local 不是日志族，只允许携带 request_id 做辅助关联搜索。文件名只填 basename；path/parser 默认由代码 Catalog 推断。',
    NULL,
    '{
        "type": "object",
        "properties": {
            "timeout": {
                "type": "integer",
                "minimum": 1,
                "maximum": 300,
                "default": 60,
                "description": "采集/执行超时（秒，1-300）"
            },
            "host": {
                "type": "string",
                "description": "采集目标主机/作用域（如 {{HOST}}），由运行时目标节点解析"
            },
            "instruction": {
                "type": "string",
                "description": "信号语义说明（不直接拼入执行命令）"
            },
            "file": {
                "type": "string",
                "description": "安全日志 basename，如 sfvt_vtpdaemon.log、LOG_ifconfig.txt、messages"
            },
            "path": {
                "type": "string",
                "description": "常规日志仅限 /sf/log；/sf/data/local 仅可与 request_id 同时使用"
            },
            "source_family": {
                "type": "string",
                "enum": ["auto", "whitebox", "blackbox", "vn_blackbox", "pod"],
                "default": "auto"
            },
            "parser": {
                "type": "string",
                "enum": ["plain_text", "timestamped_lines", "timestamped_blocks", "ifconfig_snapshot", "kv_counter_snapshot", "process_snapshot"]
            },
            "time_window": {
                "type": "string",
                "description": "YYYY-MM-DD、YYYY-MM-DD HH、YYYY-MM-DD HH:MM:SS 或 {{ABSOLUTE_TIME}}"
            },
            "request_id": {"type": "string", "description": "调用链 request_id（acli -i）"},
            "context_lines": {"type": "integer", "minimum": 0, "maximum": 50, "default": 0},
            "include_archives": {"type": "boolean", "default": false},
            "archive_precheck": {"type": "string", "enum": ["verified"]},
            "resource_keyword": {
                "type": "string",
                "description": "无 matcher 的变量产出模式所需受控行选择器"
            },
            "matcher": {
                "type": "object",
                "description": "日志判定器；计数器使用 threshold/delta/trend + metric",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "exists"]
                    },
                    "pattern": {
                        "type": ["string", "array"],
                        "description": "(keyword/regex/state) 匹配模式。keyword 支持数组，多个关键字用 mode 决定组合逻辑"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["or", "and", "not"],
                        "default": "or"
                    },
                    "metric": {"type": "string"},
                    "operator": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "!="]},
                    "value": {"type": "number"},
                    "aggregation": {"type": "string", "enum": ["first_number", "last_number", "line_count", "max", "min", "sum"]},
                    "minimum_samples": {"type": "integer", "minimum": 2},
                    "direction": {"type": "string", "enum": ["increasing", "decreasing", "stable"]},
                    "expected": {
                        "type": "boolean",
                        "default": true,
                        "description": "期望结果：true=期望匹配成功（异常判定），false=期望匹配失败（健康判定）"
                    }
                },
                "required": ["type"]
            }
        },
        "required": [],
        "additionalProperties": false
    }',
    '[{"file":"sfvt_vtpdaemon.log","source_family":"auto","matcher":{"type":"keyword","pattern":"Connection reset by peer","mode":"or","expected":true}},{"file":"LOG_ethtool_statistic.txt","source_family":"vn_blackbox","matcher":{"type":"delta","metric":"rx_missed_errors","operator":">","value":0,"minimum_samples":2,"expected":true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QFK.service: 后端信号-服务检查和操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_service',
    '后端信号-服务检查和操作',
    'qfk',
    '后端信号（消费者）：领域服务域为 asv(vt/虚拟平台)、anet(vn/虚拟网络)、asan(vs/虚拟存储)、host(宿主机/容器管理)。当前版本实机只暴露 asv/anet/host，执行前必须以 aCLI capability probe 为准，不能把领域知识冒充为已部署能力。',
    NULL,
    '{
        "type": "object",
        "properties": {
            "container": {
                "type": "string",
                "enum": ["asv", "anet", "host"],
                "default": "asv",
                "description": "当前实机可执行：asv(vt)、anet(vn)、host；asan(vs) 属领域 Catalog，但当前版本未暴露 service namespace"
            },
            "resource_keyword": {
                "type": "string",
                "description": "服务名称，如 redis、mysql、vtpdaemon"
            },
            "command": {
                "type": "string",
                "description": "服务动作，如 status/start/stop/restart；省略时运行时默认 status"
            },
            "host": {
                "type": "string",
                "description": "目标 HCI 主机，由传输层选择 SSH 会话，不拼入 aCLI 命令"
            },
            "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 10},
            "instruction": {"type": "string", "description": "信号语义说明"},
            "matcher": {
                "type": "object",
                "description": "判定器配置，通常使用 state 类型检查 running 状态",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"]
                    },
                    "pattern": {
                        "type": "string",
                        "description": "(state类型) 期望状态值，如 running、stopped、active"
                    },
                    "expected": {
                        "type": "boolean",
                        "default": true
                    }
                },
                "required": ["type"]
            }
        },
        "required": ["resource_keyword"],
        "additionalProperties": false
    }',
    '[{"container": "asv", "resource_keyword": "redis", "command": "status", "matcher": {"type": "state", "pattern": "running", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QFK.system: 后端信号-系统检查和操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_system',
    '后端信号-系统检查和操作',
    'qfk',
    '后端信号（消费者）：执行 acli system 子命令（lsof/ps/lsblk/iostat/smartctl/modinfo 等）。通过 command 指定动作，host 选择目标节点，container 选择受控执行位置。',
    'acli system {{command}}',
    '{
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "acli system 子命令，如 lsof、ps auxf、lsblk、iostat、smartctl -a /dev/sda、modinfo mpt3sas 等"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置。支持 threshold（数值阈值比较）和 keyword（关键字匹配）类型",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"],
                        "description": "判定类型"
                    },
                    "pattern": {
                        "type": ["string", "array"],
                        "description": "(keyword/regex/state) 匹配模式"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["or", "and", "not"],
                        "default": "or",
                        "description": "(keyword专用) 多关键字匹配模式"
                    },
                    "operator": {
                        "type": "string",
                        "enum": [">", ">=", "<", "<=", "==", "!="],
                        "description": "(threshold专用) 比较运算符"
                    },
                    "value": {
                        "type": "number",
                        "description": "(threshold专用) 阈值"
                    },
                    "path": {
                        "type": "string",
                        "description": "(json_path专用) JSON 路径，如 data.status"
                    },
                    "expected_value": {
                        "description": "(json_path专用) 期望值"
                    },
                    "expected": {
                        "type": "boolean",
                        "default": true,
                        "description": "期望结果：true=期望匹配成功（异常判定），false=期望匹配失败（健康判定）"
                    }
                },
                "required": ["type"]
            },
            "host": {
                "type": "string",
                "description": "目标 HCI 主机/作用域（可选，如 {{HOST}}）"
            }
        },
        "required": ["command"]
    }',
    '[
        {"command": "lsof", "matcher": {"type": "keyword", "pattern": ["qcow2", "PID"], "mode": "and", "expected": true}},
        {"command": "iostat", "matcher": {"type": "threshold", "operator": ">", "value": 1000, "expected": true}},
        {"command": "smartctl -a /dev/sda", "matcher": {"type": "threshold", "operator": ">", "value": 200, "expected": true}}
    ]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QFK.vm: 后端信号-虚拟机相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_vm',
    '后端信号-虚拟机相关操作',
    'qfk',
    '后端信号（消费者）：执行虚拟机相关子命令。通过 command 指定具体操作，如 list、config、status 等；host 选择目标节点。',
    'acli vm {{command}}',
    '{
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "acli vm 子命令，如 list、status <vmid>、config <vmid>"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"]
                    },
                    "pattern": {
                        "type": ["string", "array"],
                        "description": "匹配模式"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["or", "and", "not"],
                        "default": "or"
                    },
                    "expected": {
                        "type": "boolean",
                        "default": true
                    }
                },
                "required": ["type"]
            },
            "host": {
                "type": "string",
                "description": "目标 HCI 主机/作用域（可选，如 {{HOST}}）"
            }
        },
        "required": ["command"]
    }',
    '[{"command": "list", "matcher": {"type": "exists", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QFK.network: 后端信号-网络相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_network',
    '后端信号-网络相关操作',
    'qfk',
    '后端信号（消费者）：执行网络相关子命令。通过 command 指定具体操作；host 选择目标节点。',
    'acli network {{command}}',
    '{
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "acli network 子命令"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["or", "and", "not"], "default": "or"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "host": {"type": "string", "description": "目标 HCI 主机/作用域（可选，如 {{HOST}}）"}
        },
        "required": ["command"]
    }',
    '[{"command": "ping 192.168.1.1", "matcher": {"type": "keyword", "pattern": "bytes from", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QFK.storage: 后端信号-存储相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_storage',
    '后端信号-存储相关操作',
    'qfk',
    '后端信号（消费者）：执行存储相关子命令，如 asan disk list、disk status 等。',
    'acli storage {{command}}',
    '{
        "type": "object",
        "properties": {
            "command": {
                "type": "string",
                "description": "acli storage 子命令，如 asan disk list"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["or", "and", "not"], "default": "or"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "host": {"type": "string", "description": "目标 HCI 主机/作用域（可选，如 {{HOST}}）"}
        },
        "required": ["command"]
    }',
    '[{"command": "asan disk list", "matcher": {"type": "keyword", "pattern": ["数据同步", "数据平衡"], "mode": "or", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QFK.hardware: 后端信号-硬件相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_hardware',
    '后端信号-硬件相关操作',
    'qfk',
    '后端信号（消费者）：执行硬件相关子命令，如 sensor list、disk smart 等。',
    'acli hardware {{command}}',
    '{
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "acli hardware 子命令"},
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["or", "and", "not"], "default": "or"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "host": {"type": "string", "description": "目标 HCI 主机/作用域（可选，如 {{HOST}}）"}
        },
        "required": ["command"]
    }',
    '[{"command": "sensor list", "matcher": {"type": "exists", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- QFK.platform: 后端信号-平台相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_platform',
    '后端信号-平台相关操作',
    'qfk',
    '后端信号（消费者）：执行平台相关子命令，如 cluster status、version 等。',
    'acli platform {{command}}',
    '{
        "type": "object",
        "properties": {
            "command": {"type": "string", "description": "acli platform 子命令"},
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "delta", "trend", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["or", "and", "not"], "default": "or"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "host": {"type": "string", "description": "目标 HCI 主机/作用域（可选，如 {{HOST}}）"}
        },
        "required": ["command"]
    }',
    '[{"command": "cluster status", "matcher": {"type": "state", "pattern": "healthy", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO NOTHING;

-- 存量环境和全新环境统一以共享采集契约支持的 QFK 参数作为 Tool Registry 可读投影。
-- 这里仅补齐参数；已有枚举和风险边界继续保留。存量数据库由 027 数据迁移执行相同对齐，
-- 随后服务启动对账会产生新的不可变 Tool Revision。
WITH qfk_contract_patches(tool_name, properties_patch, required_patch) AS (
    VALUES
        ('qfk_log', '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"host":{"type":"string"},"instruction":{"type":"string"}}'::jsonb, NULL::jsonb),
        ('qfk_service', '{"service":{"type":"string"},"action":{"type":"string","enum":["status","start","stop","restart"],"default":"status"}}'::jsonb, '[]'::jsonb),
        ('qfk_system', '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"cluster":{"type":"boolean","default":false},"formatter":{"type":"string","enum":["xml","csv","keyvalue","json"]},"container":{"type":"string","enum":["asv-con","vn-con","vn-agent","vs-cp-manager"]}}'::jsonb, NULL::jsonb),
        ('qfk_vm', '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb, NULL::jsonb),
        ('qfk_network', '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb, NULL::jsonb),
        ('qfk_storage', '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb, NULL::jsonb),
        ('qfk_hardware', '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb, NULL::jsonb),
        ('qfk_platform', '{"timeout":{"type":"integer","minimum":1,"maximum":300,"default":60},"instruction":{"type":"string"},"command_args":{"type":"array","items":{"type":"string"}},"resource_keyword":{"type":"string"}}'::jsonb, NULL::jsonb)
)
UPDATE tool_definition AS tool
SET parameters_schema = CASE
        WHEN patch.required_patch IS NULL THEN jsonb_set(tool.parameters_schema, '{properties}', tool.parameters_schema->'properties' || patch.properties_patch, true)
        ELSE jsonb_set(jsonb_set(tool.parameters_schema, '{properties}', tool.parameters_schema->'properties' || patch.properties_patch, true), '{required}', patch.required_patch, true)
    END,
    updated_at = CURRENT_TIMESTAMP
FROM qfk_contract_patches AS patch
WHERE tool.tool_name = patch.tool_name
  AND (
      NOT COALESCE(tool.parameters_schema->'properties', '{}'::jsonb) @> patch.properties_patch
      OR (patch.required_patch IS NOT NULL AND tool.parameters_schema->'required' IS DISTINCT FROM patch.required_patch)
  );
