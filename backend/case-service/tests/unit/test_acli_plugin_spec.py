"""acli 插件规格域层单测：锁死「页面零凭据 + acli 硬契约 + 期望态形状」三条底线。

PR #1508 的教训是「能在页面上跑通」不等于「架构正确」：这里用纯域层断言
把服务端事实源、manifest 逐文件 hash、无 base64 依赖固定下来。

链路收口（D1）后本层多了一条边界：**域层只产期望态，不产设备侧执行脚本**。
装 / 备份 / 回滚 / 冒烟的编排语义由 terminal_bridge 的 acli_sync 第二阶段用 Go
实现；这里因此改为断言 descriptor 的键白名单，以及产物里不得再出现执行协议标记。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest
from app.domain.acli_plugin_spec import (
    ALLOWED_SPEC_FIELDS,
    BUILTIN_PLUGINS,
    FORBIDDEN_SPEC_FIELDS,
    MANIFEST_REQUIRED_FIELDS,
    MAX_PLUGIN_FILE_BYTES,
    REGISTER_REQUIRED_FIELDS,
    TEMPLATE_SPEC,
    SpecValidationError,
    build_descriptor,
    compile_descriptor,
    normalize_spec,
    validate_descriptor,
    validate_spec,
)

NOW = datetime(2026, 11, 14, 6, 0, 0, tzinfo=UTC)

# descriptor 的完整键集合：新增键＝平台又开始替设备决定"怎么做"，必须在这里显式认领。
DESCRIPTOR_KEYS = {
    "built_in",
    "descriptor_version",
    "enable_completion",
    "files",
    "manifest",
    "plugin_name",
    "register_item",
    "smoke_args",
    "smoke_command",
    "spec_hash",
    "update_mode",
    "verify_commands",
    "version",
    "wrapper",
}

# 旧下发时代的设备侧执行协议标记：它们只应存在于桥里。
EXECUTION_MARKERS = ("ACP_RESULT", "ACP_SMOKE", "flock", "trap ", "rm -rf", "plugin_register.lock")


def _spec(**overrides):
    return normalize_spec({**TEMPLATE_SPEC, **overrides})


def _descriptor(**overrides):
    return build_descriptor(_spec(**overrides), now=NOW)


def test_template_is_lmt_and_has_no_runtime_fields():
    assert "1mt" not in json.dumps(TEMPLATE_SPEC, ensure_ascii=False)
    assert TEMPLATE_SPEC["plugin_name"] == "hci_lmt_check"
    assert not (set(TEMPLATE_SPEC) & FORBIDDEN_SPEC_FIELDS)
    result = validate_spec(normalize_spec(TEMPLATE_SPEC))
    assert result["ok"], result["errors"]


@pytest.mark.parametrize(
    "field",
    ["node_ip", "password", "private_key", "bridge_url", "case_id", "credential_ref", "device_host"],
)
def test_runtime_fields_are_rejected(field):
    with pytest.raises(SpecValidationError) as exc:
        normalize_spec({**TEMPLATE_SPEC, field: "172.28.24.1"})
    assert field in exc.value.errors[0]


def test_unknown_fields_are_dropped_from_fact_source():
    spec = normalize_spec({**TEMPLATE_SPEC, "debug_panel_open": True, "admin_note": "keep?"})
    assert "debug_panel_open" not in spec
    assert "admin_note" not in spec


def test_env_code_is_dropped_not_rejected():
    """env_code 已移出规格白名单：作为未知键静默丢弃，不进入 spec_hash。"""
    assert "env_code" not in ALLOWED_SPEC_FIELDS
    assert "env_code" not in FORBIDDEN_SPEC_FIELDS
    spec = normalize_spec({**TEMPLATE_SPEC, "env_code": "24"})
    assert "env_code" not in spec
    assert validate_spec(spec)["ok"]
    with_env = build_descriptor(spec, now=NOW)
    without_env = build_descriptor(normalize_spec(TEMPLATE_SPEC), now=NOW)
    assert with_env["spec_hash"] == without_env["spec_hash"]


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({"plugin_name": "1mt_bad"}, "plugin_name"),
        ({"plugin_name": "NetDoctor"}, "plugin_name"),
        ({"version": "v1"}, "version"),
        ({"script_filename": "../evil.sh"}, "script_filename"),
        ({"script_content": ""}, "script_content"),
        ({"smoke_args": ""}, "smoke_args"),
        ({"smoke_args": "--help"}, "--help"),
        ({"smoke_args": "-m env; rm -rf /"}, "元字符"),
        ({"interpreter": "python3"}, "interpreter"),
        ({"timeout_seconds": 999}, "timeout_seconds"),
        ({"container": "vs-cp-manager"}, "container"),
    ],
)
def test_hard_contract_violations_are_rejected(overrides, expected):
    result = validate_spec(_spec(**overrides))
    assert not result["ok"]
    assert any(expected in error for error in result["errors"]), result["errors"]


def test_script_over_max_file_bytes_is_rejected_on_save():
    """单文件超限必须在保存时就拒绝，不能等客户侧 manifest_file_too_large 才发现。"""
    result = validate_spec(_spec(script_content="x" * (MAX_PLUGIN_FILE_BYTES + 1)))
    assert not result["ok"]
    assert any("script_content" in e and "1MiB" in e for e in result["errors"]), result["errors"]


def test_script_at_max_file_bytes_is_accepted():
    result = validate_spec(_spec(script_content="x" * MAX_PLUGIN_FILE_BYTES))
    assert result["ok"], result["errors"]


def test_new_plugin_cannot_collide_with_builtin():
    result = validate_spec(_spec(plugin_name="netdoctor"))
    assert not result["ok"]
    assert any("builtin_override" in error for error in result["errors"])


def test_builtin_override_requires_original_id_and_official_source():
    bad = validate_spec(_spec(update_mode="builtin_override", plugin_name="netdoctor", change_log=""))
    assert not bad["ok"]
    assert any("plugin_uuid" in error for error in bad["errors"]), bad["errors"]
    assert any("change_log" in error for error in bad["errors"]), bad["errors"]

    spec = _spec(update_mode="builtin_override", plugin_name="netdoctor", plugin_uuid="现场原id")
    assert validate_spec(spec)["ok"]
    assert spec["source"] == 1
    desc = build_descriptor(spec, now=NOW)
    assert desc["register_item"]["built_in"] is True
    assert desc["register_item"]["source"] == 1
    assert desc["register_item"]["id"] == "现场原id"
    assert validate_descriptor(desc)["ok"]


def test_builtin_override_keeps_factory_directory_layout():
    """内置覆盖只写 extra_files 声明的脚本，目录结构与出厂逐文件一致。"""
    spec = _spec(
        update_mode="builtin_override",
        plugin_name="netdoctor",
        plugin_uuid="现场原id",
        extra_files=[{"path": "scripts/netdoctor.sh", "mode": "0755", "content": "#!/bin/bash\nexit 0\n"}],
    )
    desc = build_descriptor(spec, now=NOW)
    paths = [item["path"] for item in desc["files"]]

    assert "netdoctor/plugin" not in paths
    assert not any("/bin/" in path for path in paths)
    assert "netdoctor/scripts/netdoctor.sh" in paths
    assert validate_descriptor(desc)["ok"]


def test_descriptor_manifest_and_hashes():
    desc = _descriptor()
    assert validate_descriptor(desc)["ok"], validate_descriptor(desc)["errors"]
    assert set(desc["manifest"]) >= MANIFEST_REQUIRED_FIELDS
    assert set(desc["register_item"]) >= REGISTER_REQUIRED_FIELDS
    assert desc["manifest"]["hash_algo"] == 1
    assert "manifest.json" not in desc["manifest"]["hash"]
    assert "plugin" in desc["manifest"]["hash"]
    assert "bin/troubleshoot.sh" in desc["manifest"]["hash"]
    for item in desc["files"]:
        assert len(item["sha256"]) == 64
    assert desc["register_item"]["built_in"] is False
    assert desc["register_item"]["source"] == 3


def test_wrapper_transparency_and_mode():
    desc = _descriptor()
    wrapper = desc["wrapper"]
    assert wrapper.startswith("#!/bin/bash")
    assert 'SCRIPT="$SCRIPT_DIR/bin/troubleshoot.sh"' in wrapper
    assert '"$@"' in wrapper
    assert next(item for item in desc["files"] if item["path"].endswith("/plugin"))["mode"] == "0755"
    assert "1mt" not in wrapper


def test_descriptor_is_desired_state_not_a_device_script():
    """域层只描述"应该是什么样"：出现设备侧执行协议标记就是旧链路回来了。"""
    desc = _descriptor()
    assert set(desc) == DESCRIPTOR_KEYS
    serialized = json.dumps(desc, ensure_ascii=False)
    for marker in EXECUTION_MARKERS:
        assert marker not in serialized, marker


def test_spec_hash_is_deterministic_and_covers_every_input():
    first = _descriptor()
    second = build_descriptor(_spec(), now=NOW)
    assert first["spec_hash"] == second["spec_hash"]

    changed_smoke = _descriptor(smoke_args="-m env -c mem_usage")
    assert changed_smoke["spec_hash"] != first["spec_hash"]

    changed_script = _descriptor(script_content="#!/bin/bash\necho changed\n")
    assert changed_script["spec_hash"] != first["spec_hash"]

    changed_version = _descriptor(version="0.0.2")
    assert changed_version["spec_hash"] != first["spec_hash"]


def test_spec_hash_ignores_the_build_stamp_but_compiled_freezes_it():
    """``spec_hash`` 不含 last_updated_at（否则同一规格永远算不出"无修改"），
    但 ``register_item.last_updated_at`` 必须随 revision 固化下来。"""
    early = build_descriptor(_spec(), now=datetime(2026, 11, 14, 6, tzinfo=UTC))
    late = build_descriptor(_spec(), now=datetime(2027, 1, 1, 0, tzinfo=UTC))
    assert early["spec_hash"] == late["spec_hash"]
    assert compile_descriptor(early)["register_item"]["last_updated_at"] != compile_descriptor(late)[
        "register_item"
    ]["last_updated_at"]


def test_compiled_payload_is_what_stage_two_writes():
    """编译载荷＝阶段二直接落盘的东西：逐文件四要素齐备，标量够判序与冒烟。"""
    compiled = compile_descriptor(_descriptor())
    assert {"version", "spec_hash", "update_mode", "built_in", "smoke_args", "enable_completion"} <= set(compiled)
    assert {"register_item", "files", "size"} <= set(compiled)
    for item in compiled["files"]:
        assert set(item) == {"path", "mode", "sha256", "content"}
        assert len(item["sha256"]) == 64
    # 桥要在没有网络的情况下完成收敛，所以内容与清单必须内联，不能只给引用。
    assert any(item["path"].endswith("/manifest.json") for item in compiled["files"])
    assert compiled["size"] == sum(len(item["content"].encode("utf-8")) for item in compiled["files"])


def test_compiled_payload_carries_no_credentials_or_device_targets():
    serialized = json.dumps(compile_descriptor(_descriptor()), ensure_ascii=False)
    for forbidden in ("password", "private_key", "node_ip", "172.28.", "ssh"):
        assert forbidden not in serialized, forbidden


def test_builtin_reference_list_matches_24_environment():
    assert set(BUILTIN_PLUGINS) == {
        "asan_ops",
        "asys",
        "log_collect",
        "netdoctor",
        "performance_tools",
        "vm_suspend",
        "vm_start",
    }
