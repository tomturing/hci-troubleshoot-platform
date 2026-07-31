import pytest
from app.routes.extract_signals import (
    _build_verification_contract,
    _enrich_signal,
    _normalize_config_file_read,
    _normalize_contract_variables,
    _normalize_derived_file_assertions,
    _validate_and_collect_signals,
)
from jsonschema import ValidationError
from shared.schemas.signal_output import derive_signal_requires, sync_signal_requires
from shared.schemas.signal_schema import (
    certify_publishable_signals_json,
    validate_publishable_signals_json,
    validate_signals_json,
)


def _doc(produce, *, tool="qfk_system", command="ps auxf"):
    return {
        "schema_version": 2,
        "signals": [
            {
                "acquire": {"tool": tool, "args": {"command": command} if tool == "qfk_system" else {"keyword": "失败"}},
                "match": None,
                "orchestrate": {"produces": [produce], "requires": []},
            }
        ],
    }


def test_old_json_path_and_empty_path_remain_compatible():
    validate_signals_json(_doc({"name": "PID", "type": "integer", "path": "data.0.pid"}))


def test_publish_certification_preserves_generation_origin_and_stamps_current_contract():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_001",
                "role": "must",
                "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
                "match": {"type": "exists", "expected": True},
                "orchestrate": {"requires": [], "produces": []},
            }
        ],
        "verification_contract": {
            "schema_version": 1,
            "evidence_policy": {"must": ["sig_001"], "minimum_should": 0},
        },
        "generation_metadata": {
            "schema_version": 1,
            "status": "current",
            "source_fingerprint": "0" * 64,
            "prompt_revision": "1" * 64,
            "model_id": "model-v1",
            "tool_contract_revision": "2" * 64,
            "generation_fingerprint": "3" * 64,
        },
    }

    certified = certify_publishable_signals_json(document)

    assert certified["publish_validation"]["status"] == "passed"
    assert certified["publish_validation"]["tool_contract_revision"] != "2" * 64
    assert certified["generation_metadata"]["tool_contract_revision"] == "2" * 64
    assert "publish_validation" not in document


def test_publish_rejects_unreachable_variable_dependency():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_001",
                "role": "must",
                "acquire": {"tool": "qfk_system", "args": {"command": "ps {{PID}}"}},
                "match": {"type": "exists", "expected": True},
                "orchestrate": {"requires": ["PID"], "produces": []},
            }
        ],
        "verification_contract": {
            "schema_version": 1,
            "evidence_policy": {"must": ["sig_001"], "minimum_should": 0},
        },
    }

    with pytest.raises(ValidationError, match="输入变量没有上游产出或外部声明: PID"):
        validate_publishable_signals_json(document)
    validate_signals_json(_doc({"name": "RAW", "path": ""}))


def test_match_extract_reuses_text_extract_schema_and_derives_requires():
    document = {
        "schema_version": 2,
        "signals": [{
            "id": "log_usage",
            "role": "must",
            "acquire": {"tool": "qfk_system", "args": {"command": "df", "command_args": ["/sf/log"]}},
            "match": {
                "type": "threshold", "aggregation": "max", "operator": ">", "value": 80, "expected": True,
                "extract": {"type": "text", "include": ["{{MOUNT}}"], "column_mode": "index", "column": 5, "cardinality": "all", "value_mode": "number"},
            },
            "orchestrate": {"produces": [], "requires": []},
        }],
    }
    validate_signals_json(document)
    assert derive_signal_requires(document["signals"][0]) == ["MOUNT"]


def test_verification_contract_external_variables_require_closed_types():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "storage_latency",
                "acquire": {
                    "tool": "qfk_system",
                    "args": {"command": "time ls {{STORAGE_PATH}}"},
                },
                "match": {"type": "exists", "expected": True},
                "orchestrate": {
                    "requires": ["STORAGE_PATH"],
                    "produces": [],
                },
            }
        ],
        "verification_contract": {
            "schema_version": 1,
            "variables": {"STORAGE_PATH": {}},
            "evidence_policy": {"must": ["storage_latency"]},
        },
    }

    with pytest.raises(ValidationError, match="'type' is a required property"):
        validate_signals_json(document)

    document["verification_contract"]["variables"]["STORAGE_PATH"] = {
        "type": "string"
    }
    validate_signals_json(document)


def test_publish_gate_requires_unique_stable_signal_ids():
    document = _doc({"name": "PID", "path": ""})

    with pytest.raises(ValidationError, match="缺少稳定 id"):
        validate_publishable_signals_json(document)

    document["signals"][0]["id"] = "sig_001"
    document["signals"].append(dict(document["signals"][0]))
    with pytest.raises(ValidationError, match="id 重复"):
        validate_publishable_signals_json(document)


def test_text_extract_contract_is_accepted():
    validate_signals_json(
        _doc(
            {
                "name": "KVM_PID",
                "type": "integer",
                "extract": {
                    "type": "text",
                    "include": ["-id {{VM}}"],
                    "column": 2,
                    "column_mode": "index",
                },
            }
        )
    )


@pytest.mark.parametrize(
    "produce",
    [
        {"name": "PID", "path": "data.0.pid", "extract": {"type": "text"}},
        {"name": "PID", "extract": {"type": "text", "column": 0}},
        {"name": "PID", "extract": {"type": "text", "column_mode": "index"}},
        {"name": "PID", "extract": {"type": "text", "unknown": True}},
    ],
)
def test_invalid_extract_contract_is_rejected(produce):
    with pytest.raises(ValidationError):
        validate_signals_json(_doc(produce))


def test_qkv_text_extract_and_qfk_pipe_are_rejected():
    with pytest.raises(ValidationError, match="只支持 JSON path"):
        validate_signals_json(_doc({"name": "HOST", "extract": {"type": "text"}}, tool="qkv_task"))
    with pytest.raises(ValidationError, match="禁止保存 shell 管道"):
        validate_signals_json(_doc({"name": "PID", "path": ""}, command="ps auxf | grep VM"))


def test_qkv_requires_produces_and_null_match():
    invalid_match = _doc({"name": "HOST", "path": "host"}, tool="qkv_task")
    invalid_match["signals"][0]["match"] = {
        "type": "keyword",
        "pattern": "",
        "mode": "or",
        "expected": True,
    }
    with pytest.raises(ValidationError, match="match 必须为 null"):
        validate_signals_json(invalid_match)

    no_produces = _doc({"name": "HOST", "path": "host"}, tool="qkv_task")
    no_produces["signals"][0]["orchestrate"]["produces"] = []
    with pytest.raises(ValidationError, match="必须配置 orchestrate.produces"):
        validate_signals_json(no_produces)


def test_requires_are_derived_from_args_and_extract_conditions():
    signal = _doc(
        {
            "name": "PID",
            "extract": {"type": "text", "include": ["-id {{VM}}", "{{TARGET}}"]},
        },
        command="ps auxf",
    )["signals"][0]
    signal["acquire"]["args"]["host"] = "{{HOST}}"
    assert derive_signal_requires(signal) == ["HOST", "TARGET", "VM"]
    assert sync_signal_requires(signal) == ["HOST", "TARGET", "VM"]
    assert signal["orchestrate"]["requires"] == ["HOST", "TARGET", "VM"]


def _matcher_doc(tool: str, args: dict, matcher: dict) -> dict:
    return {
        "schema_version": 2,
        "signals": [{
            "id": "sig_contract",
            "acquire": {"tool": tool, "args": args},
            "match": matcher,
            "orchestrate": {"requires": [], "produces": []},
        }],
    }


@pytest.mark.parametrize(
    ("tool", "args", "missing"),
    [
        ("qfk_log", {}, "file"),
        ("qfk_service", {}, "resource_keyword"),
        ("qfk_system", {}, "command"),
        ("qfk_vm", {}, "command"),
        ("qfk_network", {}, "command"),
        ("qfk_storage", {}, "command"),
        ("qfk_hardware", {}, "command"),
        ("qfk_platform", {}, "command"),
    ],
)
def test_qfk_runtime_required_args_are_rejected_at_save_time(tool, args, missing):
    matcher = {"type": "keyword", "pattern": "failed", "expected": True}

    with pytest.raises(ValidationError) as exc_info:
        validate_signals_json(_matcher_doc(tool, args, matcher))

    assert missing in str(exc_info.value)


@pytest.mark.parametrize(
    "matcher",
    [
        {"type": "keyword", "expected": True},
        {"type": "regex", "expected": True},
        {"type": "state", "expected": True},
        {"type": "threshold", "expected": True, "value": 10},
        {"type": "json_path", "expected": True},
        {"type": "unknown", "expected": True},
    ],
)
def test_incomplete_or_unknown_matcher_is_rejected(matcher):
    with pytest.raises(ValidationError):
        validate_signals_json(_matcher_doc("qfk_system", {"command": "ps auxf"}, matcher))


def test_invalid_regex_is_rejected_before_publish_or_execution():
    matcher = {"type": "regex", "pattern": "([", "expected": True}

    with pytest.raises(ValidationError, match="regex pattern 非法"):
        validate_signals_json(_matcher_doc("qfk_system", {"command": "ps auxf"}, matcher))


def test_qfk_log_contract_requires_executable_matcher_and_absolute_time():
    valid = _matcher_doc(
        "qfk_log",
        {"file": "sfvt_numa-server.log", "time_window": "2026-07-30 00:10:00"},
        {"type": "keyword", "pattern": "failed to set numa", "mode": "or", "expected": True},
    )
    validate_signals_json(valid)

    regex_signal = _matcher_doc(
        "qfk_log",
        {"file": "sfvt_numa-server.log"},
        {"type": "regex", "pattern": "failed.*numa", "expected": True},
    )
    validate_signals_json(regex_signal)

    relative_time = _matcher_doc(
        "qfk_log",
        {"file": "sfvt_numa-server.log", "time_window": "-1h"},
        {"type": "keyword", "pattern": "failed", "expected": True},
    )
    with pytest.raises(ValidationError, match="does not match"):
        validate_signals_json(relative_time)


def test_qfk_log_accepts_real_safe_basenames_but_rejects_unsafe_names():
    for file_name in (
        "LOG_ethtool_statistic.txt",
        "messages",
        "nic_list.ini",
        "sfvt_qemu_{{VM}}.log",
    ):
        log = _matcher_doc(
            "qfk_log",
            {"file": file_name, "path": "/sf/log/blackbox/today/"},
            {"type": "keyword", "pattern": "dropped", "expected": True},
        )
        validate_signals_json(log)

    bmc = _matcher_doc(
        "qfk_log",
        {"file": "BMC_Event_Log"},
        {"type": "keyword", "pattern": "restarted", "expected": True},
    )
    with pytest.raises(ValidationError, match="不能由本机 qfk_log 获取"):
        validate_signals_json(bmc)

    for file_name in ("../kernel.log", "..\\kernel.log", ".", "..", "kernel.log;reboot"):
        invalid = _matcher_doc(
            "qfk_log",
            {"file": file_name},
            {"type": "keyword", "pattern": "failed", "expected": True},
        )
        with pytest.raises(ValidationError):
            validate_signals_json(invalid)


def test_sf_cfg_read_is_normalized_to_read_only_system_cat():
    signal = {
        "id": "gpu_config",
        "role": "must",
        "acquire": {
            "tool": "qfk_log",
            "args": {
                "host": "{{HOST}}",
                "path": "/sf/cfg",
                "file": "gpu_info.ini",
                "instruction": "查看 GPU 配置文件",
            },
        },
        "match": {"type": "keyword", "pattern": "gpu_type=", "expected": False},
        "orchestrate": {"requires": ["HOST"], "produces": []},
        "review": {"notes": "", "require_human_confirm": False},
    }

    assert _normalize_config_file_read(signal) is True
    assert signal["acquire"] == {
        "tool": "qfk_system",
        "args": {
            "host": "{{HOST}}",
            "command": "cat",
            "resource_keyword": "/sf/cfg/gpu_info.ini",
            "instruction": "查看 GPU 配置文件",
        },
    }
    assert "确定性工具路由" in signal["review"]["notes"]
    validate_signals_json({"schema_version": 2, "signals": [signal]})


@pytest.mark.parametrize(
    ("path", "file_name"),
    [
        ("/etc", "shadow"),
        ("/sf/cfg/../data", "secret.ini"),
        ("/sf/cfg", "../gpu_info.ini"),
    ],
)
def test_config_read_normalizer_does_not_bypass_path_boundary(path, file_name):
    signal = {
        "acquire": {"tool": "qfk_log", "args": {"path": path, "file": file_name}},
    }

    assert _normalize_config_file_read(signal) is False
    assert signal["acquire"]["tool"] == "qfk_log"


def test_matchers_reuse_safe_file_acquisition_instead_of_cat_content_variable():
    producer = {
        "id": "config",
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "host": "{{HOST}}",
                "command": "cat",
                "resource_keyword": "/sf/cfg/gpu_info.ini",
                "instruction": "读取 GPU 配置",
            },
        },
        "match": None,
        "orchestrate": {
            "requires": ["HOST"],
            "produces": [{"name": "GPU_CONFIG_TEXT", "path": "$.text"}],
        },
    }
    assertion = {
        "id": "gpu-type",
        "acquire": {
            "tool": "qfk_system",
            "args": {
                "command": "cat",
                "resource_keyword": "{{GPU_CONFIG_TEXT}}",
                "instruction": "解析 GPU 配置",
            },
        },
        "match": {"type": "keyword", "pattern": "gpu_type=", "expected": True},
        "orchestrate": {"requires": ["GPU_CONFIG_TEXT"], "produces": []},
    }

    signals = [producer, assertion]
    assert _normalize_derived_file_assertions(signals) == 1
    assert assertion["acquire"]["args"] == producer["acquire"]["args"]
    assert "GPU_CONFIG_TEXT" in assertion["review"]["notes"]


def test_derived_file_assertion_normalizer_does_not_guess_ambiguous_producer():
    producers = [
        {
            "acquire": {
                "tool": "qfk_system",
                "args": {"command": "cat", "resource_keyword": path},
            },
            "match": None,
            "orchestrate": {"produces": [{"name": "TEXT", "path": "$.text"}]},
        }
        for path in ("/sf/cfg/a.ini", "/sf/cfg/b.ini")
    ]
    assertion = {
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "cat", "resource_keyword": "{{TEXT}}"},
        },
        "match": {"type": "keyword", "pattern": "enabled", "expected": True},
        "orchestrate": {"requires": ["TEXT"], "produces": []},
    }

    assert _normalize_derived_file_assertions([*producers, assertion]) == 0
    assert assertion["acquire"]["args"]["resource_keyword"] == "{{TEXT}}"


def test_save_gate_rejects_runtime_command_injection_chars():
    invalid = _matcher_doc(
        "qfk_system",
        {"command": 'mysql -e "select 1; select 2"'},
        {"type": "exists", "expected": True},
    )

    with pytest.raises(ValidationError, match="命令注入类非法字符"):
        validate_signals_json(invalid)

    canonical_placeholder = _matcher_doc(
        "qfk_system",
        {"command": "ping -c 4 {{NODE_IP}}"},
        {"type": "state", "pattern": "reachable", "expected": True},
    )
    validate_signals_json(canonical_placeholder)


def test_signal_provenance_accepts_image_region_source_refs():
    doc = _matcher_doc(
        "qfk_system",
        {"command": "lsof"},
        {"type": "exists", "expected": True},
    )
    doc["signals"][0]["provenance"] = {
        "category": "backend",
        "source_section": "steps_text",
        "source_refs": ["img:0/region:img_0:r_0", "section:steps_text/paragraph:3"],
        "evidence": "截图可见 lsof 输出",
    }

    validate_signals_json(doc)


def test_verification_contract_is_normalized_to_known_disjoint_signal_ids():
    signals = [
        {
            "id": "anchor",
            "role": "must",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机"}},
            "match": None,
            "orchestrate": {"phase": "diagnostic", "produces": [{"name": "HOST", "path": "host"}]},
        },
        {
            "id": "detail",
            "role": "should",
            "acquire": {"tool": "qfk_system", "args": {"command": "lsof"}},
            "match": {"type": "exists", "expected": True},
            "orchestrate": {"phase": "diagnostic", "produces": []},
        },
    ]
    contract = _build_verification_contract(
        signals,
        {
            "scope": {"products": ["HCI"], "unknown": ["drop"]},
            "evidence_policy": {
                "must": ["anchor", "missing"],
                "should": ["anchor", "detail"],
                "minimum_should": 9,
            },
        },
        case_id="37150",
    )

    assert contract is not None
    assert contract["scope"] == {"products": ["HCI"]}
    assert contract["evidence_policy"]["must"] == ["anchor"]
    assert contract["evidence_policy"]["should"] == ["detail"]
    assert contract["evidence_policy"]["minimum_should"] == 1
    validate_signals_json({"schema_version": 2, "signals": signals, "verification_contract": contract})


def test_solution_signal_is_context_and_cannot_satisfy_diagnostic_dependency():
    signals = [
        {
            "id": "set_bmc_ip",
            "role": "must",
            "acquire": {"tool": "qfk_hardware", "args": {"command": "set ip"}},
            "match": None,
            "orchestrate": {
                "phase": "solution",
                "requires": ["HOST"],
                "produces": [{"name": "NODE_IP", "path": "station_ip"}],
            },
        },
        {
            "id": "ping_bmc",
            "role": "must",
            "acquire": {
                "tool": "qfk_network",
                "args": {"command": "ping -c 4 {{NODE_IP}}", "host": "{{HOST}}"},
            },
            "match": {"type": "state", "pattern": "reachable", "expected": True},
            "orchestrate": {
                "phase": "diagnostic",
                "requires": ["HOST", "NODE_IP"],
                "produces": [],
            },
        },
    ]

    contract = _build_verification_contract(signals, {}, case_id="bmc")

    assert contract is not None
    assert contract["evidence_policy"]["must"] == ["ping_bmc"]
    assert contract["evidence_policy"]["context"] == ["set_bmc_ip"]
    assert set(contract["variables"]) == {"HOST", "NODE_IP"}


def test_human_confirmation_does_not_turn_read_only_diagnostic_into_solution():
    signal = {
        "id": "snapshot_policy",
        "role": "must",
        "acquire": {
            "tool": "qfk_storage",
            "args": {"command": "snapshot policy list"},
        },
        "match": {"type": "keyword", "pattern": "批量快照策略", "expected": True},
        "orchestrate": {"phase": "diagnostic", "requires": [], "produces": []},
        "review": {"require_human_confirm": True},
    }

    enriched = _enrich_signal(signal)

    assert enriched["orchestrate"]["phase"] == "diagnostic"
    assert enriched["review"]["require_human_confirm"] is True


def test_custom_external_variable_must_be_typed_and_is_preserved_in_contract():
    signal = {
        "id": "storage_latency",
        "role": "must",
        "acquire": {
            "tool": "qfk_system",
            "args": {"command": "time ls {{STORAGE_PATH}}"},
        },
        "match": {"type": "exists", "expected": True},
        "orchestrate": {"phase": "diagnostic", "produces": []},
    }
    proposed = {
        "variables": {
            "STORAGE_PATH": {
                "type": "string",
                "description": "待诊断存储挂载路径",
            },
            "bad-name": {"type": "string"},
            "UNTYPED": {},
        },
        "evidence_policy": {"must": ["storage_latency"]},
    }
    variables = _normalize_contract_variables(proposed)

    assert variables == {
        "STORAGE_PATH": {
            "type": "string",
            "description": "待诊断存储挂载路径",
        }
    }
    validated, rejected = _validate_and_collect_signals(
        [signal],
        "test:external-variable",
        set(variables),
    )
    assert rejected == []
    assert validated[0]["orchestrate"]["requires"] == ["STORAGE_PATH"]

    contract = _build_verification_contract(
        validated,
        proposed,
        case_id="37180",
    )

    assert contract is not None
    assert contract["variables"] == variables
    validate_signals_json(
        {
            "schema_version": 2,
            "signals": validated,
            "verification_contract": contract,
        }
    )


def test_verification_contract_rejects_missing_or_overlapping_references():
    doc = _matcher_doc(
        "qfk_system",
        {"command": "ps auxf"},
        {"type": "exists", "expected": True},
    )
    doc["signals"][0]["id"] = "sig_1"
    doc["verification_contract"] = {
        "schema_version": 1,
        "evidence_policy": {
            "must": ["sig_1"],
            "should": ["sig_1"],
            "minimum_should": 0,
            "on_missing_must": "inconclusive",
        },
    }
    with pytest.raises(ValidationError, match="同时属于"):
        validate_signals_json(doc)

    doc["verification_contract"]["evidence_policy"]["should"] = ["missing"]
    with pytest.raises(ValidationError, match="不存在的 signal_id"):
        validate_signals_json(doc)
