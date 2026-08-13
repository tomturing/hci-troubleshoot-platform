"""KBD 与 Tool Registry 驱动离线资源同步的纯规则测试。"""

import pytest
from app.domain.collector_security import validate_collector_contract
from app.errors import DiagnosisError
from app.schemas.collector_definition import CollectorDefinitionWrite
from app.services.offline_resource_sync_service import (
    build_tool_collector_candidate,
    extract_requirements,
    normalize_acquirer,
    resolve_scenario,
    resolve_target_scope,
)


def make_tool(**overrides):
    """构造已发布 Tool 修订快照。"""

    return {
        "tool_name": "qfk_system",
        "display_name": "后端信号-系统检查",
        "usage_template": None,
        "parameters_schema": {
            "type": "object",
            "properties": {
                "command": {"type": "string"},
                "timeout": {"type": "integer", "minimum": 1, "maximum": 300, "default": 60},
            },
            "required": ["command"],
            "additionalProperties": False,
        },
        "risk_level": 1,
        "revision": 3,
        "version": "1.2.0",
        "checksum": "tool-checksum-3",
        **overrides,
    }


def test_resolve_scenario_uses_the_shared_final_kbd_category_only():
    """在线、离线诊断共用 KBD 最终分类，离线专属元数据和标题不能改写场景。"""

    assert (
        resolve_scenario(
            {
                "category_id": "虚拟机-027",
                "metadata": {"offline_scenario": "vm_migration_failed"},
                "title": "硬盘告警",
            }
        )
        == "虚拟机-027"
    )
    assert resolve_scenario({"category_id": "平台-023", "metadata": {}}) == "平台-023"
    assert resolve_scenario({"title": "虚拟机备份任务失败", "metadata": {}}) is None
    assert resolve_scenario({"category_id": "非法 场景"}) is None
    assert resolve_scenario({"category_id": "非法/场景"}) is None
    assert resolve_scenario({"category_id": "非法:场景"}) is None


def test_extract_requirements_only_reads_structured_signals():
    """自然语言步骤不能绕过安全目录生成命令。"""

    kbd = {
        "id": 42,
        "resource_revision": 3,
        "resource_checksum": "kbd-checksum-3",
        "category_id": "vm-start",
        "steps_text": "执行 rm -rf /",
        "signals_json": {
            "schema_version": "2.0",
            "signals": [
                {
                    "id": "signal-1",
                    "acquire": {"tool": "qkv.task", "args": {"keyword": "启动虚拟机"}},
                }
            ],
        },
    }

    assert extract_requirements(kbd) == [
        {
            "signal_id": "signal-1",
            "kbd_revision": 3,
            "kbd_checksum": "kbd-checksum-3",
            "tool": "qkv_task",
            "command": "启动虚拟机",
            "args": {"keyword": "启动虚拟机"},
            "matcher": {},
            "produces": [],
            "needs_review": False,
            "kbd_id": 42,
            "required_level": "mandatory",
            "time_window": {},
            "target_scope": "",
            "required_permissions": [],
            "sensitive_data_types": [],
            "support_id": "",
            "category_id": "vm-start",
        }
    ]
    assert normalize_acquirer("QFK.Log") == "qfk_log"


def test_extract_requirements_reads_v2_review_flags():
    """v2 信号的复核标记位于 provenance/review 段，必须与 v1 顶层字段等价生效。"""

    kbd = {
        "id": 43,
        "resource_revision": 4,
        "resource_checksum": "kbd-checksum-4",
        "category_id": "vm-backup",
        "signals_json": {
            "schema_version": 2,
            "signals": [
                {
                    "id": "signal-v2-needs-review",
                    "acquire": {"tool": "qfk_log", "args": {"resource_keyword": "backup"}},
                    "provenance": {"needs_review": True},
                    "review": {"require_human_confirm": False},
                },
                {
                    "id": "signal-v2-human-confirm",
                    "acquire": {"tool": "qfk_log", "args": {"resource_keyword": "restore"}},
                    "provenance": {"needs_review": False},
                    "review": {"require_human_confirm": True},
                },
                {
                    "id": "signal-v2-clean",
                    "acquire": {"tool": "qfk_log", "args": {"resource_keyword": "clean"}},
                    "provenance": {"needs_review": False},
                    "review": {"require_human_confirm": False},
                },
            ],
        },
    }

    requirements = extract_requirements(kbd)
    assert [item["needs_review"] for item in requirements] == [True, True, False]


def test_qkv_dialog_expands_both_reviewed_log_paths():
    """弹框信号必须生成两个独立 Collector，不能静默漏掉 vt 日志域。"""

    requirements = extract_requirements(
        {
            "id": 44,
            "resource_revision": 5,
            "resource_checksum": "kbd-checksum-5",
            "category_id": "vm-start",
            "signals_json": {
                "signals": [
                    {
                        "id": "dialog",
                        "acquire": {
                            "tool": "qkv_dialog",
                            "args": {
                                "keyword": "编辑显卡核心失败",
                                "paths": ["/sf/log/today", "/sf/log/today/vt"],
                                "context_lines": 2,
                            },
                        },
                    }
                ]
            },
        }
    )
    assert [item["dialog_path"] for item in requirements] == ["/sf/log/today", "/sf/log/today/vt"]

    tool = make_tool(
        tool_name="qkv_dialog",
        display_name="前端信号-弹框日志定位",
        parameters_schema={
            "type": "object",
            "properties": {
                "keyword": {"type": "string"},
                "paths": {
                    "type": "array",
                    "items": {"type": "string", "enum": ["/sf/log/today", "/sf/log/today/vt"]},
                },
                "context_lines": {"type": "integer", "minimum": 0, "maximum": 10},
            },
            "required": ["keyword"],
            "additionalProperties": False,
        },
    )
    candidates = [build_tool_collector_candidate(item, tool, version="1.0.1")[0] for item in requirements]
    assert {item["command_template"] for item in candidates} == {
        "acli log get -k {keyword} -p {path} -c {context_lines}"
    }
    assert len({item["collector_id"] for item in candidates}) == 2


def test_shared_resolver_builds_valid_collector_without_usage_template():
    """Collector 命令来自 Shared Resolver，Tool 展示模板可以为空。"""

    candidate, parameters, _query_type = build_tool_collector_candidate(
        {
            "tool": "qfk_system",
            "args": {"command": "iostat"},
            "matcher": {},
        },
        make_tool(),
        version="1.0.7",
    )
    command = CollectorDefinitionWrite.model_validate(candidate)
    validate_collector_contract(command.command_template, command.parameter_schema)
    assert command.command_template == "acli --timeout {timeout} system {command}"
    assert parameters == {"timeout": 60, "command": "iostat"}
    assert command.generation_metadata["tool_revision"] == 3
    assert command.generation_metadata["resolution_catalog_version"]
    assert command.generation_metadata["resolution_snapshot"]["argv"][-2:] == ["system", "iostat"]


def test_runtime_target_placeholder_is_validated_without_becoming_a_frozen_parameter():
    """target_id 由采集计划运行时注入，同步预检不得误报未解析或固化假目标。"""

    candidate, parameters, _query_type = build_tool_collector_candidate(
        {
            "tool": "qfk_vm",
            "args": {
                "command": "status get",
                "command_args": ["--vm-id", "{{VM_ID}}"],
                "host": "{{HOST}}",
                "resource_keyword": "{{VM_ID}}",
            },
            "matcher": {"type": "exists", "expected": True},
        },
        make_tool(
            tool_name="qfk_vm",
            parameters_schema={
                "type": "object",
                "properties": {
                    "command": {"type": "string"},
                    "command_args": {"type": "array", "items": {"type": "string"}},
                    "host": {"type": "string"},
                    "resource_keyword": {"type": "string"},
                },
                "required": ["command"],
                "additionalProperties": False,
            },
        ),
        version="1.0.7",
    )

    assert "{target_id}" in candidate["command_template"]
    assert "target_id" not in parameters
    assert "target_id" not in candidate["parameter_schema"]["properties"]
    assert candidate["supported_product_versions"] == [">=6.12.0"]


def test_qfk_log_uses_acli_and_freezes_kbd_parameters():
    """qfk_log 必须生成 acli 直执行模板，不能回退到 journalctl。"""

    tool = make_tool(
        tool_name="qfk_log",
        display_name="后端信号-日志检查",
        usage_template=None,
        parameters_schema={
            "type": "object",
            "properties": {
                "file": {"type": "string"},
                "path": {"type": "string"},
                "time_window": {"type": "string"},
            },
            "required": ["file"],
            "additionalProperties": False,
        },
    )
    candidate, parameters, _query_type = build_tool_collector_candidate(
        {
            "tool": "qfk_log",
            "args": {
                "file": "vm-manager-api.log",
                "path": "/sf/log/",
                "time_window": "2026-08-10 10:00:00",
            },
            "matcher": {"type": "keyword", "pattern": ["准备备份链失败", "error"]},
        },
        tool,
        version="1.0.7",
    )
    command = CollectorDefinitionWrite.model_validate(candidate)
    validate_collector_contract(command.command_template, command.parameter_schema)
    assert command.command_template == "acli log get -E -k {keyword} -f {file} -p {path} -t {time_window}"
    assert "journalctl" not in command.command_template
    assert parameters["file"] == "vm-manager-api.log"
    assert parameters["keyword"] == "error|准备备份链失败"


def test_qfk_log_rejects_unbounded_global_keyword_collection():
    """qfk_log 必须与在线契约一致：常规日志需要 Catalog 可识别的 file。"""

    tool = make_tool(
        tool_name="qfk_log",
        display_name="后端信号-日志检查",
        usage_template=None,
        parameters_schema={
            "type": "object",
            "properties": {"file": {"type": "string"}, "resource_keyword": {"type": "string"}},
            "required": ["file"],
            "additionalProperties": False,
        },
    )
    with pytest.raises(DiagnosisError) as exc_info:
        build_tool_collector_candidate(
            {"tool": "qfk_log", "args": {"resource_keyword": "backup failed"}, "matcher": {}},
            tool,
            version="1.0.7",
        )
    assert exc_info.value.code == "COLLECTOR_PARAMETER_VALIDATION_FAILED"


def test_collector_identity_tracks_execution_semantics_not_metadata_revision():
    """Collector 身份跟随执行语义，Tool 元数据修订只生成新资源修订。"""

    requirement = {"tool": "qfk_system", "args": {"command": "iostat"}, "matcher": {}}
    first, _, _ = build_tool_collector_candidate(requirement, make_tool(revision=3), version="1.0.1")
    second, _, _ = build_tool_collector_candidate(
        requirement,
        make_tool(revision=4, version="1.3.0", checksum="tool-checksum-4"),
        version="1.0.2",
    )
    changed_template, _, _ = build_tool_collector_candidate(
        requirement,
        make_tool(
            revision=5,
            version="1.4.0",
            checksum="tool-checksum-5",
            usage_template="这只是展示文案，不参与命令编译",
        ),
        version="1.0.3",
    )

    assert first["collector_id"] == second["collector_id"]
    assert first["generation_metadata"]["tool_revision"] != second["generation_metadata"]["tool_revision"]
    assert (
        first["generation_metadata"]["execution_contract_checksum"]
        != second["generation_metadata"]["execution_contract_checksum"]
    )
    assert first["collector_id"] == changed_template["collector_id"]


def test_collector_parameter_schema_inherits_tool_registry_constraints():
    """Collector 不得在编译时丢失 Tool 的 enum/pattern 安全边界。"""

    candidate, _, _ = build_tool_collector_candidate(
        {"tool": "qfk_system", "args": {"command": "iostat"}, "matcher": {}},
        make_tool(
            parameters_schema={
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "enum": ["iostat", "lsblk"],
                        "description": "只读子命令",
                    }
                },
                "required": ["command"],
                "additionalProperties": False,
            }
        ),
        version="1.0.1",
    )

    assert candidate["parameter_schema"]["properties"]["command"] == {
        "type": "string",
        "enum": ["iostat", "lsblk"],
    }


def test_kbd_parameter_outside_tool_contract_is_rejected_during_preview_compilation():
    """KBD 参数超出 Tool 契约时必须在同步阶段失败。"""

    with pytest.raises(DiagnosisError) as exc_info:
        build_tool_collector_candidate(
            {"tool": "qfk_system", "args": {"command": "smartctl"}, "matcher": {}},
            make_tool(
                parameters_schema={
                    "type": "object",
                    "properties": {"command": {"type": "string", "enum": ["iostat", "lsblk"]}},
                    "required": ["command"],
                    "additionalProperties": False,
                }
            ),
            version="1.0.1",
        )

    assert exc_info.value.code == "COLLECTOR_PARAMETER_VALIDATION_FAILED"


def test_target_scope_depends_on_compiled_target_binding_not_vm_tool_name():
    """vm list 可按节点执行；只有真实引用 target_id 的命令才要求故障对象。"""

    assert resolve_target_scope([], "qfk_vm", "acli --formatter json vm list") == "source_node"
    assert (
        resolve_target_scope([], "qfk_vm", "acli --formatter json vm status get --vm-id {target_id}")
        == "affected_object"
    )
    assert (
        resolve_target_scope([{"target_scope": "once"}], "qfk_vm", "acli vm status get --vm-id {target_id}")
        == "once"
    )
