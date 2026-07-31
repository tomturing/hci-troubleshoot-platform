import pytest
from jsonschema import ValidationError
from shared.schemas.signal_output import derive_signal_requires
from shared.schemas.signal_schema import validate_signals_json


def _text_extract(*, rows=None, columns=None, value_key=None, value_mode="string"):
    extract = {"type": "text", "rows": rows or {"mode": "all"}, "cardinality": "all", "source": "stdout", "value_mode": value_mode}
    if columns:
        extract.update({"parser": "whitespace_table", "header": {"mode": "contains", "required": ["Filesystem", "Use%"]}, "columns": columns})
    if value_key:
        extract["value_key"] = value_key
    return extract


def _qfk_match(matcher, *, command="df -P /sf/log"):
    return {"schema_version": 2, "signals": [{"acquire": {"tool": "qfk_system", "args": {"command": command}}, "match": matcher, "orchestrate": {"produces": [], "requires": []}}]}


def _qfk_produce(produce):
    return {"schema_version": 2, "signals": [{"acquire": {"tool": "qfk_system", "args": {"command": "ps"}}, "match": None, "orchestrate": {"produces": [produce], "requires": []}}]}


def test_match_and_produces_reuse_the_same_value_extract_contract():
    extract = _text_extract(
        rows={"mode": "keywords", "include": ["{{MOUNT}}"], "exclude": [], "include_mode": "all", "case_sensitive": True},
        columns=[{"key": "USE_PERCENT", "selector": {"by": "header", "name": "Use%"}, "value_mode": "number"}],
        value_key="USE_PERCENT",
        value_mode="number",
    )
    matcher = {"type": "threshold", "operator": ">", "value": 80, "expected": True, "extract": extract}
    match_doc = _qfk_match(matcher)
    validate_signals_json(match_doc)
    assert derive_signal_requires(match_doc["signals"][0]) == ["MOUNT"]
    validate_signals_json(_qfk_produce({"name": "USE_PERCENT", "type": "number", "extract": extract}))


def test_matcher_extract_is_required_and_old_json_path_matcher_is_rejected():
    with pytest.raises(ValidationError, match="extract"):
        validate_signals_json(_qfk_match({"type": "exists", "expected": True}))
    with pytest.raises(ValidationError):
        validate_signals_json(_qfk_match({"type": "json_path", "path": "status", "expected": True, "extract": {"type": "json", "path": "status"}}))


@pytest.mark.parametrize("extract", [
    {"type": "text", "include": ["old"]},
    {"type": "text", "rows": {"mode": "all"}, "column": 2},
    {"type": "text", "rows": {"mode": "all"}, "column_mode": "index"},
])
def test_old_single_column_text_extract_fields_are_rejected(extract):
    with pytest.raises(ValidationError):
        validate_signals_json(_qfk_produce({"name": "VALUE", "type": "string", "extract": extract}))


def test_qfk_produces_path_is_rejected_but_json_extract_is_allowed():
    with pytest.raises(ValidationError, match="新版 extract"):
        validate_signals_json(_qfk_produce({"name": "PID", "type": "integer", "path": "data.0.pid"}))
    validate_signals_json(_qfk_produce({"name": "PID", "type": "integer", "extract": {"type": "json", "path": "data[0].pid", "cardinality": "exactly_one", "source": "stdout", "value_mode": "integer"}}))


def test_multicolumn_scalar_requires_value_key_and_object_cardinality_is_closed():
    columns = [
        {"key": "USED", "selector": {"by": "index", "index": 3}, "value_mode": "string"},
        {"key": "USE_PERCENT", "selector": {"by": "index", "index": 5}, "value_mode": "number"},
    ]
    with pytest.raises(ValidationError, match="value_key"):
        validate_signals_json(_qfk_match({"type": "threshold", "operator": ">", "value": 80, "expected": True, "extract": _text_extract(columns=columns)}))
    with pytest.raises(ValidationError, match="object"):
        validate_signals_json(_qfk_produce({"name": "ROW", "type": "object", "extract": {**_text_extract(columns=columns), "cardinality": "all"}}))
    validate_signals_json(_qfk_produce({"name": "ROWS", "type": "array<object>", "extract": {**_text_extract(columns=columns), "cardinality": "all"}}))


def test_data_basis_and_header_column_selection_require_a_header():
    with pytest.raises(ValidationError, match="basis=data"):
        validate_signals_json(_qfk_match({"type": "exists", "expected": True, "extract": _text_extract(rows={"mode": "indices", "basis": "data", "indices": [1]})}))
    with pytest.raises(ValidationError, match="表头选列"):
        validate_signals_json(_qfk_produce({"name": "VALUE", "type": "string", "extract": {"type": "text", "rows": {"mode": "all"}, "parser": "whitespace_table", "columns": [{"key": "VALUE", "selector": {"by": "header", "name": "Value"}}]}}))
