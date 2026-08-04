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
            "ok": True,
            "value": "192.168.100.55",
            "evidence_lines": [2],
        }
    )
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "ai_extract": {"instruction": "提取其中的第一个 IP 地址"},
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
    assert prompt["candidate_lines"] == [{"line": 2, "text": "检测到IP，发生冲突，ip=192.168.100.55"}]
    assert client.calls[0]["response_format"] == {"type": "json_object"}


@pytest.mark.asyncio
async def test_ai_extract_rejects_value_not_present_in_cited_complete_line():
    client = _FakeAIClient({"ok": True, "value": "192.168.100.99", "evidence_lines": [1]})
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_extract": {"instruction": "提取 IP"},
    }

    with pytest.raises(QFKExtractionError, match="QFK_AI_EXTRACT_UNGROUNDED"):
        await extract_ai_value("检测到IP，冲突，ip=192.168.100.55\n", spec, "string", client)


@pytest.mark.asyncio
async def test_ai_extract_grounding_uses_raw_literal_before_number_normalization():
    client = _FakeAIClient({"ok": True, "value": "54%", "evidence_lines": [1]})
    spec = {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "ai_extract": {"instruction": "提取磁盘使用率"},
    }

    result = await extract_ai_value("磁盘使用率为 54%\n", spec, "number", client)

    assert result.value == 54.0
