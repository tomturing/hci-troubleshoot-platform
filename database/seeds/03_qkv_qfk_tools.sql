-- ============================================================
-- Seed 数据：QKV/QFK 关键信号工具定义
-- Version : 20260716
-- Issue   : T-TOOL-QKV-QFK-001
-- 说明    : 插入 11 条工具定义记录（QKV 3个 + QFK 8个）
-- 幂等键  : tool_name（ON CONFLICT DO UPDATE）
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
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选），用于指定查询的节点"
            },
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
        "required": ["keyword"]
    }',
    '[{"keyword": "磁盘被拔出", "limit": 50, "produces": [{"name": "HOST", "path": "host|hostname"}, {"name": "DISK_SN", "path": "target|object_name"}]}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

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
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            },
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
        "required": ["keyword"]
    }',
    '[{"keyword": "虚拟机镜像忙", "is_failed": true, "limit": 20, "produces": [{"name": "VM_ID", "path": "vm|object_id"}, {"name": "HOST", "path": "host|hostname"}]}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QKV.dialog: 前端信号-弹框查询
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qkv_dialog',
    '前端信号-弹框查询',
    'qkv',
    '前端信号（生产者）：查询弹框或对话日志，按关键字过滤。dialog 类型按行返回，produces 通常不使用（直接返回文本行）。',
    'acli log get -k {{keyword}} -l {{limit}}',
    '{
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "日志关键字过滤。例如：iotimeout、error、failed"
            },
            "limit": {
                "type": "integer",
                "description": "最大返回日志行数",
                "default": 100,
                "minimum": 1,
                "maximum": 200
            },
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            },
            "produces": {
                "type": "array",
                "description": "产出变量规格（dialog 类型通常不使用，按行返回文本）",
                "items": {
                    "type": "object",
                    "properties": {
                        "name": {"type": "string", "description": "输出变量名"},
                        "path": {"type": "string", "description": "JSON 字段路径"}
                    },
                    "required": ["name", "path"]
                },
                "default": []
            }
        },
        "required": ["keyword"]
    }',
    '[{"keyword": "iotimeout", "limit": 50}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- ─── QFK 后端信号（消费者）─────────────────────────────────────

-- QFK.log: 后端信号-日志检查和操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_log',
    '后端信号-日志检查和操作',
    'qfk',
    '后端信号（消费者）：在指定日志文件中搜索关键字，进行布尔判定。支持 target.resource（日志文件名）、target.path（日志路径）、target.time_window（时间范围）。matcher 支持 keyword 类型。',
    'acli log get -k {{keyword}} {{#if target.resource}}-f {{target.resource}}{{/if}} {{#if target.path}}-p {{target.path}}{{/if}}',
    '{
        "type": "object",
        "properties": {
            "keyword": {
                "type": "string",
                "description": "搜索关键字（取自 matcher.pattern 的第一个元素）"
            },
            "target": {
                "type": "object",
                "description": "检查目标定位参数",
                "properties": {
                    "resource": {
                        "type": "string",
                        "description": "日志文件名，如 vtpdaemon.log、mysql-managed.log"
                    },
                    "path": {
                        "type": "string",
                        "description": "日志路径，如 /sf/log/today/"
                    },
                    "time_window": {
                        "type": "string",
                        "description": "时间范围，如 最近1小时、今天"
                    }
                }
            },
            "keywords": {
                "type": "array",
                "description": "匹配关键字列表（用于判定）",
                "items": {"type": "string"}
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置。根据 type 字段决定其他参数：keyword（关键字匹配）、regex（正则）、state（状态）、threshold（阈值）、json_path（JSON路径）、exists（存在性）",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"],
                        "description": "判定类型：keyword=关键字匹配，regex=正则表达式，state=状态值，threshold=数值阈值，json_path=JSON路径取值，exists=存在性"
                    },
                    "pattern": {
                        "type": ["string", "array"],
                        "description": "(keyword/regex/state) 匹配模式。keyword 支持数组，多个关键字用 mode 决定组合逻辑"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["any", "all"],
                        "default": "any",
                        "description": "(keyword专用) 多关键字匹配模式：any=任一匹配，all=全部匹配"
                    },
                    "expected": {
                        "type": "boolean",
                        "default": true,
                        "description": "期望结果：true=期望匹配成功（异常判定），false=期望匹配失败（健康判定）"
                    }
                },
                "required": ["type"]
            },
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            }
        },
        "required": ["keyword"]
    }',
    '[{"keyword": "iotimeout", "target": {"resource": "sfvt_qemu_7436939093432.log", "path": "/sf/log/3/"}, "matcher": {"type": "keyword", "pattern": ["iotimeout"], "mode": "or", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QFK.service: 后端信号-服务检查和操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_service',
    '后端信号-服务检查和操作',
    'qfk',
    '后端信号（消费者）：检查服务状态是否正常。需要指定 container（容器类型：asv/anet/host）和服务名称。matcher 支持 state 类型判定 running/stopped。',
    'acli service {{container}} {{service_name}} status',
    '{
        "type": "object",
        "properties": {
            "container": {
                "type": "string",
                "enum": ["asv", "anet", "host"],
                "description": "容器类型：asv=应用服务容器，anet=网络容器，host=主机进程"
            },
            "service_name": {
                "type": "string",
                "description": "服务名称，如 redis、mysql、vtpdaemon"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置，通常使用 state 类型检查 running 状态",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"]
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
            },
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            }
        },
        "required": ["container", "service_name"]
    }',
    '[{"container": "asv", "service_name": "redis", "matcher": {"type": "state", "pattern": "running", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QFK.system: 后端信号-系统检查和操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_system',
    '后端信号-系统检查和操作',
    'qfk',
    '后端信号（消费者）：执行系统级子命令，封装了 37 个主机级命令（lsof/ps/lsblk/iostat/smartctl/modinfo 等）。通过 sub_command 指定具体命令。matcher 支持 threshold（数值阈值）和 keyword 类型。',
    'acli system {{sub_command}}',
    '{
        "type": "object",
        "properties": {
            "sub_command": {
                "type": "string",
                "description": "acli system 子命令，如 lsof、ps auxf、lsblk、iostat、smartctl -a /dev/sda、modinfo mpt3sas 等"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置。支持 threshold（数值阈值比较）和 keyword（关键字匹配）类型",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"],
                        "description": "判定类型"
                    },
                    "pattern": {
                        "type": ["string", "array"],
                        "description": "(keyword/regex/state) 匹配模式"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["any", "all"],
                        "default": "any",
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
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            }
        },
        "required": ["sub_command"]
    }',
    '[
        {"sub_command": "lsof", "matcher": {"type": "keyword", "pattern": ["qcow2", "PID"], "mode": "and", "expected": true}},
        {"sub_command": "iostat", "matcher": {"type": "threshold", "operator": ">", "value": 1000, "expected": true}},
        {"sub_command": "smartctl -a /dev/sda", "matcher": {"type": "threshold", "operator": ">", "value": 200, "expected": true}}
    ]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QFK.vm: 后端信号-虚拟机相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_vm',
    '后端信号-虚拟机相关操作',
    'qfk',
    '后端信号（消费者）：执行虚拟机相关子命令。通过 sub_command 指定具体操作，如 list、config、status 等。matcher 支持 keyword/state/json_path/exists。',
    'acli vm {{sub_command}}',
    '{
        "type": "object",
        "properties": {
            "sub_command": {
                "type": "string",
                "description": "acli vm 子命令，如 list、status <vmid>、config <vmid>"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {
                        "type": "string",
                        "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"]
                    },
                    "pattern": {
                        "type": ["string", "array"],
                        "description": "匹配模式"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["any", "all"],
                        "default": "any"
                    },
                    "expected": {
                        "type": "boolean",
                        "default": true
                    }
                },
                "required": ["type"]
            },
            "node_ip": {
                "type": "string",
                "description": "目标节点 IP（可选）"
            }
        },
        "required": ["sub_command"]
    }',
    '[{"sub_command": "list", "matcher": {"type": "exists", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QFK.network: 后端信号-网络相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_network',
    '后端信号-网络相关操作',
    'qfk',
    '后端信号（消费者）：执行网络相关子命令。通过 sub_command 指定具体操作，如 ping、connectivity 等。',
    'acli network {{sub_command}}',
    '{
        "type": "object",
        "properties": {
            "sub_command": {
                "type": "string",
                "description": "acli network 子命令"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["any", "all"], "default": "any"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "node_ip": {"type": "string", "description": "目标节点 IP（可选）"}
        },
        "required": ["sub_command"]
    }',
    '[{"sub_command": "ping 192.168.1.1", "matcher": {"type": "keyword", "pattern": "bytes from", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QFK.storage: 后端信号-存储相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_storage',
    '后端信号-存储相关操作',
    'qfk',
    '后端信号（消费者）：执行存储相关子命令，如 asan disk list、disk status 等。',
    'acli storage {{sub_command}}',
    '{
        "type": "object",
        "properties": {
            "sub_command": {
                "type": "string",
                "description": "acli storage 子命令，如 asan disk list"
            },
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["any", "all"], "default": "any"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "node_ip": {"type": "string", "description": "目标节点 IP（可选）"}
        },
        "required": ["sub_command"]
    }',
    '[{"sub_command": "asan disk list", "matcher": {"type": "keyword", "pattern": ["数据同步", "数据平衡"], "mode": "or", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QFK.hardware: 后端信号-硬件相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_hardware',
    '后端信号-硬件相关操作',
    'qfk',
    '后端信号（消费者）：执行硬件相关子命令，如 sensor list、disk smart 等。',
    'acli hardware {{sub_command}}',
    '{
        "type": "object",
        "properties": {
            "sub_command": {"type": "string", "description": "acli hardware 子命令"},
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["any", "all"], "default": "any"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "node_ip": {"type": "string", "description": "目标节点 IP（可选）"}
        },
        "required": ["sub_command"]
    }',
    '[{"sub_command": "sensor list", "matcher": {"type": "exists", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();

-- QFK.platform: 后端信号-平台相关操作
INSERT INTO tool_definition (
    tool_name, display_name, category, description,
    usage_template, parameters_schema, examples, risk_level, is_active
) VALUES (
    'qfk_platform',
    '后端信号-平台相关操作',
    'qfk',
    '后端信号（消费者）：执行平台相关子命令，如 cluster status、version 等。',
    'acli platform {{sub_command}}',
    '{
        "type": "object",
        "properties": {
            "sub_command": {"type": "string", "description": "acli platform 子命令"},
            "matcher": {
                "type": "object",
                "description": "判定器配置",
                "properties": {
                    "type": {"type": "string", "enum": ["keyword", "regex", "state", "threshold", "json_path", "exists"]},
                    "pattern": {"type": ["string", "array"]},
                    "mode": {"type": "string", "enum": ["any", "all"], "default": "any"},
                    "expected": {"type": "boolean", "default": true}
                },
                "required": ["type"]
            },
            "node_ip": {"type": "string", "description": "目标节点 IP（可选）"}
        },
        "required": ["sub_command"]
    }',
    '[{"sub_command": "cluster status", "matcher": {"type": "state", "pattern": "healthy", "expected": true}}]',
    1,
    true
) ON CONFLICT (tool_name) DO UPDATE SET
    display_name = EXCLUDED.display_name,
    category = EXCLUDED.category,
    description = EXCLUDED.description,
    usage_template = EXCLUDED.usage_template,
    parameters_schema = EXCLUDED.parameters_schema,
    examples = EXCLUDED.examples,
    risk_level = EXCLUDED.risk_level,
    is_active = EXCLUDED.is_active,
    updated_at = NOW();
