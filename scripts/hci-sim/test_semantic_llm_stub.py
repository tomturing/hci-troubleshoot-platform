import importlib.util
import json
from pathlib import Path

import pytest

MODULE_PATH = Path(__file__).with_name("semantic_llm_stub.py")
SPEC = importlib.util.spec_from_file_location("semantic_llm_stub", MODULE_PATH)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _request(instruction="仅从筛选后的 Use% 列提取百分比数值。"):
    contract = {
        "instruction": instruction,
        "mode": "extract",
        "output_type": "array",
        "candidates": [
            {
                "ref": "line:2",
                "content": "/dev/sample 100000 83000 17000 83% /sf/log",
            }
        ],
    }
    return {"messages": [{"role": "user", "content": json.dumps(contract, ensure_ascii=False)}]}


def test_build_completion_extracts_grounded_percentage_only():
    response = MODULE.build_completion(_request())
    payload = json.loads(response["choices"][0]["message"]["content"])

    assert payload["status"] == "success"
    assert payload["output"] == [83]
    assert payload["evidence"] == [
        {"ref": "line:2", "quote": "/dev/sample 100000 83000 17000 83% /sf/log"}
    ]


def test_build_completion_rejects_unimplemented_ai_contract():
    with pytest.raises(ValueError, match="仅支持 Signal v2"):
        MODULE.build_completion(_request("总结所有异常"))


def test_build_completion_returns_bounded_vm_console_observation():
    request = {
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "你是虚拟机控制台画面观察器。"},
                    {"type": "image_url", "image_url": {"url": "data:image/png;base64,AA=="}},
                ],
            }
        ]
    }

    response = MODULE.build_completion(request)
    payload = json.loads(response["choices"][0]["message"]["content"])

    assert payload["display_state"] == "normal"
    assert payload["confidence"] == 0.99
