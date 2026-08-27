"""shared.signals.ai_extractor 单元测试。"""

import json
from types import SimpleNamespace

import pytest
from shared.signals.ai_extractor import (
    AIExtractionResult,
    ai_value_type_for_matcher,
    extract_ai_value,
    has_ai_extract,
)
from shared.signals.extractor import QFKExtractionError


class _FakeAIClient:
    def __init__(self, payload):
        self.payload = payload
        self.calls = []

    async def invoke(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(content=json.dumps(self.payload, ensure_ascii=False))


@pytest.fixture(autouse=True)
def mock_prompt_loader(monkeypatch):
    async def load_prompt(_factory, *, mode, output_type, **kwargs):
        return (
            f"Mode: {mode}, Output: {output_type}",
            "rev-1",
        )

    monkeypatch.setattr("shared.signals.ai_extractor._load_ai_processing_system_prompt", load_prompt)


@pytest.mark.asyncio
async def test_shared_ai_extract_success_with_consumer_and_signal_type():
    client = _FakeAIClient(
        {
            "status": "success",
            "output": "192.168.1.100",
            "evidence": [{"ref": "line:2", "quote": "eth0 inet 192.168.1.100/24"}],
            "reason": "成功提取 IP",
        }
    )
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_processing": {"instruction": "提取 IP", "output_type": "string"},
    }
    output = "lo inet 127.0.0.1\neth0 inet 192.168.1.100/24\n"

    result = await extract_ai_value(
        output,
        spec,
        "string",
        client,
        consumer="agent-service.qkv.ai_processing",
        signal_type="qkv",
        conversation_id="conv-test",
        case_id="case-test",
    )

    assert isinstance(result, AIExtractionResult)
    assert result.value == "192.168.1.100"
    assert result.evidence_line_numbers == [2]
    assert len(client.calls) == 1
    prompt_payload = json.loads(client.calls[0]["messages"][1]["content"])
    assert prompt_payload["output_type"] == "string"
    assert prompt_payload["mode"] == "extract"


@pytest.mark.asyncio
async def test_shared_ai_extract_rejects_ungrounded_output():
    client = _FakeAIClient(
        {
            "status": "success",
            "output": "10.0.0.1",
            "evidence": [{"ref": "line:1", "quote": "eth0 inet 192.168.1.100/24"}],
            "reason": "错误提取",
        }
    )
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_processing": {"instruction": "提取 IP", "output_type": "string"},
    }

    with pytest.raises(QFKExtractionError, match="QFK_AI_PROCESSING_UNGROUNDED"):
        await extract_ai_value("eth0 inet 192.168.1.100/24\n", spec, "string", client)


def test_shared_ai_helpers():
    assert has_ai_extract({"ai_processing": {"instruction": "test"}}) is True
    assert has_ai_extract({"type": "text"}) is False
    assert ai_value_type_for_matcher("threshold") == "array<number>"
    assert ai_value_type_for_matcher("keyword") is None
