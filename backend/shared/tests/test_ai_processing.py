import pytest
from shared.signals.ai_processing import validate_ai_response


def _config(mode="extract", output_type="string", **extra):
    return {"contract_version": 1, "mode": mode, "instruction": "按说明处理", "output_type": output_type, **extra}


def test_extract_output_must_be_grounded_by_platform_caller_contract():
    # 统一响应层验证结构和证据；原文逐字回查由 QFK 适配器在候选上下文中完成。
    result = validate_ai_response(
        {"status": "success", "output": "node-1", "evidence": [{"ref": "line:1", "quote": "host=node-1"}], "reason": "识别主机"},
        _config(),
        {"line:1": "host=node-1"},
        "string",
    )
    assert result.output == "node-1"


def test_derive_accepts_computed_numeric_array():
    result = validate_ai_response(
        {"status": "success", "output": [4, 8], "evidence": [{"ref": "line:1", "quote": "t=11:00"}], "reason": "转换为秒"},
        _config("derive", "array", item_type="number"),
        {"line:1": "t=11:00"},
        "array<number>",
    )
    assert result.output == [4, 8]


def test_response_rejects_unknown_fields_and_untrusted_evidence():
    with pytest.raises(ValueError, match="只包含"):
        validate_ai_response(
            {"status": "success", "output": "x", "evidence": [{"ref": "line:1", "quote": "x"}], "reason": "ok", "debug": 1},
            _config(),
            {"line:1": "x"},
            "string",
        )
    with pytest.raises(ValueError, match="不存在"):
        validate_ai_response(
            {"status": "success", "output": "x", "evidence": [{"ref": "line:9", "quote": "x"}], "reason": "ok"},
            _config(),
            {"line:1": "x"},
            "string",
        )
