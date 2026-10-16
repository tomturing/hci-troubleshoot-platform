"""硬盘坏道对抗 LLM 桩的纯逻辑单测（无需起服务/docker）。"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))

_STUB_PATH = ROOT / "scripts" / "hci-sim" / "disk_badtrack_llm_stub.py"
_spec = importlib.util.spec_from_file_location("disk_badtrack_llm_stub", _STUB_PATH)
stub = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(stub)  # type: ignore[union-attr]

# 统一输出约束里必然出现的标记词
_REASONING_SYSTEM = "严格按此结构输出：user_questions 列表、next_state 阶段。"


def _reasoning_request() -> dict:
    return {
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": _REASONING_SYSTEM},
            {"role": "user", "content": "已知变量 disk_dev=/dev/sde，请生成下一轮推理。"},
        ],
    }


def test_is_reasoning_output_request_positive_and_negative():
    assert stub.is_reasoning_output_request(_reasoning_request()) is True
    # 非 json_object → 不是结构化生成合同
    assert stub.is_reasoning_output_request({"messages": _reasoning_request()["messages"]}) is False
    # 缺输出约束标记 → 判否
    bad = {"response_format": {"type": "json_object"}, "messages": [{"role": "user", "content": "随便聊聊"}]}
    assert stub.is_reasoning_output_request(bad) is False


def test_build_completion_injects_adversarial_and_valid_reasoning_output():
    completion = stub.build_completion(_reasoning_request())
    assert completion["object"] == "chat.completion"
    content = json.loads(completion["choices"][0]["message"]["content"])
    # 注入的正是历史上被误判的两类坏提问
    joined = "".join(content["user_questions"])
    assert "disk_dev" in joined and "disk_sn" in joined and "disk_type" in joined
    assert content["next_state"] == "S2"


def test_build_completion_reasoning_output_parses_as_real_schema():
    # 关键回归：桩产出的 JSON 必须能被真实 ReasoningOutput schema 解析，
    # 否则 E2E 会误判为「模型没按契约返回」而非驱动到过滤守卫。
    from shared.models.reliability import ReasoningOutput

    completion = stub.build_completion(_reasoning_request())
    parsed = ReasoningOutput.model_validate_json(completion["choices"][0]["message"]["content"])
    assert parsed.next_state == "S2"
    assert parsed.user_questions  # 非空，交由引擎过滤


def test_build_completion_rejects_unsupported_contract():
    try:
        stub.build_completion({"messages": [{"role": "user", "content": "hi"}]})
    except ValueError:
        return
    raise AssertionError("非受支持合同应抛 ValueError")
