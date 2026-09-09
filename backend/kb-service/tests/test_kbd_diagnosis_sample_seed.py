"""在线/离线诊断 KBD 样例种子的通用契约门禁。"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest
from jsonschema import ValidationError
from shared.resolution.review import SignalReviewFeature, SignalReviewStatus, review_signal_document
from shared.schemas.acquirer_args import ACQUIRER_ARGS_SCHEMA, BACKEND_TOOLS, FRONTEND_TOOLS
from shared.schemas.kbd_signal_safety import validate_kbd_read_only_signals_json
from shared.schemas.signal_schema import validate_kbd_publishable_signals_json

REPO_ROOT = Path(__file__).resolve().parents[3]
SEED_PATH = REPO_ROOT / "database" / "seeds" / "04_kbd_diagnosis_samples.sql"
ACLI_CATALOG_PATH = REPO_ROOT / "backend" / "shared" / "resolution" / "catalogs" / "acli_command_catalog.json"
SAMPLE_IDS = {
    "SAMPLE-SIG-VM",
    "SAMPLE-SIG-CORE",
    "SAMPLE-SIG-LOG",
    "SAMPLE-SIG-NET-STO",
    "SAMPLE-SIG-HW-PLT",
}


def _sample_documents() -> list[dict]:
    sql = SEED_PATH.read_text(encoding="utf-8")
    payloads = re.findall(r"\$signals\$\s*(\{.*?\})\s*\$signals\$::jsonb", sql, re.DOTALL)
    return [json.loads(payload) for payload in payloads]


def test_sample_seed_preserves_published_baseline_and_creates_semantic_entry_v2_drafts():
    sql = SEED_PATH.read_text(encoding="utf-8")

    assert "INSERT INTO kbd_entry" in sql
    assert "'diagnosis-signal-matrix-v1'::text AS target_sample_suite" in sql
    assert "'kbd-semantic-entry-signal-v2'" in sql
    assert "语义入口 Signal v2 样例" in sql
    assert "【语义入口兜底样例】" in sql
    assert "sample_entry_mode" in sql
    assert "semantic_fallback" in sql
    assert "移除强生产者后仍被" in sql
    for variable in ("HOST", "VM_ID", "REQUEST_ID"):
        assert f"'{variable}'" in sql
    assert "'SAMPLE-V2-'" in sql
    assert "'SAMPLE-SEM-V2-'" not in sql
    assert "'draft'" in sql
    assert "ON CONFLICT (support_id) DO UPDATE SET" in sql
    assert "WHERE kbd_entry.status = 'draft'" in sql
    assert "EXCLUDED.metadata ->> 'sample_suite'" in sql
    assert "EXCLUDED.metadata ->> 'seed_version'" in sql
    assert "dynamic_resource" not in sql
    assert "collector_definition" not in sql
    assert "collection_profile" not in sql
    assert "signal_mapping" not in sql


def test_semantic_entry_v2_seed_keeps_only_case_context_as_producer():
    """V2 语义入口样例不能意外继承基线样例中的强生产者信号。"""

    sql = SEED_PATH.read_text(encoding="utf-8")
    start = sql.index("semantic_entry_rows AS (")
    end = sql.index("seed_rows AS (", start)
    semantic_cte = sql[start:end]

    assert "'qkv_case_context'" in semantic_cte
    for tool in ("qkv_task", "qkv_alert", "qkv_dialog", "qkv_vm_console", "qkv_effect"):
        assert f"'{tool}'" in semantic_cte
    assert "NOT IN" in semantic_cte
    assert "semantic_entry_profile" in semantic_cte


def test_sample_documents_cover_every_signal_tool_and_every_registered_argument():
    documents = _sample_documents()
    assert documents

    observed_args = {tool: set() for tool in ACQUIRER_ARGS_SCHEMA}
    observed_tools: set[str] = set()
    for document in documents:
        for signal in document["signals"]:
            tool = signal["acquire"]["tool"]
            observed_tools.add(tool)
            observed_args[tool].update(signal["acquire"]["args"])

    assert observed_tools == set(ACQUIRER_ARGS_SCHEMA)
    assert observed_tools & FRONTEND_TOOLS == FRONTEND_TOOLS
    assert observed_tools & BACKEND_TOOLS == BACKEND_TOOLS
    for tool, schema in ACQUIRER_ARGS_SCHEMA.items():
        assert set(schema["properties"]) <= observed_args[tool], f"{tool} 缺少参数覆盖"


def test_sample_documents_cover_signal_v2_processing_and_evidence_contracts():
    documents = _sample_documents()
    signals = [signal for document in documents for signal in document["signals"]]
    matchers = [signal["match"] for signal in signals if isinstance(signal.get("match"), dict)]
    produces = [output for signal in signals for output in (signal.get("orchestrate") or {}).get("produces") or []]

    assert {matcher["type"] for matcher in matchers} == {
        "keyword",
        "regex",
        "state",
        "boolean",
        "threshold",
        "delta",
        "trend",
        "exists",
    }
    assert {signal["role"] for signal in signals} == {"must", "should", "exclude", "context"}
    assert {matcher["extract"]["type"] for matcher in matchers} == {"text", "json"}
    assert {output.get("extract", {}).get("type") for output in produces if output.get("extract")} == {
        "text",
        "json",
    }
    assert any("path" in output for output in produces)
    assert any(len((signal.get("orchestrate") or {}).get("produces") or []) > 1 for signal in signals)
    assert any("columns" in matcher["extract"] for matcher in matchers)
    assert any("ai_extract" in matcher["extract"] for matcher in matchers)
    assert any((signal.get("orchestrate") or {}).get("output_processing") for signal in signals)
    assert any(
        unit.get("mode") == "assert" and (unit.get("match") or {}).get("type") == "exists"
        for signal in signals
        for unit in (signal.get("orchestrate") or {}).get("output_processing") or []
        if isinstance(unit, dict)
    )
    assert all(signal.get("provenance") and signal.get("review") for signal in signals)
    assert all(document.get("verification_contract") for document in documents)
    semantic_documents = [document for document in documents if document.get("semantic_entry_profile")]
    assert semantic_documents
    assert any(
        signal["acquire"]["tool"] == "qkv_case_context"
        for document in semantic_documents for signal in document["signals"]
    )


def test_sample_documents_are_publishable_read_only_and_runtime_compilable():
    documents = _sample_documents()
    assert {item["verification_contract"]["case_id"] for item in documents} == SAMPLE_IDS
    assert ACLI_CATALOG_PATH.is_file(), "shared 必须提供发布审查和运行时共用的 aCLI Catalog"
    for document in documents:
        tools = {signal["acquire"]["tool"] for signal in document["signals"]}
        assert tools & FRONTEND_TOOLS, f"{document['verification_contract']['case_id']} 缺少生产者信号"
        assert tools & BACKEND_TOOLS, f"{document['verification_contract']['case_id']} 缺少消费者信号"
        validate_kbd_publishable_signals_json(document)
        validate_kbd_read_only_signals_json(document)
        result = review_signal_document(document, feature=SignalReviewFeature.PUBLISH)
        assert result.status in {SignalReviewStatus.PASSED, SignalReviewStatus.NEEDS_REVIEW}
        assert not result.blocked
        # 条件型生产者编译为不可变意图（Capture Intent / Verification Intent），
        # 刻意不产出命令字符串；其余信号仍必须编译出可执行命令。
        assert all(
            item.command
            for item in result.signals
            if item.tool not in {"qkv_vm_console", "qkv_effect", "qkv_case_context"}
        )


def test_sample_documents_compile_in_online_agent_runtime():
    """五篇样例必须通过在线 Agent 的 CDD 计划与真实命令编译门禁。"""

    agent_test = REPO_ROOT / "backend" / "agent-service" / "tests" / "unit" / "test_diagnosis_sample_contracts.py"
    assert agent_test.is_file()


def test_sample_documents_compile_in_offline_sync_runtime():
    """离线回归必须作为服务启动前门禁存在。"""

    diagnosis_test = (
        REPO_ROOT / "backend" / "diagnosis-service" / "tests" / "unit" / "test_diagnosis_sample_contracts.py"
    )
    assert diagnosis_test.is_file()


def test_effect_usage_and_phase_are_consistent_in_samples_and_publish_gate():
    documents = _sample_documents()
    vm_document = next(
        document for document in documents
        if document["verification_contract"]["case_id"] == "SAMPLE-SIG-VM"
    )
    effect = next(
        signal for signal in vm_document["signals"]
        if signal["acquire"]["tool"] == "qkv_effect"
    )
    assert effect["acquire"]["args"]["usage"] == "remediation_verify"
    assert effect["orchestrate"]["phase"] == "remediation"
    effect["orchestrate"]["phase"] = "diagnostic"

    with pytest.raises(ValidationError, match="remediation phase"):
        validate_kbd_publishable_signals_json(vm_document)
