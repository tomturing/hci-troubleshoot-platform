-- ============================================================
-- Seed 数据：在线/离线诊断 KBD 信号契约样例集
-- 样例集标识：diagnosis-signal-matrix-v1
--
-- 设计约束：
--   1. 只创建 draft KBD，不自动审核、不自动发布、不生成离线派生资源；
--   2. 通过 metadata.sample_suite 检索，不允许业务代码判断具体 KBD ID；
--   3. 覆盖 3 类 QKV、8 类 QFK、7 类 Matcher 以及当前 Signal v2 字段；
--   4. 只使用 Tool Registry/Shared Resolution Runtime 已声明的只读命令；
--   5. 已发布样例不覆盖；仅升级仍为 draft 的旧版样例，修复测试契约后可重新审核。
-- ============================================================

WITH sample_rows (
    support_id,
    domain_hint,
    title,
    problem_description,
    alert_info,
    steps_text,
    root_cause,
    solution,
    signals_json,
    sample_tools
) AS (
    VALUES
    (
        'SAMPLE-SIG-VM',
        '虚拟机',
        '【诊断样例】虚拟机状态异常与任务变量链',
        '虚拟机操作失败，需要从失败任务中定位宿主机与虚拟机标识，并核对虚拟机当前状态。',
        '任务列表中出现“启动虚拟机”失败记录。',
        '先查询失败任务并提取 HOST、VM_ID、END，再执行只读虚拟机状态与清单检查。',
        '样例环境中的虚拟机状态与任务目标不一致。',
        '本条仅用于验证诊断采集和判定链路，不包含自动处置动作。',
        $signals$
        {
          "schema_version": 2,
          "signals": [
            {
              "id": "vm_task_context",
              "role": "should",
              "acquire": {
                "tool": "qkv_task",
                "args": {
                  "keyword": "启动虚拟机",
                  "is_failed": true,
                  "limit": 20,
                  "timeout": 60,
                  "instruction": "查询失败的虚拟机启动任务并产出诊断变量"
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "action": "collect",
                "source": "task",
                "target": "cluster",
                "container": "host",
                "requires": [],
                "produces": [
                  {"name": "HOST", "type": "string", "path": "host|hostname"},
                  {"name": "VM_ID", "type": "integer", "path": "vm|object_id"},
                  {"name": "END", "type": "string", "path": "end"}
                ]
              },
              "provenance": {
                "category": "frontend",
                "method": "sql_sample",
                "source_section": "steps_text",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "失败任务包含虚拟机、宿主机与结束时间字段。",
                "source_refs": ["kbd:steps_text"]
              },
              "review": {"require_human_confirm": false, "notes": "审核时确认现场任务关键字。"}
            },
            {
              "id": "vm_status_must",
              "role": "must",
              "acquire": {
                "tool": "qfk_vm",
                "args": {
                  "command": "status get",
                  "command_args": ["--vm-id", "{{VM_ID}}"],
                  "formatter": "json",
                  "host": "{{HOST}}",
                  "resource_keyword": "{{VM_ID}}",
                  "timeout": 60,
                  "instruction": "在目标宿主机读取虚拟机状态"
                }
              },
              "match": {
                "type": "exists",
                "expected": true,
                "extract": {
                  "type": "json",
                  "source": "stdout",
                  "path": "data[0]",
                  "cardinality": "exactly_one",
                  "value_mode": "object"
                }
              },
              "orchestrate": {
                "phase": "diagnostic",
                "requires": ["HOST", "VM_ID"],
                "produces": []
              },
              "provenance": {
                "category": "backend",
                "method": "sql_sample",
                "source_section": "steps_text",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "虚拟机状态查询应返回目标对象。",
                "source_refs": ["kbd:steps_text"]
              },
              "review": {"require_human_confirm": false, "notes": "确认 VM_ID 来自上游任务。"}
            },
            {
              "id": "vm_list_context",
              "role": "context",
              "acquire": {
                "tool": "qfk_vm",
                "args": {
                  "command": "list",
                  "command_args": [],
                  "formatter": "json",
                  "host": "{{HOST}}",
                  "resource_keyword": "{{VM_ID}}",
                  "timeout": 60,
                  "instruction": "读取虚拟机清单并产出结构化对象"
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "requires": ["HOST"],
                "produces": [
                  {
                    "name": "VM_OBJECT",
                    "type": "object",
                    "extract": {
                      "type": "json",
                      "source": "stdout",
                      "path": "data[0]",
                      "cardinality": "exactly_one",
                      "value_mode": "object"
                    }
                  }
                ]
              },
              "provenance": {
                "category": "backend",
                "method": "sql_sample",
                "source_section": "steps_text",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "虚拟机清单用于补充上下文。",
                "source_refs": ["kbd:steps_text"]
              },
              "review": {"require_human_confirm": false, "notes": "验证 JSON 路径是否符合目标版本。"}
            }
          ],
          "rejected_candidates": [],
          "verification_contract": {
            "schema_version": 1,
            "case_id": "SAMPLE-SIG-VM",
            "scope": {
              "products": ["HCI"],
              "versions": ["sample"],
              "components": ["virtual-machine"],
              "topology_constraints": ["single-or-multi-node"]
            },
            "variables": {
              "HOST": {"type": "string", "description": "目标宿主机"},
              "VM_ID": {"type": "integer", "description": "虚拟机标识"},
              "SAMPLE_LOAD": {"type": "number", "description": "数值类型覆盖样例"},
              "SAMPLE_ENABLED": {"type": "boolean", "description": "布尔类型覆盖样例"},
              "SAMPLE_ITEMS": {"type": "array", "description": "数组类型覆盖样例"}
            },
            "evidence_policy": {
              "must": ["vm_status_must"],
              "should": ["vm_task_context"],
              "exclude": [],
              "context": ["vm_list_context"],
              "minimum_should": 1,
              "on_missing_must": "inconclusive"
            }
          }
        }
        $signals$::jsonb,
        '["qkv_task", "qfk_vm"]'::jsonb
    ),
    (
        'SAMPLE-SIG-CORE',
        '平台',
        '【诊断样例】告警、服务与系统容量联合检查',
        '平台出现服务异常告警，需要定位目标主机、检查服务状态并核对日志分区容量。',
        '活跃告警包含“服务不可用”关键字。',
        '查询告警产出 HOST 与 SERVICE；检查服务只读状态；在集群范围读取 df 结果并做容量阈值判定。',
        '服务不可用可能与日志分区容量过高相关。',
        '本条仅验证多信号编排、文本取值和阈值判定，不执行服务启停。',
        $signals$
        {
          "schema_version": 2,
          "signals": [
            {
              "id": "core_alert_context",
              "role": "should",
              "acquire": {
                "tool": "qkv_alert",
                "args": {
                  "keyword": "服务不可用",
                  "limit": 50,
                  "alert_type": "service",
                  "timeout": 60,
                  "instruction": "读取服务类告警并产出主机与服务名"
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "requires": [],
                "produces": [
                  {"name": "HOST", "type": "string", "path": "host|hostname"},
                  {"name": "SERVICE", "type": "string", "path": "service|object_name"}
                ]
              },
              "provenance": {
                "category": "frontend",
                "method": "sql_sample",
                "source_section": "alert_info",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "服务告警提供目标主机和服务对象。",
                "source_refs": ["kbd:alert_info"]
              },
              "review": {"require_human_confirm": false, "notes": "审核告警关键字与字段路径。"}
            },
            {
              "id": "core_service_must",
              "role": "must",
              "acquire": {
                "tool": "qfk_service",
                "args": {
                  "service": "asv-manager",
                  "resource_keyword": "asv-manager",
                  "container": "asv",
                  "host": "{{HOST}}",
                  "command": "status",
                  "action": "status",
                  "timeout": 60,
                  "instruction": "读取服务运行状态，禁止启停"
                }
              },
              "match": {
                "type": "state",
                "pattern": "running",
                "mode": "or",
                "expected": true,
                "extract": {
                  "type": "text",
                  "rows": {"mode": "all"},
                  "cardinality": "exactly_one",
                  "source": "stdout",
                  "value_mode": "string"
                }
              },
              "orchestrate": {
                "phase": "diagnostic",
                "requires": ["HOST"],
                "produces": []
              },
              "provenance": {
                "category": "backend",
                "method": "sql_sample",
                "source_section": "steps_text",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "服务状态输出应包含 running。",
                "source_refs": ["kbd:steps_text"]
              },
              "review": {"require_human_confirm": false, "notes": "只允许 status 动作。"}
            },
            {
              "id": "core_disk_threshold",
              "role": "must",
              "acquire": {
                "tool": "qfk_system",
                "args": {
                  "command": "df",
                  "command_args": ["-P", "/sf/log"],
                  "host": "cluster",
                  "cluster": true,
                  "formatter": "json",
                  "container": "asv-con",
                  "timeout": 90,
                  "instruction": "在集群节点的受控容器域读取日志分区容量"
                }
              },
              "match": {
                "type": "threshold",
                "expected": true,
                "value": 80,
                "operator": ">=",
                "aggregation": "max",
                "extract": {
                  "type": "text",
                  "delimiter": "whitespace",
                  "cardinality": "last",
                  "source": "stdout",
                  "value_mode": "number",
                  "ai_extract": {"instruction": "仅从筛选后的 Use% 列提取百分比数值。"},
                  "parser": "whitespace_table",
                  "header": {
                    "mode": "contains",
                    "required": ["Filesystem", "Use%"],
                    "case_sensitive": false
                  },
                  "rows": {
                    "mode": "keywords",
                    "include": ["/sf/log"],
                    "exclude": ["tmpfs"],
                    "scope": "same_record",
                    "include_mode": "all",
                    "exclude_mode": "any",
                    "case_sensitive": true
                  },
                  "columns": [
                    {
                      "key": "FILESYSTEM",
                      "selector": {"by": "index", "index": 1},
                      "value_mode": "string"
                    },
                    {
                      "key": "USED_PERCENT",
                      "selector": {"by": "header", "name": "Use%", "aliases": ["Capacity"]},
                      "value_mode": "number"
                    }
                  ],
                  "value_key": "USED_PERCENT"
                }
              },
              "orchestrate": {
                "phase": "diagnostic",
                "action": "inspect",
                "source": "host-os",
                "target": "cluster",
                "container": "asv-con",
                "requires": [],
                "produces": []
              },
              "provenance": {
                "category": "backend",
                "method": "sql_sample",
                "source_section": "steps_text",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "日志分区容量达到阈值会影响服务写入。",
                "source_refs": ["kbd:steps_text"]
              },
              "review": {"require_human_confirm": false, "notes": "确认 formatter 与目标版本兼容。"}
            }
          ],
          "rejected_candidates": [],
          "verification_contract": {
            "schema_version": 1,
            "case_id": "SAMPLE-SIG-CORE",
            "scope": {"products": ["HCI"], "versions": ["sample"], "components": ["service", "system"], "topology_constraints": ["cluster"]},
            "variables": {},
            "evidence_policy": {
              "must": ["core_service_must", "core_disk_threshold"],
              "should": ["core_alert_context"],
              "exclude": [],
              "context": [],
              "minimum_should": 1,
              "on_missing_must": "inconclusive"
            }
          }
        }
        $signals$::jsonb,
        '["qkv_alert", "qfk_service", "qfk_system"]'::jsonb
    ),
    (
        'SAMPLE-SIG-LOG',
        '网络',
        '【诊断样例】弹框请求链与日志多判定模式',
        '页面弹框提示网络处理失败，需要提取 Request ID 并在受控日志源中完成关键字、差值、趋势和变量产出检查。',
        '弹框原文包含“网络处理失败”。',
        '定位弹框记录，提取 REQUEST_ID、HOST、END；再查询网络计数器快照并执行确定性判定。',
        '样例网络计数器持续增长并出现失败关键字。',
        '本条只用于验证日志采集、判定和变量产出，不修改网络配置。',
        $signals$
        {
          "schema_version": 2,
          "signals": [
            {
              "id": "log_dialog_context",
              "role": "should",
              "acquire": {
                "tool": "qkv_dialog",
                "args": {
                  "keyword": "网络处理失败",
                  "paths": ["/sf/log/today", "/sf/log/today/vt"],
                  "context_lines": 3,
                  "limit": 30,
                  "timeout": 60,
                  "instruction": "从固定弹框日志域提取关联变量"
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "requires": [],
                "produces": [
                  {"name": "REQUEST_ID", "type": "string", "path": "request_id"},
                  {"name": "HOST", "type": "string", "path": "host"},
                  {"name": "END", "type": "string", "path": "end"}
                ]
              },
              "provenance": {
                "category": "frontend",
                "method": "sql_sample",
                "source_section": "alert_info",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "弹框日志上下文可提供 Request ID 与目标主机。",
                "source_refs": ["kbd:alert_info"]
              },
              "review": {"require_human_confirm": false, "notes": "确认弹框关键片段足够稳定。"}
            },
            {
              "id": "log_keyword_must",
              "role": "must",
              "acquire": {
                "tool": "qfk_log",
                "args": {
                  "resource_keyword": "network-counter",
                  "host": "{{HOST}}",
                  "file": "LOG_ethtool_statistic.txt",
                  "path": "/sf/log/vn-blackbox/today",
                  "time_window": "2026-08-12 10:00:00",
                  "source_family": "vn_blackbox",
                  "parser": "kv_counter_snapshot",
                  "request_id": "{{REQUEST_ID}}",
                  "context_lines": 5,
                  "include_archives": true,
                  "archive_precheck": "verified",
                  "timeout": 120,
                  "instruction": "按请求链读取网络计数器快照及归档"
                }
              },
              "match": {
                "type": "keyword",
                "pattern": ["drop", "error"],
                "mode": "and",
                "expected": true,
                "extract": {
                  "type": "text",
                  "rows": {
                    "mode": "keywords",
                    "include": ["drop", "error"],
                    "exclude": ["probe"],
                    "scope": "same_record",
                    "include_mode": "all",
                    "exclude_mode": "any",
                    "case_sensitive": false
                  },
                  "cardinality": "all",
                  "source": "stdout",
                  "value_mode": "array"
                }
              },
              "orchestrate": {
                "phase": "diagnostic",
                "requires": ["HOST", "REQUEST_ID"],
                "produces": []
              },
              "provenance": {
                "category": "backend",
                "method": "sql_sample",
                "source_section": "steps_text",
                "confidence": 1.0,
                "risk": 0,
                "needs_review": false,
                "evidence": "同一条网络记录同时出现 drop 与 error。",
                "source_refs": ["kbd:steps_text"]
              },
              "review": {"require_human_confirm": false, "notes": "归档搜索前置检查必须保持 verified。"}
            },
            {
              "id": "log_delta_should",
              "role": "should",
              "acquire": {
                "tool": "qfk_log",
                "args": {
                  "file": "LOG_ethtool_statistic.txt",
                  "source_family": "vn_blackbox",
                  "parser": "kv_counter_snapshot",
                  "resource_keyword": "rx_dropped",
                  "host": "{{HOST}}",
                  "timeout": 90,
                  "instruction": "读取丢包计数器差值"
                }
              },
              "match": {
                "type": "delta",
                "expected": true,
                "value": 10,
                "operator": ">=",
                "aggregation": "last_number",
                "minimum_samples": 2,
                "extract": {
                  "type": "text",
                  "parser": "whitespace_table",
                  "header": {"mode": "contains", "required": ["Metric", "Value"], "case_sensitive": false},
                  "rows": {"mode": "keywords", "include": ["rx_dropped"], "scope": "same_record", "include_mode": "all", "exclude_mode": "any", "case_sensitive": true},
                  "cardinality": "all",
                  "source": "stdout",
                  "value_mode": "array",
                  "columns": [{"key": "VALUE", "selector": {"by": "header", "name": "Value"}, "value_mode": "number"}],
                  "value_key": "VALUE"
                }
              },
              "orchestrate": {"phase": "diagnostic", "requires": ["HOST"], "produces": []},
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "丢包计数器差值达到阈值。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "样本按日志顺序计算差值。"}
            },
            {
              "id": "log_trend_should",
              "role": "should",
              "acquire": {
                "tool": "qfk_log",
                "args": {
                  "file": "LOG_ethtool_statistic.txt",
                  "source_family": "vn_blackbox",
                  "parser": "kv_counter_snapshot",
                  "resource_keyword": "tx_dropped",
                  "host": "{{HOST}}",
                  "timeout": 90,
                  "instruction": "读取丢包计数器趋势"
                }
              },
              "match": {
                "type": "trend",
                "expected": true,
                "aggregation": "max",
                "minimum_samples": 3,
                "direction": "increasing",
                "extract": {
                  "type": "text",
                  "parser": "whitespace_table",
                  "header": {"mode": "contains", "required": ["Metric", "Value"], "case_sensitive": false},
                  "rows": {"mode": "keywords", "include": ["tx_dropped"], "scope": "same_record", "include_mode": "all", "exclude_mode": "any", "case_sensitive": true},
                  "cardinality": "all",
                  "source": "stdout",
                  "value_mode": "array",
                  "columns": [{"key": "VALUE", "selector": {"by": "header", "name": "Value"}, "value_mode": "number"}],
                  "value_key": "VALUE"
                }
              },
              "orchestrate": {"phase": "diagnostic", "requires": ["HOST"], "produces": []},
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "丢包计数器呈递增趋势。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "至少需要三个时间有序样本。"}
            },
            {
              "id": "log_lines_context",
              "role": "context",
              "acquire": {
                "tool": "qfk_log",
                "args": {
                  "file": "LOG_ethtool_statistic.txt",
                  "source_family": "vn_blackbox",
                  "parser": "kv_counter_snapshot",
                  "host": "{{HOST}}",
                  "request_id": "{{REQUEST_ID}}",
                  "resource_keyword": "error",
                  "timeout": 60,
                  "instruction": "提取有界日志行作为变量"
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "requires": ["HOST", "REQUEST_ID"],
                "produces": [
                  {
                    "name": "NETWORK_ERROR_LINES",
                    "type": "array",
                    "extract": {
                      "type": "text",
                      "rows": {"mode": "keywords", "include": ["error"], "exclude": ["probe"], "scope": "same_record", "include_mode": "all", "exclude_mode": "any", "case_sensitive": false},
                      "cardinality": "all",
                      "source": "stdout",
                      "value_mode": "array"
                    }
                  }
                ]
              },
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "有界错误行供后续诊断上下文使用。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "产出模式不得同时配置 match。"}
            }
          ],
          "rejected_candidates": [],
          "verification_contract": {
            "schema_version": 1,
            "case_id": "SAMPLE-SIG-LOG",
            "scope": {"products": ["HCI"], "versions": ["sample"], "components": ["network-log"], "topology_constraints": ["target-host"]},
            "variables": {},
            "evidence_policy": {
              "must": ["log_keyword_must"],
              "should": ["log_dialog_context", "log_delta_should", "log_trend_should"],
              "exclude": [],
              "context": ["log_lines_context"],
              "minimum_should": 1,
              "on_missing_must": "inconclusive"
            }
          }
        }
        $signals$::jsonb,
        '["qkv_dialog", "qfk_log"]'::jsonb
    ),
    (
        'SAMPLE-SIG-NET-STO',
        '存储',
        '【诊断样例】网络正则与存储差值检查',
        '网络接口与存储卷同时出现性能异常，需要通过只读命令分别采集并判定。',
        '活跃告警包含“网络链路状态异常告警”，并伴随存储 I/O 差值超过预期。',
        '读取网络接口清单并进行正则判断；读取存储卷 I/O 并进行差值判断。',
        '网络链路状态和存储 I/O 指标共同异常。',
        '本条用于覆盖网络、存储消费者及索引行选择，不执行配置变更。',
        $signals$
        {
          "schema_version": 2,
          "signals": [
            {
              "id": "network_storage_alert_context",
              "role": "should",
              "acquire": {
                "tool": "qkv_alert",
                "args": {
                  "keyword": "网络链路状态异常告警",
                  "limit": 50,
                  "alert_type": "network",
                  "timeout": 60
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "requires": [],
                "produces": [
                  {"name": "HOST", "type": "string", "path": "host|hostname|target"},
                  {"name": "END", "type": "string", "path": "end"},
                  {"name": "ALERT_TYPE", "type": "string", "path": "alert_type|type"}
                ]
              },
              "provenance": {"category": "frontend", "method": "sql_sample", "source_section": "alert_info", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "网络链路或存储性能告警提供目标主机、发生时间和告警类型。", "source_refs": ["kbd:alert_info"]},
              "review": {"require_human_confirm": false, "notes": "审核告警关键字是否与目标版本的分类基线一致。"}
            },
            {
              "id": "network_regex_must",
              "role": "must",
              "acquire": {
                "tool": "qfk_network",
                "args": {
                  "command": "nic list",
                  "command_args": [],
                  "formatter": "keyvalue",
                  "host": "{{HOST}}",
                  "resource_keyword": "eth0",
                  "timeout": 60,
                  "instruction": "读取网卡清单并检查链路状态"
                }
              },
              "match": {
                "type": "regex",
                "pattern": "(?i)eth0\\s+.*up",
                "mode": "or",
                "expected": true,
                "extract": {
                  "type": "text",
                  "rows": {"mode": "keywords", "include": ["eth0"], "scope": "same_record", "include_mode": "all", "exclude_mode": "any", "case_sensitive": false},
                  "cardinality": "first",
                  "source": "stdout",
                  "value_mode": "string"
                }
              },
              "orchestrate": {"phase": "diagnostic", "requires": ["HOST"], "produces": []},
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "网卡清单应显示 eth0 为 up。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "stderr 仅用于覆盖输出来源字段，现场可改为 stdout。"}
            },
            {
              "id": "storage_delta_should",
              "role": "should",
              "acquire": {
                "tool": "qfk_storage",
                "args": {
                  "command": "asan volume iostat",
                  "command_args": [],
                  "formatter": "csv",
                  "host": "{{HOST}}",
                  "resource_keyword": "sample-volume",
                  "timeout": 90,
                  "instruction": "读取存储卷 I/O 指标"
                }
              },
              "match": {
                "type": "delta",
                "expected": true,
                "value": 100,
                "operator": ">",
                "aggregation": "last_number",
                "minimum_samples": 2,
                "extract": {
                  "type": "text",
                  "parser": "whitespace_table",
                  "header": {"mode": "contains", "required": ["Volume", "IOPS"], "case_sensitive": false},
                  "rows": {"mode": "keywords", "include": ["sample-volume"], "scope": "same_record", "include_mode": "all", "exclude_mode": "any", "case_sensitive": true},
                  "cardinality": "all",
                  "source": "stdout",
                  "value_mode": "array",
                  "columns": [{"key": "IOPS", "selector": {"by": "header", "name": "IOPS"}, "value_mode": "number"}],
                  "value_key": "IOPS"
                }
              },
              "orchestrate": {"phase": "diagnostic", "requires": ["HOST"], "produces": []},
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "存储卷 IOPS 差值超过阈值。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "AI 只提取数值，不参与是否命中判定。"}
            }
          ],
          "rejected_candidates": [],
          "verification_contract": {
            "schema_version": 1,
            "case_id": "SAMPLE-SIG-NET-STO",
            "scope": {"products": ["HCI"], "versions": ["sample"], "components": ["network", "storage"], "topology_constraints": ["target-host"]},
            "variables": {},
            "evidence_policy": {
              "must": ["network_regex_must"],
              "should": ["network_storage_alert_context", "storage_delta_should"],
              "exclude": [],
              "context": [],
              "minimum_should": 1,
              "on_missing_must": "inconclusive"
            }
          }
        }
        $signals$::jsonb,
        '["qkv_alert", "qfk_network", "qfk_storage"]'::jsonb
    ),
    (
        'SAMPLE-SIG-HW-PLT',
        '硬件',
        '【诊断样例】硬件趋势与平台信息采集',
        '硬件状态波动且平台版本信息需要补充，需通过只读硬件与平台命令采集证据。',
        '活跃告警包含“硬件温度异常告警”，且硬件指标呈递增趋势。',
        '读取 GPU 配置并判定趋势；读取平台版本是否存在；提取平台信息对象。',
        '硬件配置趋势与平台环境信息需要联合分析。',
        '本条用于覆盖硬件、平台消费者和 JSON 变量产出，不执行配置修改。',
        $signals$
        {
          "schema_version": 2,
          "signals": [
            {
              "id": "hardware_alert_context",
              "role": "should",
              "acquire": {
                "tool": "qkv_alert",
                "args": {
                  "keyword": "硬件温度异常告警",
                  "limit": 50,
                  "alert_type": "hardware",
                  "timeout": 60
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "requires": [],
                "produces": [
                  {"name": "HOST", "type": "string", "path": "host|hostname|target"},
                  {"name": "END", "type": "string", "path": "end"},
                  {"name": "HARDWARE_TYPE", "type": "string", "path": "hardware_type|object_type|type"}
                ]
              },
              "provenance": {"category": "frontend", "method": "sql_sample", "source_section": "alert_info", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "硬件异常告警提供目标主机、发生时间和硬件类型。", "source_refs": ["kbd:alert_info"]},
              "review": {"require_human_confirm": false, "notes": "审核硬件告警关键字与对象字段路径。"}
            },
            {
              "id": "hardware_trend_must",
              "role": "must",
              "acquire": {
                "tool": "qfk_hardware",
                "args": {
                  "command": "gpu config get",
                  "command_args": [],
                  "formatter": "xml",
                  "host": "{{HOST}}",
                  "resource_keyword": "gpu0",
                  "timeout": 90,
                  "instruction": "读取 GPU 配置指标并进行趋势判断"
                }
              },
              "match": {
                "type": "trend",
                "expected": true,
                "aggregation": "max",
                "minimum_samples": 3,
                "direction": "increasing",
                "extract": {
                  "type": "text",
                  "parser": "whitespace_table",
                  "header": {"mode": "contains", "required": ["Metric", "Value"], "case_sensitive": false},
                  "rows": {"mode": "keywords", "include": ["gpu_temperature"], "scope": "same_record", "include_mode": "all", "exclude_mode": "any", "case_sensitive": true},
                  "cardinality": "all",
                  "source": "stdout",
                  "value_mode": "array",
                  "columns": [{"key": "VALUE", "selector": {"by": "header", "name": "Value"}, "value_mode": "number"}],
                  "value_key": "VALUE"
                }
              },
              "orchestrate": {"phase": "diagnostic", "requires": ["HOST"], "produces": []},
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "GPU 温度样本呈递增趋势。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "确认硬件命令在目标版本可用。"}
            },
            {
              "id": "platform_unsupported_exclude",
              "role": "exclude",
              "acquire": {
                "tool": "qfk_platform",
                "args": {
                  "command": "version get",
                  "command_args": [],
                  "formatter": "keyvalue",
                  "host": "{{HOST}}",
                  "resource_keyword": "version",
                  "timeout": 60,
                  "instruction": "读取平台版本信息"
                }
              },
              "match": {
                "type": "keyword",
                "pattern": "unsupported",
                "mode": "or",
                "expected": true,
                "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "first", "source": "stdout", "value_mode": "string"}
              },
              "orchestrate": {"phase": "diagnostic", "requires": ["HOST"], "produces": []},
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "平台明确返回 unsupported 时排除当前案例。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "正常版本输出不得触发排除证据。"}
            },
            {
              "id": "platform_info_context",
              "role": "context",
              "acquire": {
                "tool": "qfk_platform",
                "args": {
                  "command": "info get",
                  "command_args": [],
                  "formatter": "json",
                  "host": "{{HOST}}",
                  "resource_keyword": "platform-info",
                  "timeout": 60,
                  "instruction": "读取平台信息并产出结构化对象"
                }
              },
              "match": null,
              "orchestrate": {
                "phase": "diagnostic",
                "requires": ["HOST"],
                "produces": [
                  {
                    "name": "PLATFORM_INFO",
                    "type": "object",
                    "extract": {"type": "json", "source": "stdout", "path": "data", "cardinality": "exactly_one", "value_mode": "object"}
                  },
                  {
                    "name": "PLATFORM_VERSION",
                    "type": "string",
                    "extract": {"type": "json", "source": "stdout", "path": "data.version", "cardinality": "first", "value_mode": "string"}
                  }
                ]
              },
              "provenance": {"category": "backend", "method": "sql_sample", "source_section": "steps_text", "confidence": 1.0, "risk": 0, "needs_review": false, "evidence": "平台信息用于补充诊断上下文。", "source_refs": ["kbd:steps_text"]},
              "review": {"require_human_confirm": false, "notes": "产出变量使用声明式 JSON 取值。"}
            }
          ],
          "rejected_candidates": [],
          "verification_contract": {
            "schema_version": 1,
            "case_id": "SAMPLE-SIG-HW-PLT",
            "scope": {"products": ["HCI"], "versions": ["sample"], "components": ["hardware", "platform"], "topology_constraints": ["target-host"]},
            "variables": {},
            "evidence_policy": {
              "must": ["hardware_trend_must"],
              "should": ["hardware_alert_context"],
              "exclude": ["platform_unsupported_exclude"],
              "context": ["platform_info_context"],
              "minimum_should": 1,
              "on_missing_must": "inconclusive"
            }
          }
        }
        $signals$::jsonb,
        '["qkv_alert", "qfk_hardware", "qfk_platform"]'::jsonb
    )
),
resolved_rows AS (
    SELECT
        sample_rows.*,
        (
            SELECT category.code
            FROM kb_category AS category
            WHERE category.is_active = true
              AND category.domain = sample_rows.domain_hint
              AND category.code IS NOT NULL
              AND length(category.code) <= 32
            ORDER BY category.level DESC, category.code
            LIMIT 1
        ) AS suggested_category_id
    FROM sample_rows
)
INSERT INTO kbd_entry (
    support_id,
    title,
    problem_description,
    alert_info,
    steps_text,
    root_cause,
    solution,
    operational_impact,
    is_temporary,
    recommendations,
    signals_json,
    images_json,
    content_md,
    content_raw,
    metadata,
    category_id,
    ai_category_id,
    ai_category_conf,
    ai_category_reason,
    status
)
SELECT
    support_id,
    title,
    problem_description,
    alert_info,
    steps_text,
    root_cause,
    solution,
    '仅用于测试环境验证在线诊断与离线诊断的读取、同步、采集和判定流程。',
    '否',
    '请由审核人员核对分类、命令模板、变量链与证据作用后再发布。',
    signals_json,
    '[]'::jsonb,
    '# 问题描述' || chr(10) || chr(10) || problem_description || chr(10) || chr(10)
      || '# 告警信息' || chr(10) || chr(10) || alert_info || chr(10) || chr(10)
      || '# 有效排查步骤' || chr(10) || chr(10) || steps_text || chr(10) || chr(10)
      || '# 根因' || chr(10) || chr(10) || root_cause || chr(10) || chr(10)
      || '# 解决方案' || chr(10) || chr(10) || solution,
    problem_description || chr(10) || alert_info || chr(10) || steps_text || chr(10) || root_cause || chr(10) || solution,
    jsonb_build_object(
        'is_test_sample', true,
        'sample_suite', 'diagnosis-signal-matrix-v1',
        'sample_suite_label', '在线/离线诊断 Signal v2 全量样例',
        'sample_purpose', 'online_offline_diagnosis',
        'domain_hint', domain_hint,
        'signal_tools', sample_tools,
        'seed_version', 4
    ),
    NULL,
    suggested_category_id,
    CASE WHEN suggested_category_id IS NULL THEN NULL ELSE 1.0 END,
    CASE
        WHEN suggested_category_id IS NULL THEN '当前分类树中未找到对应技术域，请审核人员手工选择分类'
        ELSE 'SQL 样例按技术域给出分类建议，仍须审核人员确认'
    END,
    'draft'
FROM resolved_rows
ON CONFLICT (support_id) DO UPDATE SET
    title = EXCLUDED.title,
    problem_description = EXCLUDED.problem_description,
    alert_info = EXCLUDED.alert_info,
    steps_text = EXCLUDED.steps_text,
    root_cause = EXCLUDED.root_cause,
    solution = EXCLUDED.solution,
    operational_impact = EXCLUDED.operational_impact,
    is_temporary = EXCLUDED.is_temporary,
    recommendations = EXCLUDED.recommendations,
    signals_json = EXCLUDED.signals_json,
    images_json = EXCLUDED.images_json,
    content_md = EXCLUDED.content_md,
    content_raw = EXCLUDED.content_raw,
    metadata = EXCLUDED.metadata,
    category_id = EXCLUDED.category_id,
    ai_category_id = EXCLUDED.ai_category_id,
    ai_category_conf = EXCLUDED.ai_category_conf,
    ai_category_reason = EXCLUDED.ai_category_reason
WHERE kbd_entry.status = 'draft'
  AND kbd_entry.metadata ->> 'sample_suite' = 'diagnosis-signal-matrix-v1'
  AND COALESCE((kbd_entry.metadata ->> 'seed_version')::integer, 0) < 4;
