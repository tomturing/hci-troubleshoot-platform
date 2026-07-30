"""数据库 QKV/QFK 可读投影不得继续传播旧字段或无效 aCLI 示例。"""

from __future__ import annotations

import json
import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_PATH = REPO_ROOT / "database" / "seeds" / "03_qkv_qfk_tools.sql"


def _tool_block(tool: str) -> str:
    text = SEED_PATH.read_text(encoding="utf-8")
    match = re.search(
        rf"\(\s*'{tool}'(?P<body>.*?)\)\s*ON CONFLICT \(tool_name\)",
        text,
        re.DOTALL,
    )
    assert match, f"缺少 {tool} seed"
    return match.group(0)


def _json_literals(block: str) -> list[object]:
    payloads = []
    for literal in re.findall(r"'(?:''|[^'])*'", block, re.DOTALL):
        value = literal[1:-1].replace("''", "'").strip()
        if value.startswith(("{", "[")):
            payloads.append(json.loads(value))
    return payloads


def test_all_qkv_qfk_seed_json_payloads_are_valid():
    tools = [
        "qkv_alert",
        "qkv_task",
        "qkv_dialog",
        "qfk_log",
        "qfk_service",
        "qfk_system",
        "qfk_vm",
        "qfk_network",
        "qfk_storage",
        "qfk_hardware",
        "qfk_platform",
    ]

    for tool in tools:
        schema, examples = _json_literals(_tool_block(tool))
        assert isinstance(schema, dict)
        assert isinstance(examples, list)


def test_qfk_seed_projection_uses_v2_command_host_and_service_fields():
    for tool in (
        "qfk_system",
        "qfk_vm",
        "qfk_network",
        "qfk_storage",
        "qfk_hardware",
        "qfk_platform",
    ):
        schema, _ = _json_literals(_tool_block(tool))
        properties = schema["properties"]
        assert "command" in properties
        assert "host" in properties
        assert "sub_command" not in properties
        assert "node_ip" not in properties
        matcher_properties = properties["matcher"]["properties"]
        assert {"delta", "trend"}.issubset(matcher_properties["type"]["enum"])
        assert matcher_properties["mode"]["enum"] == ["or", "and", "not"]

    service_schema, _ = _json_literals(_tool_block("qfk_service"))
    service_properties = service_schema["properties"]
    assert service_properties["container"]["enum"] == ["asv", "anet", "host"]
    assert "resource_keyword" in service_properties
    assert "command" in service_properties
    assert "host" in service_properties
    assert "service_name" not in service_properties


def test_qkv_seed_projection_does_not_put_transport_node_in_signal_args():
    alert_schema, _ = _json_literals(_tool_block("qkv_alert"))
    task_schema, _ = _json_literals(_tool_block("qkv_task"))

    for schema in (alert_schema, task_schema):
        properties = schema["properties"]
        assert "node_ip" not in properties
        assert "timeout" in properties
        assert "instruction" in properties
        # produces 是工具级默认编排投影，KBD 保存时仍进入 orchestrate.produces。
        assert "produces" in properties


def test_dialog_is_executable_composite_and_legacy_lines_shortcut_is_absent():
    dialog_block = _tool_block("qkv_dialog")
    _, examples = _json_literals(dialog_block)

    assert examples
    assert "REQUEST_ID" in dialog_block
    assert "\n    true\n" in dialog_block
    assert "--lines" not in SEED_PATH.read_text(encoding="utf-8")
