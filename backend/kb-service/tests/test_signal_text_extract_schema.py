import pytest
from jsonschema import ValidationError
from shared.resolution.review import SignalReviewFeature, SignalReviewStatus, review_signal_document
from shared.schemas.signal_output import derive_signal_requires
from shared.schemas.signal_schema import (
    humanize_signal_validation_error,
    normalize_optional_matcher_nulls,
    validate_kbd_publishable_signals_json,
    validate_publishable_signals_json,
    validate_signals_json,
)


def _text_extract(*, rows=None, columns=None, value_key=None, value_mode="string"):
    extract = {
        "type": "text",
        "rows": rows or {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "value_mode": value_mode,
    }
    if columns:
        extract.update(
            {
                "parser": "whitespace_table",
                "header": {"mode": "contains", "required": ["Filesystem", "Use%"]},
                "columns": columns,
            }
        )
    if value_key:
        extract["value_key"] = value_key
    return extract


def _qfk_match(matcher, *, command="df -P /sf/log"):
    return {
        "schema_version": 2,
        "signals": [
            {
                "acquire": {"tool": "qfk_system", "args": {"command": command}},
                "match": matcher,
                "orchestrate": {"produces": [], "requires": []},
            }
        ],
    }


def _qfk_produce(produce):
    return {
        "schema_version": 2,
        "signals": [
            {
                "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
                "match": None,
                "orchestrate": {"produces": [produce], "requires": []},
            }
        ],
    }


def _qfk_log_produce(rows):
    return {
        "schema_version": 2,
        "signals": [
            {
                "acquire": {"tool": "qfk_log", "args": {"file": "sfvt_vtpdaemon.log"}},
                "match": None,
                "orchestrate": {
                    "produces": [
                        {
                            "name": "DUP_IP",
                            "type": "string",
                            "extract": {
                                "type": "text",
                                "rows": rows,
                                "cardinality": "all",
                                "source": "stdout",
                                "ai_extract": {"instruction": "提取其中的第一个 IP 地址"},
                            },
                        }
                    ],
                    "requires": [],
                },
            }
        ],
    }


def test_match_and_produces_reuse_the_same_value_extract_contract():
    extract = _text_extract(
        rows={
            "mode": "keywords",
            "include": ["{{MOUNT}}"],
            "exclude": [],
            "include_mode": "all",
            "case_sensitive": True,
        },
        columns=[{"key": "USE_PERCENT", "selector": {"by": "header", "name": "Use%"}, "value_mode": "number"}],
        value_key="USE_PERCENT",
        value_mode="number",
    )
    matcher = {"type": "threshold", "operator": ">", "value": 80, "expected": True, "extract": extract}
    match_doc = _qfk_match(matcher)
    validate_signals_json(match_doc)
    assert derive_signal_requires(match_doc["signals"][0]) == ["MOUNT"]
    validate_signals_json(_qfk_produce({"name": "USE_PERCENT", "type": "number", "extract": extract}))


def test_numeric_matcher_threshold_accepts_canonical_variable_reference():
    extract = _text_extract(value_mode="number")
    matcher = {"type": "threshold", "operator": ">", "value": "{{THRESHOLD}}", "expected": True, "extract": extract}

    validate_signals_json(_qfk_match(matcher))
    assert derive_signal_requires({"acquire": {"tool": "qfk_system", "args": {}}, "match": matcher}) == ["THRESHOLD"]


def test_numeric_matcher_rejects_arbitrary_string_threshold():
    extract = _text_extract(value_mode="number")
    matcher = {"type": "threshold", "operator": ">", "value": "80ms", "expected": True, "extract": extract}

    with pytest.raises(ValidationError):
        validate_signals_json(_qfk_match(matcher))


def _count_extract():
    return {
        "type": "text",
        "rows": {"mode": "keywords", "include": ["failed"], "exclude": [], "include_mode": "all"},
        "cardinality": "count",
        "source": "stdout",
        "value_mode": "integer",
    }


def test_count_cardinality_is_a_numeric_row_selection_projection():
    validate_signals_json(
        _qfk_match({"type": "threshold", "operator": ">=", "value": 2, "expected": True, "extract": _count_extract()})
    )
    validate_signals_json(_qfk_produce({"name": "FAILED_COUNT", "type": "integer", "extract": _count_extract()}))
    validate_signals_json(_qfk_produce({"name": "FAILED_COUNT", "type": "number", "extract": _count_extract()}))
    validate_signals_json(
        _qfk_produce(
            {
                "name": "DATA_ROW_COUNT",
                "type": "integer",
                "extract": {
                    **_count_extract(),
                    "header": {"mode": "contains", "required": ["Filesystem", "Use%"]},
                    "rows": {"mode": "indices", "basis": "data", "indices": [1]},
                },
            }
        )
    )


@pytest.mark.parametrize(
    "document",
    [
        _qfk_match({"type": "exists", "expected": True, "extract": _count_extract()}),
        _qfk_match(
            {
                "type": "threshold",
                "aggregation": "line_count",
                "operator": ">=",
                "value": 2,
                "expected": True,
                "extract": _count_extract(),
            }
        ),
        _qfk_produce({"name": "FAILED_COUNT", "type": "string", "extract": _count_extract()}),
        _qfk_produce(
            {
                "name": "FAILED_COUNT",
                "type": "integer",
                "extract": {**_count_extract(), "columns": [{"key": "VALUE", "selector": {"by": "index", "index": 1}}]},
            }
        ),
    ],
)
def test_count_cardinality_rejects_incompatible_consumers_or_transforms(document):
    with pytest.raises(ValidationError):
        validate_signals_json(document)


def test_matcher_extract_is_required_and_json_path_is_not_a_matcher_type():
    with pytest.raises(ValidationError, match="extract"):
        validate_signals_json(_qfk_match({"type": "exists", "expected": True}))
    with pytest.raises(ValidationError):
        validate_signals_json(
            _qfk_match(
                {"type": "json_path", "path": "status", "expected": True, "extract": {"type": "json", "path": "status"}}
            )
        )


def test_exists_matcher_explicit_null_pattern_is_semantically_absent():
    document = _qfk_match(
        {
            "type": "exists",
            "pattern": None,
            "expected": True,
            "extract": _text_extract(),
        }
    )

    normalize_optional_matcher_nulls(document)
    validate_signals_json(document)

    assert "pattern" not in document["signals"][0]["match"]


def test_required_keyword_null_pattern_is_not_silently_removed_and_has_field_target():
    document = _qfk_match(
        {
            "type": "keyword",
            "pattern": None,
            "expected": True,
            "extract": _text_extract(),
        }
    )
    document["signals"][0]["id"] = "sig_kbd30880"

    normalize_optional_matcher_nulls(document)
    with pytest.raises(ValidationError) as exc_info:
        validate_signals_json(document)

    issue = humanize_signal_validation_error(exc_info.value, document["signals"])
    assert document["signals"][0]["match"]["pattern"] is None
    assert issue["signal_id"] == "sig_kbd30880"
    assert issue["field_path"] == "match.pattern"
    assert issue["location"] == "关键信号 · sig_kbd30880 · 判定器 / 匹配内容"
    assert "值类型不正确" in issue["message"]


@pytest.mark.parametrize(
    "extract",
    [
        {"type": "text", "include": ["old"]},
        {"type": "text", "rows": {"mode": "all"}, "column": 2},
        {"type": "text", "rows": {"mode": "all"}, "column_mode": "index"},
    ],
)
def test_old_single_column_text_extract_fields_are_rejected(extract):
    with pytest.raises(ValidationError):
        validate_signals_json(_qfk_produce({"name": "VALUE", "type": "string", "extract": extract}))


def test_qfk_produces_path_is_rejected_but_json_extract_is_allowed():
    with pytest.raises(ValidationError, match="新版 extract"):
        validate_signals_json(_qfk_produce({"name": "PID", "type": "integer", "path": "data.0.pid"}))
    validate_signals_json(
        _qfk_produce(
            {
                "name": "PID",
                "type": "integer",
                "extract": {
                    "type": "json",
                    "path": "data[0].pid",
                    "cardinality": "exactly_one",
                    "source": "stdout",
                    "value_mode": "integer",
                },
            }
        )
    )


def test_qfk_produce_path_and_extract_conflict_has_actionable_message():
    document = _qfk_produce(
        {
            "name": "DUP_IP",
            "type": "string",
            "path": "",
            "extract": _text_extract(rows={"mode": "keywords", "include": ["检测到IP", "冲突"]}),
        }
    )
    document["signals"][0]["id"] = "sig_kbd27736_dup_ip"

    with pytest.raises(ValidationError) as exc_info:
        validate_signals_json(document)

    issue = humanize_signal_validation_error(exc_info.value, document["signals"])
    assert issue["code"] == "PRODUCE_PATH_EXTRACT_CONFLICT"
    assert issue["field_path"] == "orchestrate.produces.0"
    assert "path" in issue["message"]
    assert "extract" in issue["message"]


def test_text_extract_allows_grounded_ai_extract_instruction():
    document = _qfk_produce(
        {
            "name": "DUP_IP",
            "type": "string",
            "extract": {
                **_text_extract(rows={"mode": "all"}),
                "ai_extract": {"instruction": "提取其中的第一个 IP 地址"},
            },
        }
    )

    validate_signals_json(document)


def test_qfk_log_produce_accepts_bounded_complete_line_filter_for_kbd27736():
    document = _qfk_log_produce(
        {
            "mode": "keywords",
            "scope": "same_record",
            "include": ["检测到IP", "冲突"],
            "exclude": ["测试数据", "模拟冲突"],
            "include_mode": "all",
            "exclude_mode": "any",
            "case_sensitive": True,
        }
    )

    validate_signals_json(document)


def test_qfk_log_produce_rejects_exclude_only_filter_as_unbounded():
    document = _qfk_log_produce(
        {
            "mode": "keywords",
            "include": [],
            "exclude": ["测试数据"],
            "include_mode": "all",
            "exclude_mode": "any",
            "case_sensitive": True,
        }
    )

    with pytest.raises(ValidationError, match="仅排除关键字不能限制输出范围"):
        validate_signals_json(document)


def test_semantic_qfk_error_keeps_signal_id_and_editable_field_path():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_kbd23821_log",
                "acquire": {"tool": "qfk_log", "args": {"file": "sfvt_vtpdaemon.log"}},
                # 同时提供 match 和 produces，复现 KBD23821 保存时最容易被吞掉的跨字段门禁。
                "match": {
                    "type": "keyword",
                    "pattern": "Completed",
                    "expected": True,
                    "extract": _text_extract(rows={"mode": "all"}),
                },
                "orchestrate": {
                    "produces": [{"name": "END", "path": "end"}],
                    "requires": [],
                },
            }
        ],
    }

    with pytest.raises(ValidationError) as exc_info:
        validate_signals_json(document)

    issue = humanize_signal_validation_error(exc_info.value, document["signals"])
    assert issue["code"] == "QFK_OUTPUT_MODE_CONFLICT"
    assert issue["signal_id"] == "sig_kbd23821_log"
    assert issue["field_path"] == "match"
    assert issue["action"] == {
        "type": "edit_signal",
        "signal_id": "sig_kbd23821_log",
        "focus": "match",
    }
    assert "二选一" in issue["message"]


def test_qfk_log_unsupported_predicate_points_to_match_type():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_log_predicate",
                # 用 qemu_vm 作为“仍不支持 delta”的对照源（vtpdaemon 已在方案 A 补齐 delta）；
                # 该测试仅验证：当 source 的 predicates 确实不含某数值谓词时，校验器应拒绝并指向 match.type
                "acquire": {"tool": "qfk_log", "args": {"file": "sfvt_qemu_vm.log"}},
                "match": {
                    "type": "delta",
                    "operator": ">",
                    "value": 1,
                    "minimum_samples": 2,
                    "expected": True,
                    "extract": _text_extract(rows={"mode": "all"}),
                },
                "orchestrate": {"produces": [], "requires": []},
            }
        ],
    }

    with pytest.raises(ValidationError) as exc_info:
        validate_signals_json(document)

    issue = humanize_signal_validation_error(exc_info.value, document["signals"])
    assert issue["signal_id"] == "sig_log_predicate"
    assert issue["field_path"] == "match.type"
    assert "当前日志源不能直接执行该数值判定" in issue["message"]


def test_qfk_log_numeric_ai_extract_allows_delta_on_source_without_direct_delta():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_log_delta_ai",
                "acquire": {"tool": "qfk_log", "args": {"file": "sfvt_vtpdaemon.log"}},
                "match": {
                    "type": "delta",
                    "operator": "==",
                    "value": 0,
                    "minimum_samples": 2,
                    "expected": True,
                    "extract": {
                        **_text_extract(rows={"mode": "keywords", "include": ["Completed"], "exclude": []}),
                        "ai_extract": {"instruction": "按出现顺序提取 completed 和 total 两个字节数"},
                    },
                },
                "orchestrate": {"produces": [], "requires": []},
            }
        ],
    }

    validate_signals_json(document)


def test_qfk_produce_names_are_unique_case_insensitively():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_duplicate_produce",
                "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
                "match": None,
                "orchestrate": {
                    "produces": [
                        {"name": "TOTAL", "type": "number", "extract": _text_extract(value_mode="number")},
                        {"name": "total", "type": "number", "extract": _text_extract(value_mode="number")},
                    ],
                    "requires": [],
                },
            }
        ],
    }

    with pytest.raises(ValidationError, match="产出变量名重复"):
        validate_signals_json(document)


def test_variable_dependency_error_targets_signal_that_declares_missing_input():
    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_missing_input",
                "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
                "match": {
                    "type": "exists",
                    "expected": True,
                    "extract": _text_extract(rows={"mode": "all"}),
                },
                "orchestrate": {"produces": [], "requires": ["NOT_DECLARED"]},
            }
        ],
        "verification_contract": {
            "schema_version": 1,
            "variables": {},
            "evidence_policy": {"must": ["sig_missing_input"], "should": [], "exclude": [], "context": []},
        },
    }

    with pytest.raises(ValidationError) as exc_info:
        validate_publishable_signals_json(document)

    issue = humanize_signal_validation_error(exc_info.value, document["signals"])
    assert issue["code"] == "SIGNAL_VARIABLE_DEPENDENCY_INVALID"
    assert issue["signal_id"] == "sig_missing_input"
    assert issue["field_path"] == "orchestrate.requires"
    assert "NOT_DECLARED" in issue["message"]


def test_variable_name_format_is_independent_from_external_variable_catalog():
    """合法的大写变量可以由 Verification Contract 声明，不受默认目录限制。"""
    document = _qfk_match(
        {"type": "exists", "expected": True, "extract": _text_extract()},
    )
    document["signals"][0]["orchestrate"] = {
        "produces": [],
        "requires": ["VM_DISK_ID"],
    }
    document["verification_contract"] = {
        "schema_version": 1,
        "variables": {"VM_DISK_ID": {"type": "string"}},
        "evidence_policy": {
            "must": [document["signals"][0].get("id", "sig_001")],
            "should": [],
            "exclude": [],
            "context": [],
        },
    }
    document["signals"][0]["id"] = "sig_disk"
    document["verification_contract"]["evidence_policy"]["must"] = ["sig_disk"]

    validate_publishable_signals_json(document)


def test_kbd_publish_gate_rejects_consumer_only_document_with_external_variables():
    """外部变量可满足依赖，但不能替代描述故障入口的 QKV 生产者。"""

    document = _qfk_match({"type": "exists", "expected": True, "extract": _text_extract()})
    document["signals"][0]["id"] = "consumer_only"
    document["verification_contract"] = {
        "schema_version": 1,
        "variables": {},
        "evidence_policy": {"must": ["consumer_only"], "should": [], "exclude": [], "context": []},
    }

    # 通用 Signal/Playbook 契约仍允许纯消费者文档，KBD 发布契约必须拒绝。
    validate_publishable_signals_json(document)
    with pytest.raises(ValidationError) as exc_info:
        validate_kbd_publishable_signals_json(document)

    issue = humanize_signal_validation_error(exc_info.value, document["signals"])
    assert issue["code"] == "KBD_PRODUCER_SIGNAL_MISSING"
    assert issue["field_path"] == "signals"
    assert "生产者信号" in issue["message"]

    review = review_signal_document(document, feature=SignalReviewFeature.PUBLISH)
    assert review.status is SignalReviewStatus.BLOCKED
    assert any(
        item.code == "KBD_PRODUCER_SIGNAL_MISSING" and "至少需要 1 条生产者信号" in item.message
        for item in review.issues
    )


def test_unscoped_semantic_error_never_falls_back_to_vague_rule_message():
    issue = humanize_signal_validation_error(
        ValidationError("signals[0] 的自定义取值规则失败：列名 VALUE 不存在"),
        [{"id": "sig_custom"}],
    )

    assert issue["signal_id"] == "sig_custom"
    assert "列名 VALUE 不存在" in issue["message"]
    assert "未满足当前关键信号规则" not in issue["message"]


def test_signal_id_and_pipeline_errors_keep_precise_targets():
    missing_id = _qfk_match({"type": "exists", "expected": True, "extract": _text_extract()})
    with pytest.raises(ValidationError) as exc_info:
        validate_publishable_signals_json(missing_id)
    issue = humanize_signal_validation_error(exc_info.value, missing_id["signals"])
    assert issue["code"] == "SIGNAL_ID_INVALID"
    assert issue["field_path"] == "id"
    assert "内部标识" in issue["message"]

    pipeline = _qfk_match({"type": "exists", "expected": True, "extract": _text_extract()}, command="ps | grep java")
    pipeline["signals"][0]["id"] = "sig_pipeline"
    with pytest.raises(ValidationError) as exc_info:
        validate_signals_json(pipeline)
    issue = humanize_signal_validation_error(exc_info.value, pipeline["signals"])
    assert issue["code"] == "SIGNAL_COMMAND_PIPELINE_UNSUPPORTED"
    assert issue["signal_id"] == "sig_pipeline"
    assert issue["field_path"] == "acquire.args.command"


def test_keyword_rows_reject_unknown_scope_and_exclude_relation():
    invalid_scope = _qfk_log_produce(
        {
            "mode": "keywords",
            "scope": "whole_output",
            "include": ["冲突"],
            "exclude": [],
            "include_mode": "all",
            "exclude_mode": "any",
            "case_sensitive": True,
        }
    )
    invalid_exclude_mode = _qfk_log_produce(
        {
            "mode": "keywords",
            "scope": "same_record",
            "include": ["冲突"],
            "exclude": ["模拟"],
            "include_mode": "all",
            "exclude_mode": "none",
            "case_sensitive": True,
        }
    )

    with pytest.raises(ValidationError):
        validate_signals_json(invalid_scope)
    with pytest.raises(ValidationError):
        validate_signals_json(invalid_exclude_mode)


def test_multicolumn_scalar_requires_value_key_and_object_cardinality_is_closed():
    columns = [
        {"key": "USED", "selector": {"by": "index", "index": 3}, "value_mode": "string"},
        {"key": "USE_PERCENT", "selector": {"by": "index", "index": 5}, "value_mode": "number"},
    ]
    with pytest.raises(ValidationError, match="value_key"):
        validate_signals_json(
            _qfk_match(
                {
                    "type": "threshold",
                    "operator": ">",
                    "value": 80,
                    "expected": True,
                    "extract": _text_extract(columns=columns),
                }
            )
        )
    with pytest.raises(ValidationError, match="object"):
        validate_signals_json(
            _qfk_produce(
                {"name": "ROW", "type": "object", "extract": {**_text_extract(columns=columns), "cardinality": "all"}}
            )
        )
    validate_signals_json(
        _qfk_produce(
            {
                "name": "ROWS",
                "type": "array<object>",
                "extract": {**_text_extract(columns=columns), "cardinality": "all"},
            }
        )
    )


def test_data_basis_and_header_column_selection_require_a_header():
    with pytest.raises(ValidationError, match="basis=data"):
        validate_signals_json(
            _qfk_match(
                {
                    "type": "exists",
                    "expected": True,
                    "extract": _text_extract(rows={"mode": "indices", "basis": "data", "indices": [1]}),
                }
            )
        )
    with pytest.raises(ValidationError, match="表头选列"):
        validate_signals_json(
            _qfk_produce(
                {
                    "name": "VALUE",
                    "type": "string",
                    "extract": {
                        "type": "text",
                        "rows": {"mode": "all"},
                        "parser": "whitespace_table",
                        "columns": [{"key": "VALUE", "selector": {"by": "header", "name": "Value"}}],
                    },
                }
            )
        )


def test_qfk_log_signals_with_different_includes_compile_to_distinct_commands():
    """两个 qfk_log 信号即使基础参数相同，包含不同的 rows.include 时应编译为携带不同 -k 的指令。"""
    from shared.resolution.review import SignalReviewFeature, review_signal_document

    document = {
        "schema_version": 2,
        "signals": [
            {
                "id": "sig_task",
                "role": "must",
                "acquire": {"tool": "qkv_task", "args": {"keyword": "清理失败", "instruction": "查询任务"}},
                "match": None,
                "orchestrate": {
                    "phase": "diagnostic",
                    "produces": [{"name": "VM", "path": "task.vm_id"}],
                },
            },
            {
                "id": "sig_a",
                "role": "must",
                "acquire": {"tool": "qfk_log", "args": {"file": "sfvt_vtpdaemon.log", "host": "{{HOST}}"}},
                "match": {
                    "type": "exists",
                    "expected": True,
                    "extract": {
                        "type": "text",
                        "rows": {
                            "mode": "keywords",
                            "scope": "same_record",
                            "include": ["Get {{VM}} from vmlist or conf failed"],
                        },
                    },
                },
            },
            {
                "id": "sig_b",
                "role": "must",
                "acquire": {"tool": "qfk_log", "args": {"file": "sfvt_vtpdaemon.log", "host": "{{HOST}}"}},
                "match": {
                    "type": "exists",
                    "expected": True,
                    "extract": {
                        "type": "text",
                        "rows": {
                            "mode": "keywords",
                            "scope": "same_record",
                            "include": ["file is not exists, can't open file"],
                        },
                    },
                },
            },
        ],
    }

    result = review_signal_document(document, feature=SignalReviewFeature.PUBLISH)
    assert not result.blocked
    assert len(result.signals) == 3
    cmd_a = result.signals[1].command
    cmd_b = result.signals[2].command
    assert "-k" in cmd_a and "Get" in cmd_a
    assert "-k" in cmd_b and "file" in cmd_b
    assert cmd_a != cmd_b


# ─── qkv_vm_console 条件型生产者发布门禁 ───────────────────────────────


def _vm_console_document(*, external_variables: dict | None):
    """构造 Guest 内部问题 KBD：唯一生产者为 qkv_vm_console。"""

    contract: dict = {
        "schema_version": 1,
        "evidence_policy": {"must": ["s_vm_console_kernel_panic"], "should": [], "exclude": [], "context": []},
    }
    if external_variables is not None:
        contract["variables"] = external_variables
    return {
        "schema_version": 2,
        "signals": [
            {
                "id": "s_vm_console_kernel_panic",
                "role": "must",
                "acquire": {
                    "tool": "qkv_vm_console",
                    "args": {
                        "host": "{{HOST}}",
                        "vm_id": "{{VM_ID}}",
                        "capture_mode": "baseline_then_optional_wake",
                        "timeout": 60,
                        "instruction": "采集虚拟机控制台画面，确认是否存在内核恐慌或启动失败现象",
                    },
                },
                "match": None,
                "orchestrate": {
                    "requires": ["HOST", "VM_ID"],
                    "produces": [
                        {"name": "VM_CONSOLE_STATE", "path": "display_state"},
                        {"name": "VM_CONSOLE_SUMMARY", "path": "summary"},
                        {"name": "VM_CONSOLE_CONFIDENCE", "path": "confidence"},
                        {"name": "VM_CONSOLE_ARTIFACT_ID", "path": "artifact_id"},
                    ],
                },
                "provenance": {
                    "category": "frontend",
                    "method": "controlled_vm_console_capture",
                    "confidence": 0.8,
                    "risk": 0.4,
                    "needs_review": True,
                    "evidence": "受控虚拟机控制台截图与结构化视觉观察",
                },
                "review": {"require_human_confirm": False, "notes": "近黑后的 sendkey down 必须在运行时单独确认"},
            }
        ],
        "verification_contract": contract,
    }


def test_vm_console_only_kbd_is_publishable_with_declared_external_targets():
    document = _vm_console_document(
        external_variables={"HOST": {"type": "string"}, "VM_ID": {"type": "string"}}
    )

    validate_kbd_publishable_signals_json(document)


def test_vm_console_only_kbd_is_publishable_with_upstream_producers():
    document = _vm_console_document(external_variables=None)
    document["signals"].insert(
        0,
        {
            "id": "s_task_producer",
            "role": "should",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "虚拟机内核恐慌"}},
            "match": None,
            "orchestrate": {
                "produces": [{"name": "HOST", "path": "host"}, {"name": "VM_ID", "path": "vm"}],
            },
            "provenance": {
                "category": "frontend",
                "method": "task_query",
                "confidence": 0.8,
                "risk": 0.2,
                "needs_review": False,
                "evidence": "失败任务定位宿主机与 VMID",
            },
            "review": {"require_human_confirm": False, "notes": ""},
        },
    )

    validate_kbd_publishable_signals_json(document)


def test_vm_console_only_kbd_without_target_sources_is_blocked():
    document = _vm_console_document(external_variables={})

    with pytest.raises(ValidationError) as exc_info:
        validate_kbd_publishable_signals_json(document)

    # HOST/VM_ID 无来源时，变量依赖门禁或条件生产者门禁必须阻断。
    message = str(exc_info.value.message)
    assert "HOST" in message and "VM_ID" in message


def test_vm_console_free_form_fields_are_rejected_by_schema():
    document = _vm_console_document(
        external_variables={"HOST": {"type": "string"}, "VM_ID": {"type": "string"}}
    )
    document["signals"][0]["acquire"]["args"]["monitor_command"] = "screendump /tmp/x.ppm"

    with pytest.raises(ValidationError):
        validate_signals_json(document)


def test_vm_console_review_passes_with_external_targets():
    document = _vm_console_document(
        external_variables={"HOST": {"type": "string"}, "VM_ID": {"type": "string"}}
    )

    review = review_signal_document(document, feature=SignalReviewFeature.PUBLISH)
    # 占位符目标在发布期无变量上下文，resolver 报 needs_probe 属预期保留项，
    # 但不能阻断发布审查。
    assert review.status is not SignalReviewStatus.BLOCKED
