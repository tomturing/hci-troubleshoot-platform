from app.tools.qfk.matcher import evaluate_matcher


def _rows_all(*, value_mode="string") -> dict:
    return {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout", "value_mode": value_mode}


def test_keyword_regex_state_and_exists_consume_extract_before_matching():
    keyword = evaluate_matcher({"type": "keyword", "pattern": "needle", "mode": "or", "expected": True, "extract": {"type": "text", "rows": {"mode": "keywords", "include": ["keep"], "exclude": [], "include_mode": "all", "case_sensitive": True}, "cardinality": "all"}}, "skip needle\nkeep needle\n")
    assert keyword.matched is True
    regex = evaluate_matcher({"type": "regex", "pattern": r"^ready$", "expected": True, "extract": _rows_all()}, "ready\n")
    assert regex.matched is True
    state = evaluate_matcher({"type": "state", "pattern": "running", "expected": True, "extract": {"type": "text", "rows": {"mode": "indices", "basis": "physical", "indices": [2]}, "cardinality": "exactly_one"}}, "header\nrunning\n")
    assert state.matched is True
    exists = evaluate_matcher({"type": "exists", "expected": True, "extract": {"type": "text", "rows": {"mode": "keywords", "include": ["missing"], "exclude": [], "include_mode": "all", "case_sensitive": True}}}, "present\n")
    assert exists.matched is False


def test_threshold_delta_and_trend_use_explicit_numeric_column():
    extract = {
        "type": "text", "parser": "whitespace_table", "header": {"mode": "contains", "required": ["Use%"]},
        "rows": {"mode": "all"}, "columns": [{"key": "USE_PERCENT", "selector": {"by": "header", "name": "Use%"}, "value_mode": "number"}],
        "value_key": "USE_PERCENT", "cardinality": "all",
    }
    output = "Filesystem Use%\n/root 35%\n/sf/log 83%\n"
    threshold = evaluate_matcher({"type": "threshold", "aggregation": "max", "operator": ">=", "value": 80, "expected": True, "extract": extract}, output)
    assert threshold.matched is True
    delta = evaluate_matcher({"type": "delta", "operator": ">", "value": 40, "expected": True, "extract": extract}, output)
    assert delta.matched is True
    trend = evaluate_matcher({"type": "trend", "direction": "increasing", "value": 1, "minimum_samples": 2, "expected": True, "extract": extract}, output)
    assert trend.matched is True


def test_json_path_is_an_extract_option_not_a_matcher_type():
    matcher = {"type": "state", "pattern": "healthy", "expected": True, "extract": {"type": "json", "path": "data[0].status", "cardinality": "exactly_one", "source": "stdout", "value_mode": "string"}}
    assert evaluate_matcher(matcher, '{"data":[{"status":"healthy"}]}').matched is True
    assert evaluate_matcher({"type": "json_path", "expected": True, "extract": matcher["extract"]}, "{}").matched is None


def test_matcher_without_extract_fails_closed():
    result = evaluate_matcher({"type": "threshold", "operator": ">", "value": 1, "expected": True}, "value=2")
    assert result.matched is None
    assert "extract" in result.detail["error"]
