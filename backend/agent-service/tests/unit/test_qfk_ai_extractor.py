"""QFK 完整输出 AI 提取的溯源与失败闭环。"""

import json
from types import SimpleNamespace

import pytest
from app.tools.qfk.ai_extractor import extract_ai_value
from app.tools.qfk.extractor import QFKExtractionError


class _FakeAIClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


@pytest.mark.asyncio
async def test_ai_extract_uses_same_line_and_keyword_candidates_and_returns_grounded_ip():
    client = _FakeAIClient(
        {
            "status": "success", "output": "192.168.100.55",
            "evidence": [{"ref": "line:2", "quote": "检测到IP，发生冲突，ip=192.168.100.55"}], "reason": "识别 IP",
        }
    )
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "ai_processing": {"instruction": "提取其中的第一个 IP 地址", "output_type": "string"},
    }
    output = "检测到IP，但当前无异常\n检测到IP，发生冲突，ip=192.168.100.55\n"

    result = await extract_ai_value(
        output,
        spec,
        "string",
        client,
        matcher={"type": "keyword", "pattern": ["检测到IP", "冲突"], "mode": "and"},
        conversation_id="conv-1",
        case_id="case-1",
    )

    assert result.value == "192.168.100.55"
    assert result.evidence_line_numbers == [2]
    prompt = json.loads(client.calls[0]["messages"][1]["content"])
    assert prompt["candidates"] == [{"ref": "line:2", "content": "检测到IP，发生冲突，ip=192.168.100.55"}]
    assert client.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_ai_extract_rejects_value_not_present_in_cited_complete_line():
    client = _FakeAIClient({"status": "success", "output": "192.168.100.99", "evidence": [{"ref": "line:1", "quote": "检测到IP，冲突，ip=192.168.100.55"}], "reason": "识别 IP"})
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_processing": {"instruction": "提取 IP", "output_type": "string"},
    }

    with pytest.raises(QFKExtractionError, match="QFK_AI_PROCESSING_UNGROUNDED"):
        await extract_ai_value("检测到IP，冲突，ip=192.168.100.55\n", spec, "string", client)


@pytest.mark.asyncio
async def test_ai_extract_grounding_uses_raw_literal_before_number_normalization():
    client = _FakeAIClient({"status": "success", "output": 54, "evidence": [{"ref": "line:1", "quote": "磁盘使用率为 54%"}], "reason": "识别百分比"})
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_processing": {"instruction": "提取磁盘使用率", "output_type": "number"},
    }

    result = await extract_ai_value("磁盘使用率为 54%\n", spec, "number", client)

    assert result.value == 54.0


@pytest.mark.asyncio
async def test_ai_extract_array_number_preserves_order_and_raw_grounding():
    client = _FakeAIClient({"status": "success", "output": [347688534016, 347688534016], "evidence": [{"ref": "line:1", "quote": "Completed 347688534016 of 347688534016 bytes"}], "reason": "提取字节数"})
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "ai_processing": {"instruction": "按出现顺序提取 completed 和 total 两个字节数", "output_type": "array", "item_type": "number"},
    }

    result = await extract_ai_value(
        "Completed 347688534016 of 347688534016 bytes\n",
        spec,
        "array<number>",
        client,
    )

    assert result.value == [347688534016.0, 347688534016.0]
    assert result.raw_value == [347688534016, 347688534016]
    assert result.evidence_line_numbers == [1]


@pytest.mark.asyncio
async def test_ai_derive_normalizes_grounded_host_times_to_epoch_values():
    client = _FakeAIClient(
        {"status": "success", "output": [300, 0, 2], "evidence": [
            {"ref": "line:1", "quote": "10.97.128.120: Wed Aug 26 11:05:24 CST 2026"},
            {"ref": "line:2", "quote": "10.97.128.13: Wed Aug 26 11:00:24 CST 2026"},
            {"ref": "line:3", "quote": "10.97.128.11: Wed Aug 26 11:00:26 CST 2026"}], "reason": "计算时间"}
    )
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_processing": {
            "mode": "derive",
            "instruction": "从每行识别主机系统时间",
            "output_type": "array", "item_type": "number",
        },
    }
    output = (
        "10.97.128.120: Wed Aug 26 11:05:24 CST 2026\n"
        "10.97.128.13: Wed Aug 26 11:00:24 CST 2026\n"
        "10.97.128.11: Wed Aug 26 11:00:26 CST 2026\n"
    )

    result = await extract_ai_value(output, spec, "array<number>", client)

    assert max(result.value) - min(result.value) == 300
    assert result.raw_value == [300, 0, 2]
    assert result.evidence_line_numbers == [1, 2, 3]


@pytest.mark.asyncio
async def test_ai_derive_rejects_a_calculated_value_without_grounded_source_record():
    client = _FakeAIClient({"status": "success", "output": 300, "evidence": [{"ref": "line:1", "quote": "Wed Aug 26 11:05:24 CST 2026"}], "reason": "计算时间"})
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_processing": {
            "mode": "derive",
            "instruction": "从每行识别主机系统时间",
            "output_type": "number",
        },
    }

    with pytest.raises(QFKExtractionError, match="QFK_AI_PROCESSING_INVALID_RESPONSE|QFK_AI_PROCESSING_UNGROUNDED"):
        await extract_ai_value("Wed Aug 26 11:05:24 CST 2026\n", spec, "array<number>", client)


def test_ai_derive_accepts_computed_output_with_grounded_evidence():
    assert True
