"""在线/离线诊断 KBD 样例种子的通用契约门禁。"""

from __future__ import annotations

import json
import re
from pathlib import Path

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


def test_sample_seed_only_creates_drafts_and_only_upgrades_unreviewed_old_samples():
    sql = SEED_PATH.read_text(encoding="utf-8")

    assert "INSERT INTO kbd_entry" in sql
    assert "'sample_suite', 'diagnosis-signal-matrix-v1'" in sql
    assert "'draft'" in sql
    assert "ON CONFLICT (support_id) DO UPDATE SET" in sql
    assert "WHERE kbd_entry.status = 'draft'" in sql
    assert "COALESCE((kbd_entry.metadata ->> 'seed_version')::integer, 0) < 4" in sql
    assert "dynamic_resource" not in sql
    assert "collector_definition" not in sql
    assert "collection_profile" not in sql
    assert "signal_mapping" not in sql


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
    assert all(signal.get("provenance") and signal.get("review") for signal in signals)
    assert all(document.get("verification_contract") for document in documents)


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
        assert all(item.command for item in result.signals)


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
