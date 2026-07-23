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
- `acquire.args.keyword`    —— 仅 QKV 采集关键词（acli -k），唯一权威。
- `acquire.args.resource_keyword` —— QFK 的"资源/主题"选择器（原 acquirer_args.keyword），
  显式改名消歧，**不是**匹配关键词。
- `match.pattern`           —— QFK 匹配关键词唯一权威源（原 matcher.pattern）。
"""

from __future__ import annotations

import copy
from typing import Any

# ─── 公共参数：全局只定义一次 ───────────────────────────────────────────────────
# 任何适用于 >1 个 acquirer 的字段必须进 common_args；各工具以引用方式复用，禁止另造同名。
COMMON_ARGS: dict[str, dict[str, Any]] = {
    "timeout": {
        "type": "integer",
        "minimum": 1,
        "default": 10,
        "description": "采集/执行超时（秒）；QKV/QFK 通用",
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
            "keyword": {"type": "string", "description": "采集关键词（acli dialog get -k）"},
            "limit": {"type": "integer", "default": 100, "description": "翻页数上限"},
        },
        "required": ["keyword"],
    },
    # ── 后端信号（QFK）：resource_keyword=资源/主题选择器，非匹配关键词 ──
    "qfk_log": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            # 原 acquirer_args.keyword（如 "vgpu"）—— 改名消歧，非匹配关键词
            "resource_keyword": {
                "type": "string",
                "description": "资源/主题选择器（acli log get <topic>）；原 acquirer_args.keyword，改名消歧，非匹配关键词",
            },
            "resource": {
                "type": "string",
                "description": "目标资源定位（日志/服务名），支持 {{VAR}} 变量池",
            },
            "file": {"type": "string", "description": "日志文件路径（qfk_log 专属）"},
            "end": {"type": "string", "description": "时间窗上界（qfk_log 专属，如 now/-1h）"},
        },
        "required": ["resource_keyword", "resource"],
    },
    "qfk_service": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "resource_keyword": {
                "type": "string",
                "description": "服务名选择器（acli service <group> <name>）；原 acquirer_args.keyword 改名消歧",
            },
            "resource": {
                "type": "string",
                "description": "服务所属组（asv/anet/host，qfk_service 专属）",
            },
            "sub_command": {"type": "string", "description": "操作子命令（如 status/restart）"},
        },
        "required": ["resource_keyword", "resource"],
    },
    "qfk_system": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "sub_command": {
                "type": "string",
                "description": "acli system <sub_command>（如 lsof/ps auxf/lsblk/iostat/smartctl）",
            },
            "resource_keyword": {
                "type": "string",
                "description": "系统检查资源/主题选择器（可选，如镜像层路径 overlay2/docker）；"
                "说明性文本会被兜底迁移至 description，故与兄弟 qfk_* 工具保持字段一致，避免被 strict schema 拒绝",
            },
            "container": {"type": "string", "default": "asv-con", "description": "执行容器（qfk_system 专属）"},
        },
        "required": ["sub_command"],
    },
    "qfk_vm": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "sub_command": {"type": "string", "description": "acli vm <sub_command>（如 list/status/console）"},
            "resource_keyword": {"type": "string", "description": "虚拟机名选择器（可选）"},
        },
        "required": ["sub_command"],
    },
    "qfk_network": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "sub_command": {"type": "string", "description": "acli network <sub_command>"},
            "resource_keyword": {"type": "string", "description": "网络资源名选择器（可选）"},
        },
        "required": ["sub_command"],
    },
    "qfk_storage": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "sub_command": {"type": "string", "description": "acli storage <sub_command>（如 asan disk list）"},
            "resource_keyword": {"type": "string", "description": "存储资源名选择器（可选）"},
        },
        "required": ["sub_command"],
    },
    "qfk_hardware": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "sub_command": {"type": "string", "description": "acli hardware <sub_command>"},
            "resource_keyword": {"type": "string", "description": "硬件资源名选择器（可选）"},
        },
        "required": ["sub_command"],
    },
    "qfk_platform": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "timeout": COMMON_ARGS["timeout"],
            "sub_command": {"type": "string", "description": "acli platform <sub_command>"},
            "resource_keyword": {"type": "string", "description": "平台资源名选择器（可选）"},
        },
        "required": ["sub_command"],
    },
}

# ─── 注入公共可选字段：target / description ───────────────────────────────────
# 真实 v1 数据里 target（主机/资源/路径/时间窗）常置于 acquirer_args，description 亦可能落入；
# 为使 v2 的 acquire.args 能容纳真实数据（validate_acquire_args 与 JSON Schema 均
# additionalProperties:false），在此单一来源处为所有 tool 注入这两个可选字段，杜绝幽灵字段。
_TARGET_ARGS_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "description": "采集目标定位（主机/资源/路径/时间窗），支持 {{VAR}} 变量池；对应 v1 acquirer_args.target",
    "properties": {
        "scope": {"type": "string", "description": "主机/作用域选择器（如 {{HOST}}）"},
        "resource": {"type": "string", "description": "资源名（服务名/日志名/磁盘等）"},
        "path": {"type": "string", "description": "路径（日志目录/文件等）"},
        "time_window": {"type": "string", "description": "时间窗（如 now/-1h）"},
    },
}
for _name, _tool_schema in ACQUIRER_ARGS_SCHEMA.items():
    _props = _tool_schema.setdefault("properties", {})
    _props.setdefault("target", copy.deepcopy(_TARGET_ARGS_SCHEMA))
    _props.setdefault(
        "description",
        {"type": "string", "description": "信号语义说明（对应 v1 顶层 description，迁移后落 _v1_legacy）"},
    )
    # QFK 真实数据以 target（主机/资源/路径/时间窗）定位，不强约束 resource_keyword/resource，
    # 以免合法历史信号在保存校验时被 422（RFC §7 完整方案：v2 契约容纳真实数据，
    # additionalProperties:false 仍拒绝幽灵字段）。QKV 保留 keyword 必填。
    if _name.startswith("qfk"):
        _tool_schema["required"] = []

# 工具词表（与 ACQUIRER_CATALOG 同源；供校验/前端下拉复用）
SUPPORTED_TOOLS: list[str] = list(ACQUIRER_ARGS_SCHEMA.keys())

# 取数类（QKV）vs 判定类（QFK）分组：consumer 据此决定是否需 match 段
FRONTEND_TOOLS: set[str] = {"qkv_alert", "qkv_task", "qkv_dialog"}
BACKEND_TOOLS: set[str] = set(ACQUIRER_ARGS_SCHEMA) - FRONTEND_TOOLS


def get_args_schema(tool: str) -> dict[str, Any] | None:
    """返回某 acquirer 的 args schema（深拷贝，防止调用方篡改注册表）。"""
    import copy

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
                return False, f"acquire.args.{k} 类型错误：期望 {expected}，实际 {type(v).__name__}"

    return True, None
