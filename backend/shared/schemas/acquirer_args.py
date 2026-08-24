"""统一的 `acquire.args` 契约注册表（producer/consumer 同构）。

来源：RFC《关键信号数据模型分层重构》§4.4。
对应 `kb-service` 的 `ACQUIRER_CATALOG`（11 个 acquirer）。本注册表是该 catalog 的
"参数契约"补全——catalog 只描述"能采什么"，本模块描述"args 里能写什么"。

核心结构
--------
- COMMON_ARGS：跨工具公共参数，全局定义一次，被各工具 schema 引用（禁止另造同名）。
- ACQUIRER_ARGS_SCHEMA：以 `acquire.tool` 为键的注册表；每个值是一个紧凑的 schema
  dict（properties / required / additionalProperties:false）。
- validate_acquire_args(tool, args)：纯 Python 校验（类型、required、禁幽灵字段），
  无需 jsonschema 依赖即可运行；语义与 §6.1 的 JSON Schema 文件对齐。

字段命名约定（§4.4.4 语义消歧）
------------------------------
- `acquire.args.keyword`       —— 仅 QKV 采集关键词（acli -k），唯一权威。
- `acquire.args.resource_keyword` —— QFK 的"资源/主题"选择器，
  显式改名消歧，**不是**匹配关键词。
- `acquire.args.host`         —— 采集目标主机/作用域（如 {{HOST}}），特殊值 cluster 表示
  遍历集群；由 `orchestrate.requires` 中的 HOST 变量经变量池解析。
- `acquire.args.command`      —— QFK 子命令。
- `acquire.args.instruction`  —— 信号语义说明。
- `match.pattern`             —— QFK 匹配关键词唯一权威源。
"""

from __future__ import annotations

import copy
import re
import shlex
from typing import Any

from shared.schemas.log_source_catalog import (
    ABSOLUTE_LOG_TIME_PATTERN,
    ALLOWED_LOG_ROOTS,
    LOG_PARSERS,
    LOG_SOURCE_FAMILIES,
    REQUEST_ARTIFACT_ROOT,
    normalize_log_path,
    resolve_log_source,
    validate_absolute_log_time,
)

# ``acli log get -f`` 的安全边界是 basename 字符集，而不是扩展名。真实 HCI 日志
# 包含 messages、无扩展名文件及带 {{VAR}} 的动态 basename；配置文件和 BMC SEL 即使
# 字符形状合法，也会由日志源 Catalog 的 acquisition/runtime_supported 语义门拒绝。
SAFE_LOG_FILE_PATTERN = (
    r"^(?:[A-Za-z0-9_.-]|\{\{[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\}\})+$"
)
# 向后兼容旧调用方导入名；值已按实机 aCLI 契约收紧为根目录，而非宽泛前缀。
ALLOWED_LOG_PATH_PREFIXES = ALLOWED_LOG_ROOTS
ILLEGAL_COMMAND_CHARS = frozenset("|#;&`$<>{}\n\r")
# HCI 领域服务组是稳定知识；runtime_exposed 表示 2026-07-30 当前实机版本的探测结果，
# 不能把“领域存在”误写成“当前 aCLI 一定可执行”。
SERVICE_DOMAIN_CATALOG: dict[str, dict[str, Any]] = {
    "asv": {"plane": "vt", "name": "虚拟平台", "runtime_exposed": True},
    "anet": {"plane": "vn", "name": "虚拟网络", "runtime_exposed": True},
    "asan": {"plane": "vs", "name": "虚拟存储", "runtime_exposed": False},
    "host": {"plane": "host", "name": "宿主机/容器管理", "runtime_exposed": True},
}
# 当前版本 ``acli service --help`` 实际可执行的服务组。
VALID_SERVICE_CONTAINERS = frozenset({"asv", "anet", "host"})
# ``qfk_system.container`` 是 aCLI 的 ``--container`` 全局参数，而不是
# Terminal Bridge 的 container_exec 参数。未填写表示由 aCLI 在 HOST-OS 默认执行；
# ``host`` 因而不是一个合法的 --container 枚举值。
VALID_SYSTEM_CONTAINERS = frozenset({"asv-con", "vn-con", "vn-agent", "vs-cp-manager"})
_PLACEHOLDER_RE = re.compile(r"\{\{[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\}\}")


def _contains_illegal_command_chars(value: str) -> bool:
    """Reject shell control syntax while allowing canonical ``{{VAR}}`` placeholders."""

    without_placeholders = _PLACEHOLDER_RE.sub("VALUE", value)
    return any(char in ILLEGAL_COMMAND_CHARS for char in without_placeholders)

# ─── 公共参数：全局只定义一次 ───────────────────────────────────────────────────
# 60 秒是新建信号和未声明 timeout 的统一运行时默认值；显式配置的历史信号
# 保持原值，不在读取时被迁移或覆盖。单一常量同时供 Schema、Agent 和页面预览使用。
DEFAULT_SIGNAL_TIMEOUT_SECONDS = 60

COMMON_ARGS: dict[str, dict[str, Any]] = {
    "timeout": {
        "type": "integer",
        "minimum": 1,
        "maximum": 300,
        "default": DEFAULT_SIGNAL_TIMEOUT_SECONDS,
        "description": "采集/执行超时（秒，1-300）；QKV/QFK 通用",
    },
    "nonzero_exit_as_negative": {
        "type": "boolean",
        "default": False,
        "description": (
            "只读探针容错模式：当命令以非零退出码结束时，将其视为否定证据（matched=False）"
            "而非系统执行故障（QFK_COMMAND_FAILED）。"
        ),
    },
}

# qkv_vm_console 的 timeout 是对 COMMON_ARGS（1-300 秒）的**有意偏离**：控制台截图是
# 快速失败型采集，截图、上传、视觉提取各阶段另有独立内部超时，不允许长阻塞。
# 该字段独立声明，不复用 common_args（gen-schemas 据此保持内联而非 $ref）。
VM_CONSOLE_TIMEOUT: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 60,
    "default": DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    "description": "控制台截图超时（秒，1-60）；有意偏离公共 timeout（1-300），不复用公共定义",
}

# qkv_effect 的 timeout 同样是对 COMMON_ARGS（1-300 秒）的**有意偏离**：timeout 仅
# 约束单次观测（快速失败型查询），整体复核预算由期望锚点的 settle/window/max_recheck
# 显式承载，不允许一次调用长阻塞。该字段独立声明，不复用 common_args。
EFFECT_TIMEOUT: dict[str, Any] = {
    "type": "integer",
    "minimum": 1,
    "maximum": 60,
    "default": DEFAULT_SIGNAL_TIMEOUT_SECONDS,
    "description": "单次观测超时（秒，1-60）；有意偏离公共 timeout（1-300）。整体复核预算由期望窗口参数约束",
}

# qkv_vm_console 目标参数的安全形态：仅允许完整占位符或受控字面量。
# host：{{HOST}} 或 Inventory 规范化后的节点标识（字母数字开头，不含 shell 控制字符）。
VM_CONSOLE_HOST_PLACEHOLDER = "{{HOST}}"
VM_CONSOLE_HOST_LITERAL_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")
# vm_id：{{VM_ID}} 或精确数值型 VMID（不接受模糊 VM 名称）。
VM_CONSOLE_VM_ID_PLACEHOLDER = "{{VM_ID}}"
VM_CONSOLE_VM_ID_LITERAL_PATTERN = re.compile(r"^[0-9]{1,20}$")
# 条件型生产者的先决变量：二者来源齐备才允许参与发布门禁（见 signal_schema.py）。
VM_CONSOLE_REQUIRED_TARGET_VARS = frozenset({"HOST", "VM_ID"})

# ─── 条件型效果验证生产者（qkv_effect）契约常量 ─────────────────────────────────
# 设计来源：docs/solution/agent/效果验证生产者信号设计与需求.md
# 封闭观测通道集合：效果观测一律委派已批准的只读采集原语，不新开命令面。
# metric_query 等通道待设计文档第十二章平台确认项闭环后按提案加入。
EFFECT_OBSERVATION_CHANNELS = frozenset({"qkv_alert", "qkv_task", "qkv_dialog", "qkv_vm_console"})
# 使用模式：修复后复核（默认）/ S1 症状确认。
EFFECT_USAGES = frozenset({"remediation_verify", "symptom_confirm"})
# 判定规则封闭集合：与 shared/signals/matcher.py 的 7 类 matcher 严格一致，
# 不新增自由文本判定；确需扩展走提案 + 测试。
EFFECT_MATCHER_TYPES = frozenset({"keyword", "regex", "state", "threshold", "delta", "trend", "exists"})
# 各 matcher 类型的必填字段（与 signal_schema._MATCHER_REQUIRED_FIELDS 同源口径）。
_EFFECT_MATCHER_REQUIRED_FIELDS: dict[str, frozenset[str]] = {
    "keyword": frozenset({"pattern"}),
    "regex": frozenset({"pattern"}),
    "state": frozenset({"pattern"}),
    "threshold": frozenset({"value", "operator"}),
    "delta": frozenset({"value", "operator"}),
    "trend": frozenset({"direction"}),
    "exists": frozenset(),
}
# 时序受限范围：稳定窗口 / 复核窗口 / 复核次数（防止“永远复核”或“零等待立即判定”）。
EFFECT_SETTLE_RANGE = (0, 3600)
EFFECT_WINDOW_RANGE = (60, 86400)
EFFECT_MAX_RECHECK_RANGE = (0, 5)
# 判定词表（v1）：三态输出，禁止向两侧坍缩；词表变更须提升修订号。
EFFECT_VERDICT_VOCABULARY = frozenset({"achieved", "not_achieved", "inconclusive"})
EFFECT_VERDICT_VOCABULARY_REVISION = "effect-verdict-v1"
EFFECT_STATUS_VARIABLE = "EFFECT_STATUS"

# ─── 采集目标定位的扁平维度（v2 拍平的 target 字段）────────────────────────────
# 跨 QFK 工具复用的"目标"维度，各自按需声明子集，避免幽灵字段。
_TARGET_DIMENSIONS: dict[str, dict[str, Any]] = {
    "host": {
        "type": "string",
        "description": "采集目标主机/作用域（如 {{HOST}}），特殊值 cluster 表示遍历集群",
    },
    "path": {
        "type": "string",
        "description": (
            "aCLI 搜索路径；常规日志仅限 /sf/log。/sf/data/local 不是日志目录，"
            "仅允许与 request_id 同时使用以关联诊断产物"
        ),
    },
    "time_window": {
        "type": "string",
        "pattern": ABSOLUTE_LOG_TIME_PATTERN,
        "description": (
            "绝对日志时间：YYYY-MM-DD、YYYY-MM-DD HH、YYYY-MM-DD HH:MM:SS 或 {{ABSOLUTE_TIME}}；"
            "now/-1h 等相对表达式须由 Agent 先解析"
        ),
    },
}

# ─── 各工具 args 注册表（与 ACQUIRER_CATALOG 一一对应）──────────────────────────
# additionalProperties:false 防幽灵字段；required 列出该工具必填项。
# 注：具体字段以各工具真实 acli 契约为准；下方为基于当前代码的最佳近似骨架，
#     待 Phase 2 逐工具核对 acli 参数后可在此精确化（结构已稳定）。
ACQUIRER_ARGS_SCHEMA: dict[str, dict[str, Any]] = {
    # ── 前端信号（QKV）：采集关键词即查询过滤，无独立 match 段 ──
    "qkv_alert": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "keyword": {"type": "string", "description": "采集关键词（acli alert get -k）"},
            "limit": {"type": "integer", "default": 100, "description": "翻页数上限"},
            "alert_type": {"type": "string", "description": "告警类型过滤（可选）"},
        },
        "required": ["keyword"],
    },
    "qkv_task": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "keyword": {"type": "string", "description": "采集关键词（acli task get -k）"},
            "limit": {"type": "integer", "default": 100, "description": "翻页数上限"},
            # 失败标志由 status 派生；控制 acli 的 -s failed（§4.2 删 status）
            "is_failed": {"type": "boolean", "default": False, "description": "是否仅取失败任务"},
        },
        "required": ["keyword"],
    },
    "qkv_dialog": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "keyword": {"type": "string", "description": "页面弹框原文/稳定关键片段"},
            "limit": {"type": "integer", "default": 100, "description": "结构化候选结果上限"},
            "paths": {
                "type": "array",
                "items": {"type": "string", "enum": ["/sf/log/today", "/sf/log/today/vt"]},
                "minItems": 1,
                "maxItems": 2,
                "uniqueItems": True,
                "default": ["/sf/log/today", "/sf/log/today/vt"],
                "description": "在当前主控搜索的固定弹框日志域；不是自由路径",
            },
            "context_lines": {
                "type": "integer", "minimum": 0, "maximum": 10, "default": 2,
                "description": "命中行上下文，用于提取 END/REQUEST_ID",
            },
        },
        "required": ["keyword"],
    },
    # ── 条件型实时视觉生产者（QKV 扩展）：虚拟机控制台截图 ──
    # 只产出变量（produces），match 必须为 null。不提供任意 Monitor 指令、路径、
    # 按键或文件名字段——additionalProperties:false 使 command/monitor_command/path/
    # key/sleep/shell/url 等一律 422。
    "qkv_vm_console": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": VM_CONSOLE_TIMEOUT,
            "host": {
                "type": "string",
                "description": "仅允许 {{HOST}} 或由系统规范化后的节点变量；执行时必须 Inventory 校验",
            },
            "vm_id": {
                "type": "string",
                "description": "仅允许 {{VM_ID}} 或受控 VMID 变量；执行时必须精确匹配、不可含 Shell 控制字符",
            },
            "capture_mode": {
                "type": "string",
                "enum": ["baseline_then_optional_wake"],
                "default": "baseline_then_optional_wake",
                "description": "固定采集模式：基线截图，近黑时经人工确认后可唤醒重截；不提供任意 Monitor 指令",
            },
        },
        "required": ["host", "vm_id"],
    },
    # ── 条件型效果验证生产者（QKV 扩展）：期望 × 观测的三态判定 ──
    # 只产出变量（produces），match 必须为 null。期望锚点必须是结构化契约数据：
    # 观测通道封闭集合 + 7 类封闭 matcher + 受限窗口；不提供自由文本判定、命令、
    # 脚本字段——additionalProperties:false 使 command/shell/judge_text/prompt 等
    # 一律 422。深度语义校验见 validate_acquire_args 的 qkv_effect 分支。
    "qkv_effect": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": EFFECT_TIMEOUT,
            "usage": {
                "type": "string",
                "enum": ["remediation_verify", "symptom_confirm"],
                "default": "remediation_verify",
                "description": "修复后复核（默认）/ S1 症状确认",
            },
            "expectation": {
                "type": "object",
                "description": "结构化期望锚点：观测通道 + 封闭判定规则 + 时序窗口",
                "additionalProperties": False,
                "required": ["observation", "matcher"],
                "properties": {
                    "observation": {
                        "type": "object",
                        "description": "观测原语引用；args 原样通过对应原语的 acquirer_args 校验",
                        "additionalProperties": False,
                        "required": ["tool"],
                        "properties": {
                            "tool": {
                                "type": "string",
                                "enum": ["qkv_alert", "qkv_dialog", "qkv_task", "qkv_vm_console"],
                                "description": "封闭观测通道集合；metric_query 等待平台确认项闭环后按提案加入",
                            },
                            "args": {"type": "object"},
                        },
                    },
                    "matcher": {
                        "type": "object",
                        "description": "封闭 matcher 判定规则（与信号 match 段同构）；必须配置新版 extract",
                        "additionalProperties": False,
                        "required": ["type", "expected", "extract"],
                        "properties": {
                            "type": {
                                "type": "string",
                                "enum": [
                                    "keyword", "regex", "state", "threshold", "delta", "trend", "exists"
                                ],
                            },
                            "pattern": {
                                "anyOf": [
                                    {"type": "string"},
                                    {"type": "array", "items": {"type": "string", "minLength": 1}, "minItems": 1},
                                ]
                            },
                            "mode": {"type": "string", "enum": ["or", "and", "not"]},
                            "expected": {"type": "boolean"},
                            "value": {
                                "anyOf": [
                                    {"type": ["number", "integer"]},
                                    {
                                        "type": "string",
                                        "pattern": r"^\{\{[A-Z][A-Z0-9_]*(?:\.[A-Z0-9_]+)*\}\}$",
                                    },
                                ]
                            },
                            "operator": {"type": "string", "enum": [">", ">=", "<", "<=", "==", "=", "!="]},
                            "aggregation": {
                                "type": "string",
                                "enum": [
                                    "first_number", "last_number", "line_count", "duration_seconds", "max", "min", "sum"
                                ],
                                "default": "first_number",
                            },
                            "metric": {"type": "string"},
                            "minimum_samples": {"type": "integer", "minimum": 2, "maximum": 10000},
                            "direction": {"type": "string", "enum": ["increasing", "decreasing", "stable"]},
                            # 深度形态（text/json、rows 等）由 validate_acquire_args 校验。
                            "extract": {"type": "object"},
                        },
                    },
                    "settle_seconds": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 3600,
                        "default": 120,
                        "description": "动作完成后、首次判定前的稳定窗口（秒）",
                    },
                    "window_seconds": {
                        "type": "integer",
                        "minimum": 60,
                        "maximum": 86400,
                        "default": 900,
                        "description": "复核窗口总预算（秒）",
                    },
                    "max_recheck": {
                        "type": "integer",
                        "minimum": 0,
                        "maximum": 5,
                        "default": 2,
                        "description": "窗口内最大复核次数；配额耗尽即 inconclusive",
                    },
                },
            },
            "host": {
                "type": "string",
                "description": "目标绑定（可选）：仅允许 {{HOST}} 或规范化节点变量；执行时 Inventory 校验",
            },
        },
        "required": ["expectation"],
    },
    # ── 后端信号（QFK）：resource_keyword=资源/主题选择器，非匹配关键词 ──
    "qfk_log": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            # QFK 资源/主题选择器（改名消歧，非匹配关键词）
            "resource_keyword": {
                "type": "string",
                "description": "资源/主题选择器（acli log get <topic>）；改名消歧，非匹配关键词",
            },
            "host": _TARGET_DIMENSIONS["host"],
            "file": {
                "type": "string",
                "pattern": SAFE_LOG_FILE_PATTERN,
                "description": "安全日志文件 basename（acli -f；禁止目录与控制字符，扩展名不限）",
            },
            "path": _TARGET_DIMENSIONS["path"],
            "time_window": _TARGET_DIMENSIONS["time_window"],
            "source_family": {
                "type": "string",
                "enum": list(LOG_SOURCE_FAMILIES),
                "default": "auto",
                "description": "日志族；auto 时根据显式 path 和日志源 Catalog 推断",
            },
            "parser": {
                "type": "string",
                "enum": list(LOG_PARSERS),
                "description": "结构解析器；省略时由日志源 Catalog 按文件类型选择",
            },
            "request_id": {
                "type": "string",
                "description": "调用链 request_id（acli log get -i），可使用 {{REQUEST_ID}}",
            },
            "context_lines": {
                "type": "integer",
                "minimum": 0,
                "maximum": 50,
                "default": 0,
                "description": "命中行上下文行数（acli -c，0-50）",
            },
            "include_archives": {
                "type": "boolean",
                "default": False,
                "description": "是否以 -g 搜索 .gz 历史归档；必须同时声明 archive_precheck=verified",
            },
            "archive_precheck": {
                "type": "string",
                "enum": ["verified"],
                "description": "归档搜索前置检查已确认磁盘空间、目标日期和路径范围",
            },
        },
        # 常规日志必须有 file；/sf/data/local request_id 辅助域可不指定 file，
        # 两者互斥语义由 validate_acquire_args/运行时 Handler 做确定性校验。
        "required": [],
    },
    "qfk_service": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            "host": _TARGET_DIMENSIONS["host"],
            "resource_keyword": {
                "type": "string",
                "description": "历史兼容字段；读取时归一为 service，新配置不再写入",
            },
            "service": {
                "type": "string",
                "description": "服务名（acli service <container> <service> status）",
            },
            "container": {
                "type": "string",
                "enum": sorted(VALID_SERVICE_CONTAINERS),
                "default": "asv",
                "description": (
                    "当前版本 aCLI 已探测服务组：asv(vt)/anet(vn)/host；"
                    "领域 Catalog 另含 asan(vs)，当前节点未暴露，禁止假定可执行"
                ),
            },
            "command": {
                "type": "string",
                "description": "历史兼容字段；读取时归一为 action",
            },
            "action": {
                "type": "string",
                "enum": ["status", "start", "stop", "restart"],
                "default": "status",
                "description": "服务动作；缺省 status。KBD 只读领域门禁另行禁止 start/stop/restart",
            },
        },
        # service/resource_keyword 的兼容二选一由 validate_acquire_args 与 Signal
        # 语义校验完成，避免 required 把历史不可变 Revision 一次性打断。
        "required": [],
    },
    "qfk_system": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            "command": {
                "type": "string",
                "description": "acli system 的基础子命令（如 lsof/ps/df/lsblk/iostat/smartctl）；不得含参数、acli 前缀或 shell 管道",
            },
            "command_args": {
                "type": "array",
                "items": {"type": "string"},
                "description": "可选的结构化命令参数；逐项安全转义后追加到 acli system <command> 之后",
            },
            "host": _TARGET_DIMENSIONS["host"],
            "cluster": {
                "type": "boolean",
                "default": False,
                "description": "是否添加 acli --cluster，在集群所有节点执行；不再使用 host=cluster 表达此语义",
            },
            "formatter": {
                "type": "string",
                "enum": ["xml", "csv", "keyvalue", "json"],
                "description": "可选的 acli --formatter 输出格式，位于 system namespace 之前",
            },
            "container": {
                "type": "string",
                "enum": sorted(VALID_SYSTEM_CONTAINERS),
                "description": "可选的 acli --container 执行域；不填写时由 aCLI 在 HOST-OS 默认执行，不会由 Terminal Bridge 进入容器",
            },
        },
        "required": ["command"],
    },
    "qfk_vm": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            "command": {"type": "string", "description": "acli vm <command>（如 list/status/console）"},
            "command_args": {"type": "array", "items": {"type": "string"}, "description": "结构化命令参数，例如 --vm-id {{VM}}"},
            "host": _TARGET_DIMENSIONS["host"],
            "resource_keyword": {"type": "string", "description": "虚拟机名选择器（可选）"},
        },
        "required": ["command"],
    },
    "qfk_network": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            "command": {"type": "string", "description": "acli network <command>"},
            "command_args": {"type": "array", "items": {"type": "string"}, "description": "结构化 aCLI 参数"},
            "host": _TARGET_DIMENSIONS["host"],
            "resource_keyword": {"type": "string", "description": "网络资源名选择器（可选）"},
        },
        "required": ["command"],
    },
    "qfk_storage": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            "command": {"type": "string", "description": "acli storage <command>（如 asan disk list）"},
            "command_args": {"type": "array", "items": {"type": "string"}, "description": "结构化 aCLI 参数"},
            "host": _TARGET_DIMENSIONS["host"],
            "resource_keyword": {"type": "string", "description": "存储资源名选择器（可选）"},
        },
        "required": ["command"],
    },
    "qfk_hardware": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            "command": {"type": "string", "description": "acli hardware <command>"},
            "command_args": {"type": "array", "items": {"type": "string"}, "description": "结构化 aCLI 参数"},
            "host": _TARGET_DIMENSIONS["host"],
            "resource_keyword": {"type": "string", "description": "硬件资源名选择器（可选）"},
        },
        "required": ["command"],
    },
    "qfk_platform": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "nonzero_exit_as_negative": COMMON_ARGS["nonzero_exit_as_negative"],
            "command": {"type": "string", "description": "acli platform <command>"},
            "command_args": {"type": "array", "items": {"type": "string"}, "description": "结构化 aCLI 参数"},
            "host": _TARGET_DIMENSIONS["host"],
            "resource_keyword": {"type": "string", "description": "平台资源名选择器（可选）"},
        },
        "required": ["command"],
    },
}

# aCLI 的 formatter 是全局参数，适用于所有领域命令，并且必须位于 namespace 前。
for _domain_tool in ("qfk_vm", "qfk_network", "qfk_storage", "qfk_hardware", "qfk_platform"):
    ACQUIRER_ARGS_SCHEMA[_domain_tool]["properties"]["formatter"] = {
        "type": "string",
        "enum": ["xml", "csv", "keyvalue", "json"],
        "description": "可选的 acli --formatter 输出格式；Resolver 会把它放在领域 namespace 之前",
    }

# ─── 注入公共可选字段：instruction（信号语义说明）──────────────────────────────
# 真实数据里 instruction 常置于 acquire.args，为使 v2 的 acquire.args 能容纳真实数据
# （validate_acquire_args 与 JSON Schema 均 additionalProperties:false），在此单一来源处
# 为所有 tool 注入该可选字段，杜绝幽灵字段。
for _name, _tool_schema in ACQUIRER_ARGS_SCHEMA.items():
    _props = _tool_schema.setdefault("properties", {})
    _props.setdefault(
        "instruction",
        {"type": "string", "description": "信号语义说明（acli 调用的人类可读解释）"},
    )

# 工具词表（与 ACQUIRER_CATALOG 同源；供校验/前端下拉复用）
SUPPORTED_TOOLS: list[str] = list(ACQUIRER_ARGS_SCHEMA.keys())

# 取数类（QKV）vs 判定类（QFK）分组：consumer 据此决定是否需 match 段
FRONTEND_TOOLS: set[str] = {"qkv_alert", "qkv_task", "qkv_dialog"}
# 条件型生产者：自身不能从全局事件发现目标，必须先具备可信先决条件才可执行。
# qkv_vm_console 要求 HOST 与 VM_ID；qkv_effect 要求期望锚点变量来源可达，且
# 不得作为 KBD 唯一生产者（见 signal_schema.validate_kbd_publishable_signals_json）。
# 注意：**不得**并入 FRONTEND_TOOLS——该集合同时被"至少一个生产者"发布门禁、
# qkv 校验分支与解析器分发引用，并入会让条件生产者获得直接生产者语义。
# 发布门禁见 signal_schema.validate_kbd_publishable_signals_json。
CONDITIONAL_PRODUCERS: set[str] = {"qkv_vm_console", "qkv_effect"}
BACKEND_TOOLS: set[str] = set(ACQUIRER_ARGS_SCHEMA) - FRONTEND_TOOLS - CONDITIONAL_PRODUCERS

# Stable error code for the most common LLM contract drift.  Keep QKV keyword
# strict: the runtime builds one ``acli -k`` argument, while arrays belong to
# QFK matcher/extract fields and have different AND/OR semantics.
QKV_KEYWORD_TYPE_ERROR_CODE = "QKV_KEYWORD_MUST_BE_STRING"


def get_args_schema(tool: str) -> dict[str, Any] | None:
    """返回某 acquirer 的 args schema（深拷贝，防止调用方篡改注册表）。"""

    schema = ACQUIRER_ARGS_SCHEMA.get(tool)
    return copy.deepcopy(schema) if schema else None


def _check_type(value: Any, expected: str) -> bool:
    """对齐 JSON Schema 基础类型。"""
    if expected == "integer":
        return isinstance(value, int) and not isinstance(value, bool)
    if expected == "number":
        return isinstance(value, (int, float)) and not isinstance(value, bool)
    if expected == "boolean":
        return isinstance(value, bool)
    if expected == "string":
        return isinstance(value, str)
    if expected == "object":
        return isinstance(value, dict)
    if expected == "array":
        return isinstance(value, list)
    return True  # 未声明类型的字段放行


def normalize_qfk_system_args(args: Any) -> dict[str, Any]:
    """把 qfk_system 的单一命令输入规范为 ``command + command_args``。

    旧 ``resource_keyword`` 不具备可推导的 argv 语义，特别是 VM ID 不能直接追加给
    ``lsof``，因此一律要求人工复核，而不是静默改变命令。
    """

    if not isinstance(args, dict):
        raise ValueError("acquire.args 必须是对象")
    normalized = copy.deepcopy(args)
    legacy_resource = normalized.get("resource_keyword")
    if isinstance(legacy_resource, str) and legacy_resource.strip():
        raise ValueError(
            "qfk_system 已不支持 resource_keyword 命令参数；请将确有语义的参数写入命令，并人工复核后删除旧字段"
        )
    normalized.pop("resource_keyword", None)

    command = normalized.get("command")
    if not isinstance(command, str) or not command.strip():
        raise ValueError("qfk_system 必须提供非空 command")
    try:
        command_tokens = shlex.split(command.strip())
    except ValueError as exc:
        raise ValueError(f"qfk_system.command 无法安全分词: {exc}") from exc
    if not command_tokens or any(_contains_illegal_command_chars(token) for token in command_tokens):
        raise ValueError("qfk_system.command 包含命令注入类非法字符")

    configured_args = normalized.get("command_args") or []
    if not isinstance(configured_args, list) or not all(isinstance(item, str) for item in configured_args):
        raise ValueError("qfk_system.command_args 必须是字符串数组")
    if len(command_tokens) > 1 and configured_args:
        raise ValueError("qfk_system.command 已含参数时不得同时填写 command_args")
    command_args = command_tokens[1:] if len(command_tokens) > 1 else configured_args
    if any(not item or _contains_illegal_command_chars(item) for item in command_args):
        raise ValueError("qfk_system.command_args 包含空值或命令注入类非法字符")
    normalized["command"] = command_tokens[0]
    normalized["command_args"] = command_args
    return normalized


def _validate_effect_expectation(expectation: Any) -> tuple[bool, str | None]:
    """深度校验 qkv_effect 期望锚点：封闭通道 + 封闭 matcher + 受限窗口。

    与 JSON Schema（结构层）互补：这里做跨字段语义门禁，尤其是观测原语 args
    必须原样通过其自身 ``acquirer_args`` 校验（递归调用 validate_acquire_args，
    观测通道均为 qkv_* 直接生产者，不会回到 qkv_effect 分支，无递归风险）。
    """

    if not isinstance(expectation, dict):
        return False, "qkv_effect.expectation 必须是对象（结构化期望锚点）"
    extra = set(expectation) - {"observation", "matcher", "settle_seconds", "window_seconds", "max_recheck"}
    if extra:
        return False, f"qkv_effect.expectation 含未注册字段: {', '.join(sorted(extra))}"

    observation = expectation.get("observation")
    if not isinstance(observation, dict):
        return False, "qkv_effect.expectation.observation 必填且必须是对象"
    obs_extra = set(observation) - {"tool", "args"}
    if obs_extra:
        return False, f"qkv_effect.expectation.observation 含未注册字段: {', '.join(sorted(obs_extra))}"
    obs_tool = observation.get("tool")
    if obs_tool not in EFFECT_OBSERVATION_CHANNELS:
        return False, (
            f"qkv_effect 观测通道不在封闭集合: {obs_tool}；允许 {sorted(EFFECT_OBSERVATION_CHANNELS)}"
        )
    obs_args = observation.get("args") or {}
    obs_ok, obs_error = validate_acquire_args(str(obs_tool), obs_args)
    if not obs_ok:
        return False, f"qkv_effect 观测原语（{obs_tool}）参数不合法: {obs_error}"

    matcher = expectation.get("matcher")
    if not isinstance(matcher, dict):
        return False, "qkv_effect.expectation.matcher 必填且必须是对象（封闭判定规则）"
    matcher_extra = set(matcher) - {
        "type", "pattern", "mode", "expected", "value", "operator",
        "aggregation", "metric", "minimum_samples", "direction", "extract",
    }
    if matcher_extra:
        return False, f"qkv_effect.expectation.matcher 含未注册字段: {', '.join(sorted(matcher_extra))}"
    mtype = matcher.get("type")
    if mtype not in EFFECT_MATCHER_TYPES:
        return False, (
            f"qkv_effect 判定规则不在封闭 matcher 集合: {mtype}；允许 {sorted(EFFECT_MATCHER_TYPES)}"
        )
    if not isinstance(matcher.get("expected"), bool):
        return False, "qkv_effect.matcher.expected 必须是布尔值（三态判定由平台合成，不由 matcher 表达）"
    for field_name in _EFFECT_MATCHER_REQUIRED_FIELDS.get(str(mtype), frozenset()):
        if matcher.get(field_name) in (None, "", []):
            return False, f"qkv_effect.matcher.{field_name} 对 {mtype} 判定必填"
    extract = matcher.get("extract")
    if not isinstance(extract, dict):
        return False, "qkv_effect.matcher 必须配置新版 extract（与 evaluate_matcher 求值契约对齐）"
    extract_type = str(extract.get("type") or "")
    if extract_type not in {"text", "json"}:
        return False, "qkv_effect.matcher.extract.type 仅支持 text/json"
    if extract_type == "text" and not isinstance(extract.get("rows"), dict):
        return False, "qkv_effect.matcher.extract 为 text 时必须配置 rows"

    settle = expectation.get("settle_seconds", 120)
    if isinstance(settle, bool) or not isinstance(settle, int) or not (
        EFFECT_SETTLE_RANGE[0] <= settle <= EFFECT_SETTLE_RANGE[1]
    ):
        return False, f"qkv_effect.expectation.settle_seconds 必须在 {EFFECT_SETTLE_RANGE[0]}-{EFFECT_SETTLE_RANGE[1]}"
    window = expectation.get("window_seconds", 900)
    if isinstance(window, bool) or not isinstance(window, int) or not (
        EFFECT_WINDOW_RANGE[0] <= window <= EFFECT_WINDOW_RANGE[1]
    ):
        return False, f"qkv_effect.expectation.window_seconds 必须在 {EFFECT_WINDOW_RANGE[0]}-{EFFECT_WINDOW_RANGE[1]}"
    max_recheck = expectation.get("max_recheck", 2)
    if isinstance(max_recheck, bool) or not isinstance(max_recheck, int) or not (
        EFFECT_MAX_RECHECK_RANGE[0] <= max_recheck <= EFFECT_MAX_RECHECK_RANGE[1]
    ):
        return False, f"qkv_effect.expectation.max_recheck 必须在 {EFFECT_MAX_RECHECK_RANGE[0]}-{EFFECT_MAX_RECHECK_RANGE[1]}"
    return True, None


def validate_acquire_args(tool: str, args: Any) -> tuple[bool, str | None]:
    """纯 Python 校验 `acquire.args`，与 §6.1 JSON Schema 语义对齐。

    Args:
        tool: acquire.tool（须在 ACQUIRER_ARGS_SCHEMA 内）。
        args: 待校验的参数 dict。

    Returns:
        (ok, error) —— ok=False 时 error 为可读错误信息。
    """
    schema = ACQUIRER_ARGS_SCHEMA.get(tool)
    if schema is None:
        return False, f"acquire.tool 不在契约注册表内: {tool}"

    if not isinstance(args, dict):
        return False, f"acquire.args 必须是对象，实际为 {type(args).__name__}"

    props = schema.get("properties", {})
    required = schema.get("required", [])

    # required 校验
    for r in required:
        if r not in args:
            return False, f"acquire.args 缺少必填字段: {r}（tool={tool}）"

    # additionalProperties:false —— 禁幽灵字段
    if schema.get("additionalProperties", True) is False:
        for k in args:
            if k not in props:
                return False, f"acquire.args 含未注册字段 '{k}'（tool={tool}，additionalProperties:false）"

    # 类型校验（仅对 schema 中声明的字段）
    for k, v in args.items():
        if k in props:
            expected = props[k].get("type")
            if expected and not _check_type(v, expected):
                if tool in FRONTEND_TOOLS and k == "keyword" and expected == "string":
                    return False, (
                        f"{QKV_KEYWORD_TYPE_ERROR_CODE}: acquire.args.keyword 类型错误："
                        "QKV 采集关键词必须是单个 string；多个关键词请拆成多条 qkv Candidate，"
                        "数组仅允许用于 match.pattern 或 extract.rows.include/exclude"
                    )
                return False, f"acquire.args.{k} 类型错误：期望 {expected}，实际 {type(v).__name__}"

    # 与 Agent QFK Handler 同源的运行时语义门禁。结构合法并不代表命令一定可构建；
    # 这些约束必须在保存 Proposal 前执行，不能等到现场调度才暴露。
    if tool == "qkv_dialog":
        paths = args.get("paths", ["/sf/log/today", "/sf/log/today/vt"])
        allowed_dialog_paths = {"/sf/log/today", "/sf/log/today/vt"}
        if not paths or len(paths) > 2 or any(path not in allowed_dialog_paths for path in paths):
            return False, "qkv_dialog.paths 只允许 /sf/log/today 与 /sf/log/today/vt，且至少选择一个"
        context_lines = args.get("context_lines", 2)
        if isinstance(context_lines, int) and not 0 <= context_lines <= 10:
            return False, "qkv_dialog.context_lines 必须在 0-10"

    if tool == "qkv_vm_console":
        host = args.get("host")
        vm_id = args.get("vm_id")
        if not isinstance(host, str) or not host.strip():
            return False, "qkv_vm_console.host 必须是非空字符串"
        if not isinstance(vm_id, str) or not vm_id.strip():
            return False, "qkv_vm_console.vm_id 必须是非空字符串"
        host_value = host.strip()
        vm_id_value = vm_id.strip()
        # host 仅允许完整 {{HOST}} 占位符或 Inventory 规范化后的安全节点标识；
        # 不接受未经验证的用户主机名自由文本。
        if host_value != VM_CONSOLE_HOST_PLACEHOLDER and not VM_CONSOLE_HOST_LITERAL_PATTERN.fullmatch(host_value):
            return False, (
                "qkv_vm_console.host 仅允许 {{HOST}} 占位符或系统规范化节点标识"
                "（字母数字开头，仅含字母数字/点/下划线/连字符，≤128 字符）"
            )
        # vm_id 仅允许完整 {{VM_ID}} 占位符或精确数值型 VMID；模糊 VM 名称不接受。
        if vm_id_value != VM_CONSOLE_VM_ID_PLACEHOLDER and not VM_CONSOLE_VM_ID_LITERAL_PATTERN.fullmatch(vm_id_value):
            return False, "qkv_vm_console.vm_id 仅允许 {{VM_ID}} 占位符或精确数值型 VMID，不接受模糊 VM 名称"
        capture_mode = args.get("capture_mode", "baseline_then_optional_wake")
        if capture_mode != "baseline_then_optional_wake":
            return False, "qkv_vm_console.capture_mode 仅支持 baseline_then_optional_wake"
        timeout = args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS)
        if isinstance(timeout, int) and not isinstance(timeout, bool) and not 1 <= timeout <= 60:
            return False, "qkv_vm_console.timeout 必须在 1-60（快速失败型采集，有意偏离公共 1-300）"

    if tool == "qkv_effect":
        usage = args.get("usage", "remediation_verify")
        if usage not in EFFECT_USAGES:
            return False, f"qkv_effect.usage 仅支持 {sorted(EFFECT_USAGES)}: {usage}"
        ok, error = _validate_effect_expectation(args.get("expectation"))
        if not ok:
            return False, str(error)
        host = args.get("host")
        if host is not None:
            if not isinstance(host, str) or not host.strip():
                return False, "qkv_effect.host 必须是非空字符串"
            host_value = host.strip()
            if host_value != VM_CONSOLE_HOST_PLACEHOLDER and not VM_CONSOLE_HOST_LITERAL_PATTERN.fullmatch(host_value):
                return False, (
                    "qkv_effect.host 仅允许 {{HOST}} 占位符或系统规范化节点标识"
                    "（字母数字开头，仅含字母数字/点/下划线/连字符，≤128 字符）"
                )
        timeout = args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS)
        if isinstance(timeout, int) and not isinstance(timeout, bool) and not 1 <= timeout <= 60:
            return False, "qkv_effect.timeout 必须在 1-60（单次观测快速失败，整体预算由期望窗口约束）"

    if tool == "qfk_log":
        file_name = str(args.get("file") or "")
        path = str(args.get("path") or "")
        try:
            normalized_path = normalize_log_path(path or None)
        except ValueError as exc:
            return False, f"acquire.args 日志路径不可解析: {exc}"
        is_request_artifact = bool(
            normalized_path
            and (
                normalized_path == REQUEST_ARTIFACT_ROOT
                or normalized_path.startswith(f"{REQUEST_ARTIFACT_ROOT}/")
            )
        )
        if is_request_artifact:
            if not str(args.get("request_id") or "").strip():
                return False, "/sf/data/local 不是日志目录；只有携带 request_id 时才能作为辅助关联搜索域"
            if str(args.get("source_family") or "auto") != "auto":
                return False, "/sf/data/local 辅助搜索不得声明日志 source_family"
            source = {"runtime_supported": True, "source_id": "request_artifact_scope"}
        else:
            if file_name in {"", ".", ".."} or not re.fullmatch(SAFE_LOG_FILE_PATTERN, file_name):
                return False, f"acquire.args.file 必须是无目录、无控制字符的安全 basename：{file_name}"
            try:
                source = resolve_log_source(
                    file_name,
                    source_family=str(args.get("source_family") or "auto"),
                    path=normalized_path,
                    parser=str(args.get("parser")) if args.get("parser") else None,
                )
            except ValueError as exc:
                return False, f"acquire.args 日志源不可解析: {exc}"
        ok, error = validate_absolute_log_time(str(args.get("time_window") or "") or None)
        if not ok:
            return False, f"acquire.args.time_window 非法: {error}"
        if args.get("include_archives") is True and args.get("archive_precheck") != "verified":
            return False, "include_archives=true 时必须设置 archive_precheck=verified"
        if args.get("archive_precheck") and args.get("include_archives") is not True:
            return False, "archive_precheck 只能与 include_archives=true 同时使用"
        context_lines = args.get("context_lines", 0)
        if isinstance(context_lines, int) and not 0 <= context_lines <= 50:
            return False, "acquire.args.context_lines 必须在 0-50"
        if not source.get("runtime_supported", True):
            return False, f"日志源 {source.get('source_id')} 不能由本机 qfk_log 获取"

    if tool == "qfk_service":
        container = str(args.get("container") or "asv")
        if container not in VALID_SERVICE_CONTAINERS:
            return False, f"acquire.args.container 非法: {container}"
        service = str(args.get("service") or args.get("resource_keyword") or "").strip()
        if not service:
            return False, "acquire.args 缺少必填字段: service（历史 resource_keyword 仍兼容）"
    if tool == "qfk_system":
        try:
            normalize_qfk_system_args(args)
        except ValueError as exc:
            return False, str(exc)

    if tool.startswith("qfk_"):
        for field in ("command", "resource_keyword", "service", "action"):
            value = args.get(field)
            if isinstance(value, str) and _contains_illegal_command_chars(value):
                return False, f"acquire.args.{field} 包含命令注入类非法字符"

    return True, None
