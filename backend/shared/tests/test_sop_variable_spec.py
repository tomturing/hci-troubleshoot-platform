"""SOP 变量声明契约单测。

守护目标（对应 Q2026102600003 复盘的四条根因）：
  1. 依赖列写成自然语言 -> 保存期即拦截
  2. tool_call 缺工具名     -> 保存期即拦截（运行时不再静默转人工）
  3. 未知策略被兜底成 user_input -> 保存期即拦截
  4. path_template 的模板引用与 depends_on 对齐
"""

from __future__ import annotations

import pytest
from shared.models.sop_variable import (
    SopVariableSchema,
    SopVariableSchemaError,
    SopVariableSpec,
    validate_variable_schema,
)

# ──────────────────────────────────────────────────────────────────────────────
# 合规样板：SOP#4「硬盘坏道」线上真实定义（已由 040 迁移整改，必须继续通过）
# ──────────────────────────────────────────────────────────────────────────────
SOP4_BASELINE = [
    {
        "name": "alert_logs",
        "type": "json",
        "acquisition_strategy": "tool_call",
        "acquisition_tool": "acli_exec",
        "output_path": "stdout",
        "depends_on": [],
        "fallback_strategy": "user_input",
        "description": "磁盘相关告警原始数据",
    },
    {
        "name": "alert_type",
        "type": "string",
        "acquisition_strategy": "env_injection",
        "acquisition_tool": "alert_type",
        "depends_on": [],
        "description": "告警类型",
    },
    {
        "name": "asan_disks",
        "type": "json",
        "acquisition_strategy": "tool_call",
        "acquisition_tool": "acli_exec",
        "output_path": "stdout",
        "depends_on": ["node_ip"],
        "fallback_strategy": "user_input",
        "description": "asan 磁盘列表",
    },
    {
        "name": "check_meth",
        "type": "string",
        "acquisition_strategy": "skill_call",
        "acquisition_tool": "hci-disk-health-checker",
        "output_path": "value",
        "depends_on": ["smart_info"],
        "description": "检查方式",
    },
    {
        "name": "date",
        "type": "date",
        "acquisition_strategy": "skill:hci-alert-parsing",
        "acquisition_tool": "hci-alert-parsing",
        "output_path": "values.end",
        "depends_on": ["alert_logs"],
        "description": "告警结束时间",
    },
    {
        "name": "disk_dev",
        "type": "string",
        "acquisition_strategy": "json_extract",
        "expression": "$.data.disks[?(@.host_name == '{node_hostname}' & @.disk_name == '{disk_name}')].dev",
        "depends_on": ["asan_disks", "disk_name", "node_hostname"],
        "fallback_strategy": "user_input",
        "description": "磁盘设备名",
    },
    {
        "name": "disk_name",
        "type": "string",
        "acquisition_strategy": "skill_call",
        "acquisition_tool": "hci-alert-parsing",
        "output_path": "values.disk_name",
        "depends_on": ["alert_logs"],
        "description": "磁盘槽位名",
    },
    {
        "name": "disk_sn",
        "type": "string",
        "acquisition_strategy": "json_extract",
        "expression": "$.data.disks[?(@.host_name == '{node_hostname}' & @.disk_name == '{disk_name}')].disk_sn",
        "depends_on": ["asan_disks", "disk_name", "node_hostname"],
        "description": "磁盘序列号",
    },
    {
        "name": "node_hostname",
        "type": "string",
        "acquisition_strategy": "skill_call",
        "acquisition_tool": "hci-alert-parsing",
        "output_path": "values.node_hostname",
        "depends_on": ["alert_logs"],
        "description": "节点主机名",
    },
    {
        "name": "node_ip",
        "type": "ip",
        "acquisition_strategy": "skill_call",
        "acquisition_tool": "hci-alert-parsing",
        "output_path": "values.node_ip",
        "depends_on": ["alert_logs"],
        "fallback_strategy": "user_input",
        "description": "节点 IP",
    },
    {
        "name": "smart_info",
        "type": "json",
        "acquisition_strategy": "tool_call",
        "acquisition_tool": "bash_exec",
        "output_path": "stdout",
        "depends_on": ["disk_dev", "node_ip"],
        "description": "SMART 原始输出",
    },
]


def test_sop4_baseline_passes_all_modes():
    """已整改的合规样板必须在 save / publish / runtime 三态下全部通过。"""
    for mode in ("save", "publish", "runtime"):
        report = validate_variable_schema(SOP4_BASELINE, mode=mode)
        assert not report.has_error, f"mode={mode} 误报: {[i.model_dump() for i in report.errors]}"


def test_parse_returns_typed_specs_and_preserves_extra():
    """解析结果应是值对象，且不丢失未显式建模的存量字段。"""
    raw = [dict(SOP4_BASELINE[0], auto_generated=False, display_name="alert_logs")]
    schema = SopVariableSchema.parse(raw)
    spec = schema.get("alert_logs")
    assert spec is not None
    assert spec.strategy == "tool_call"
    assert spec.effective_tool == "acli_exec"
    # extra="allow" 必须保留未建模字段
    dumped = schema.to_db()[0]
    assert dumped["auto_generated"] is False
    assert dumped["display_name"] == "alert_logs"


# ──────────────────────────────────────────────────────────────────────────────
# 根因 1：依赖列写自然语言
# ──────────────────────────────────────────────────────────────────────────────


def test_depends_on_natural_language_rejected():
    """线上真实坏数据：depends_on=["任务原始数据"]。"""
    raw = [
        {
            "name": "task_logs",
            "type": "json",
            "acquisition_strategy": "tool:acli_exec",
            "acquisition_tool": "acli_exec",
            "output_path": "stdout",
            "depends_on": ["任务原始数据"],
        }
    ]
    report = validate_variable_schema(raw)
    assert report.has_error
    codes = {i.code for i in report.errors}
    assert "SOP_VAR_DEP_NOT_IDENTIFIER" in codes


def test_depends_on_undeclared_identifier_rejected():
    """合法标识符但未声明，同样必须拦截。"""
    raw = [
        {
            "name": "vmid",
            "type": "string",
            "acquisition_strategy": "skill:hci-task-parsing",
            "acquisition_tool": "hci-task-parsing",
            "output_path": "values.vm",
            "depends_on": ["task_logs"],
        }
    ]
    report = validate_variable_schema(raw)
    codes = {i.code for i in report.errors}
    assert "SOP_VAR_DEP_UNDECLARED" in codes


def test_user_input_must_not_have_depends_on():
    """user_input 是链路起点，带 depends_on 说明配置错位（本案 task_logs 的形态）。"""
    raw = [
        {"name": "task_logs", "type": "json", "acquisition_strategy": "user_input", "depends_on": ["alert_logs"]},
        {"name": "alert_logs", "type": "json", "acquisition_strategy": "user_input", "description": "告警原文"},
    ]
    report = validate_variable_schema(raw)
    assert "SOP_VAR_MANUAL_HAS_DEPENDS" in {i.code for i in report.errors}


# ──────────────────────────────────────────────────────────────────────────────
# 根因 2：tool_call 缺工具名（运行时静默转人工的源头）
# ──────────────────────────────────────────────────────────────────────────────


def test_tool_call_without_tool_rejected():
    """线上真实坏数据：22 个 tool_call 缺 acquisition_tool。"""
    raw = [
        {"name": "qemu_log_path", "type": "string", "acquisition_strategy": "tool_call", "description": "QEMU日志路径"}
    ]
    report = validate_variable_schema(raw)
    assert "SOP_VAR_TOOL_MISSING" in {i.code for i in report.errors}


def test_skill_call_requires_output_path():
    raw = [
        {
            "name": "vmid",
            "type": "string",
            "acquisition_strategy": "skill:hci-task-parsing",
            "acquisition_tool": "hci-task-parsing",
            "depends_on": ["task_logs"],
        },
        {
            "name": "task_logs",
            "type": "json",
            "acquisition_strategy": "tool_call",
            "acquisition_tool": "acli_exec",
            "output_path": "stdout",
        },
    ]
    report = validate_variable_schema(raw)
    assert "SOP_VAR_OUTPUT_PATH_MISSING" in {i.code for i in report.errors}


# ──────────────────────────────────────────────────────────────────────────────
# 根因 3：未知策略被静默兜底
# ──────────────────────────────────────────────────────────────────────────────


def test_unknown_strategy_not_silently_fallback():
    """写错的策略不能被 parse_strategy 兜底成 user_input 后放过。"""
    raw = [{"name": "foo", "type": "string", "acquisition_strategy": "随便写的策略"}]
    report = validate_variable_schema(raw)
    assert "SOP_VAR_STRATEGY_UNKNOWN" in {i.code for i in report.errors}


# ──────────────────────────────────────────────────────────────────────────────
# 根因 4：path_template 与模板占位符
# ──────────────────────────────────────────────────────────────────────────────


def test_path_template_requires_expression():
    raw = [{"name": "qemu_log_path", "type": "string", "acquisition_strategy": "path_template"}]
    assert "SOP_VAR_EXPRESSION_MISSING" in {i.code for i in validate_variable_schema(raw).errors}


def test_path_template_ok_and_tokens_extracted():
    raw = [
        {"name": "task_day", "type": "string", "acquisition_strategy": "sop_default", "default_value": "2026-10-26"},
        {"name": "vmid", "type": "string", "acquisition_strategy": "sop_default", "default_value": "1001"},
        {
            "name": "qemu_log_path",
            "type": "string",
            "acquisition_strategy": "path_template",
            "expression": "/sf/log/{task_day}/sfvt_qemu_{vmid}.log",
            "depends_on": ["task_day", "vmid"],
            "description": "QEMU 日志路径",
        },
    ]
    report = validate_variable_schema(raw)
    assert not report.has_error, [i.model_dump() for i in report.errors]
    spec = SopVariableSchema.parse(raw).get("qemu_log_path")
    assert spec is not None
    assert spec.template_tokens() == {"task_day", "vmid"}


def test_path_template_token_unaligned_is_warning_only():
    raw = [
        {"name": "task_day", "type": "string", "acquisition_strategy": "sop_default", "default_value": "x"},
        {
            "name": "p",
            "type": "string",
            "acquisition_strategy": "path_template",
            "expression": "/sf/log/{task_day}/x.log",
            "depends_on": [],
            "description": "路径",
        },
    ]
    report = validate_variable_schema(raw)
    assert not report.has_error
    assert "SOP_VAR_TEMPLATE_DEP_UNALIGNED" in {i.code for i in report.warnings}


# ──────────────────────────────────────────────────────────────────────────────
# 集合级：重名 / 环 / 拓扑序
# ──────────────────────────────────────────────────────────────────────────────


def test_duplicate_variable_rejected():
    raw = [
        {"name": "a", "type": "string", "acquisition_strategy": "user_input", "description": "a"},
        {"name": "a", "type": "string", "acquisition_strategy": "user_input", "description": "a2"},
    ]
    assert "SOP_VAR_DUPLICATE" in {i.code for i in validate_variable_schema(raw).errors}


def test_dependency_cycle_rejected():
    raw = [
        {"name": "a", "type": "string", "acquisition_strategy": "derived", "expression": "b", "depends_on": ["b"]},
        {"name": "b", "type": "string", "acquisition_strategy": "derived", "expression": "a", "depends_on": ["a"]},
    ]
    report = validate_variable_schema(raw)
    assert "SOP_VAR_DEP_CYCLE" in {i.code for i in report.errors}


def test_topological_order_respects_dependencies():
    schema = SopVariableSchema.parse(SOP4_BASELINE)
    order = [spec.name for spec in schema.topological_order()]
    assert order.index("alert_logs") < order.index("node_ip")
    assert order.index("node_ip") < order.index("asan_disks")
    assert order.index("asan_disks") < order.index("disk_dev")
    assert len(order) == len(SOP4_BASELINE)


# ──────────────────────────────────────────────────────────────────────────────
# 输入形态兼容
# ──────────────────────────────────────────────────────────────────────────────


def test_string_depends_on_normalized():
    """存量可能存在 depends_on 为 "a, b" 字符串的形态。"""
    spec = SopVariableSpec.model_validate(
        {"name": "c", "type": "string", "acquisition_strategy": "derived", "expression": "a", "depends_on": "a, b"}
    )
    assert spec.depends_on == ["a", "b"]


def test_empty_and_none_input_ok():
    assert not validate_variable_schema(None).has_error
    assert not validate_variable_schema([]).has_error


def test_wrapped_variables_key_supported():
    report = validate_variable_schema({"variables": SOP4_BASELINE})
    assert not report.has_error


def test_publish_mode_requires_acquisition_path():
    """publish 态：必需变量必须有确定取值路径。"""
    raw = [{"name": "x", "type": "string", "acquisition_strategy": "user_input", "description": ""}]
    save_report = validate_variable_schema(raw, mode="save")
    publish_report = validate_variable_schema(raw, mode="publish")
    assert not save_report.has_error
    assert "SOP_VAR_PUBLISH_NO_ACQUISITION_PATH" in {i.code for i in publish_report.errors}


def test_parse_raises_with_report():
    with pytest.raises(SopVariableSchemaError) as exc:
        SopVariableSchema.parse([{"name": "任务原始数据", "acquisition_strategy": "user_input"}])
    assert exc.value.report.has_error
