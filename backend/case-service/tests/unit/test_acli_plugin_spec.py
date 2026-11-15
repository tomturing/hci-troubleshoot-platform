"""acli 插件规格域层单测：锁死「页面零凭据 + acli 硬契约 + 真冒烟」三条底线。

PR #1508 的教训是「能在页面上跑通」不等于「架构正确」：这里用纯域层断言
把服务端事实源、manifest 逐文件 hash、无 base64 依赖、真实生效验证固定下来。
"""

from __future__ import annotations

import json
import shutil
import subprocess
from datetime import UTC, datetime

import pytest
from app.domain.acli_plugin_spec import (
    BUILTIN_PLUGINS,
    FORBIDDEN_SPEC_FIELDS,
    MANIFEST_REQUIRED_FIELDS,
    REGISTER_REQUIRED_FIELDS,
    TEMPLATE_SPEC,
    SpecValidationError,
    build_descriptor,
    normalize_spec,
    parse_deploy_result,
    validate_descriptor,
    validate_spec,
)

NOW = datetime(2026, 11, 14, 6, 0, 0, tzinfo=UTC)


def _spec(**overrides):
    return normalize_spec({**TEMPLATE_SPEC, **overrides})


def _descriptor(**overrides):
    return build_descriptor(_spec(**overrides), deploy_id="htp-acli-test-0001", now=NOW)


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
    desc = build_descriptor(spec, deploy_id="htp-acli-test-builtin", now=NOW)
    assert desc["register_item"]["built_in"] is True
    assert desc["register_item"]["source"] == 1
    assert desc["register_item"]["id"] == "现场原id"
    assert "builtin_backup_failed" in desc["deploy_script"]
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


def test_spec_hash_is_deterministic_and_covers_every_input():
    first = _descriptor()
    second = build_descriptor(_spec(), deploy_id="htp-acli-test-0001", now=NOW)
    assert first["spec_hash"] == second["spec_hash"]

    changed_smoke = _descriptor(smoke_args="-m env -c mem_usage")
    assert changed_smoke["spec_hash"] != first["spec_hash"]

    changed_script = _descriptor(script_content="#!/bin/bash\necho changed\n")
    assert changed_script["spec_hash"] != first["spec_hash"]

    changed_version = _descriptor(version="0.0.2")
    assert changed_version["spec_hash"] != first["spec_hash"]


def test_deploy_script_is_valid_bash_without_missing_hci_dependencies():
    script = _descriptor()["deploy_script"]
    assert "base64" not in script
    assert "python3" not in script
    for token in ("flock -w 30 9", "sha256sum -c -", "jq --slurpfile", "mv \"$TMP/plugin_register.json\" \"$REG\""):
        assert token in script, token
    assert "/sf/bin/acli --timeout 60 \"$NAME\" $SMOKE_ARGS" in script
    assert "plugin_smoke_empty" in script
    if shutil.which("bash"):
        result = subprocess.run(["bash", "-n"], input=script, capture_output=True, text=True, check=False)
        assert result.returncode == 0, result.stderr


def test_deploy_command_writes_script_with_printf_only():
    desc = _descriptor()
    assert desc["deploy_command"].startswith("printf '%b' ")
    assert desc["deploy_script_path"] in desc["deploy_command"]
    assert "bash " in desc["deploy_command"]
    assert "base64" not in desc["deploy_command"]


def test_parse_deploy_result_only_accepts_success_with_smoke_output():
    desc = _descriptor()
    success = (
        "ACP_RESULT_BEGIN\n"
        + json.dumps(
            {
                "status": "success",
                "plugin_name": desc["plugin_name"],
                "version": desc["version"],
                "spec_hash": desc["spec_hash"],
                "deploy_id": desc["deploy_id"],
                "error": "",
            }
        )
        + "\nACP_RESULT_END\nACP_SMOKE_BEGIN\n巡检完成\nACP_SMOKE_END\n"
    )
    parsed = parse_deploy_result(success, exit_code=0)
    assert parsed["ok"] is True
    assert parsed["device_spec_hash"] == desc["spec_hash"]
    assert "巡检完成" in parsed["smoke_output"]

    assert parse_deploy_result(success, exit_code=1)["ok"] is False
    assert parse_deploy_result(success.replace("巡检完成", ""), exit_code=0)["ok"] is False

    rolled_back = success.replace('"status": "success"', '"status": "rolled_back"')
    assert parse_deploy_result(rolled_back, exit_code=0)["device_status"] == "rolled_back"
    assert parse_deploy_result("", exit_code=1)["ok"] is False


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
