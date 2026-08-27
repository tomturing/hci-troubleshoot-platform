"""Signal 试运行的领域层测试。"""

from __future__ import annotations

import hashlib
import json
from types import SimpleNamespace

import pytest
from app.routes.signal_dry_run import SignalDryRunRequest, evaluate_signal_dry_run


def _revision(signal: dict) -> str:
    payload = json.dumps(signal, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _qfk_signal() -> dict:
    return {
        "id": "sig_qfk_001",
        "acquire": {"tool": "qfk_system", "args": {"instruction": "检查失败状态"}},
        "match": {
            "type": "keyword",
            "pattern": "FAILED",
            "mode": "or",
            "expected": True,
            "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
        },
        "orchestrate": {},
    }


@pytest.mark.asyncio
async def test_qfk_dry_run_uses_shared_matcher_without_execution() -> None:
    signal = _qfk_signal()
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qfk_execution_result",
        unit_ref={"signal_id": signal["id"]}, verification_scope="signal",
        dataset={"dataset_id": "preview-1", "source_type": "pasted", "source_ref": "user-input", "payload": "task status: FAILED\n"},
        signal=signal, support_id="41398", kbd_revision=7,
    )

    result = await evaluate_signal_dry_run(request, ai_client=None, trace_id="a" * 32)

    assert result.status == "PASS"
    assert result.trace_id == "a" * 32
    assert result.matcher and result.matcher["matched_keywords"] == ["FAILED"]
    assert "未写入生产变量池" not in result.evidence


@pytest.mark.asyncio
async def test_qfk_dry_run_rejects_changed_draft() -> None:
    signal = _qfk_signal()
    request = SignalDryRunRequest(
        draft_revision="sha256:stale", scope="qfk_execution_result",
        unit_ref={"signal_id": signal["id"]},
        dataset={"dataset_id": "preview-1", "source_type": "pasted", "source_ref": "user-input", "payload": "FAILED"},
        signal=signal, support_id="41398", kbd_revision=7,
    )

    with pytest.raises(ValueError, match="DRAFT_REVISION_MISMATCH"):
        await evaluate_signal_dry_run(request, ai_client=None, trace_id="a" * 32)


@pytest.mark.asyncio
async def test_qkv_dry_run_only_runs_target_and_preceding_units() -> None:
    signal = {
        "id": "sig_qkv_001",
        "acquire": {"tool": "qkv_task", "args": {}},
        "orchestrate": {
            "output_processing": [
                {"mode": "derive", "input": "{{description}}", "name": "NAME", "type": "string", "extract": {"type": "feature", "feature": "vm_name", "cardinality": "exactly_one"}},
                {"mode": "assert", "input": "{{NAME}}", "match": {"type": "keyword", "pattern": "vm-01", "mode": "or", "expected": True, "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"}}},
                {"mode": "derive", "input": "{{missing}}", "name": "MUST_NOT_RUN", "type": "string", "extract": {"type": "feature", "feature": "host", "cardinality": "exactly_one"}},
            ]
        },
    }
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qkv_variable_processing",
        unit_ref={"signal_id": signal["id"], "processing_index": 1}, verification_scope="signal",
        dataset={"dataset_id": "preview-qkv", "source_type": "pasted", "source_ref": "user-input", "payload": [{"description": "虚拟机名称: vm-01"}]},
        signal=signal, support_id="41398", kbd_revision=7,
    )

    result = await evaluate_signal_dry_run(request, ai_client=None, trace_id="b" * 32)

    assert result.status == "PASS"
    assert result.value == [{"description": "虚拟机名称: vm-01", "name": "vm-01"}]
    assert result.derivation["processing_end_index"] == 1


@pytest.mark.asyncio
async def test_ai_step_requires_an_explicit_ai_target() -> None:
    signal = _qfk_signal()
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qfk_execution_result",
        unit_ref={"signal_id": signal["id"]}, verification_scope="ai_step",
        dataset={"dataset_id": "preview-ai", "source_type": "pasted", "source_ref": "user-input", "payload": "FAILED"},
        signal=signal, support_id="41398", kbd_revision=7,
    )

    with pytest.raises(ValueError, match="AI_STEP_TARGET_REQUIRED"):
        await evaluate_signal_dry_run(request, ai_client=None, trace_id="c" * 32)


@pytest.mark.asyncio
async def test_qfk_ai_dry_run_passes_prompt_session_factory(monkeypatch: pytest.MonkeyPatch) -> None:
    signal = _qfk_signal()
    signal["match"]["extract"]["ai_processing"] = {
        "contract_version": 1, "mode": "extract", "instruction": "提取状态", "output_type": "string",
    }
    session_factory = object()
    seen: dict[str, object] = {}

    async def fake_extract(*args, **kwargs):
        seen["db_session_factory"] = kwargs.get("db_session_factory")
        # 对齐生产 AIExtractionResult：证据说明字段名为 reason，没有 evidence 属性。
        return SimpleNamespace(value="FAILED", evidence_line_numbers=[1], evidence_lines=["FAILED"], candidate_count=1, prompt_revision="prompt-v1", reason="已定位")

    monkeypatch.setattr("app.routes.signal_dry_run.extract_ai_value", fake_extract)
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qfk_execution_result",
        unit_ref={"signal_id": signal["id"]}, verification_scope="ai_step",
        dataset={"dataset_id": "preview-ai", "source_type": "pasted", "source_ref": "user-input", "payload": "FAILED"},
        signal=signal, support_id="41398", kbd_revision=7,
    )

    result = await evaluate_signal_dry_run(request, ai_client=object(), trace_id="d" * 32, db_session_factory=session_factory)

    assert result.status == "PASS"
    assert seen["db_session_factory"] is session_factory
    assert result.evidence == "已定位"
