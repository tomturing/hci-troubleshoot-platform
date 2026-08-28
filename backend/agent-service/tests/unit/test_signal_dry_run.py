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
async def test_qkv_dry_run_produces_filters_raw_fields_before_output_processing() -> None:
    signal = {
        "id": "sig_qkv_task",
        "acquire": {"tool": "qkv_task", "args": {"instruction": "查看任务"}},
        "orchestrate": {
            "produces": [
                {"name": "ERRCODE_TRACING", "path": "errcode_tracing"},
                {"name": "DESCRIPTION", "path": "description"},
            ],
            "output_processing": [
                {"mode": "assert", "input": "{{ERRCODE_TRACING}}", "match": {"type": "keyword", "pattern": "0x1900006c", "mode": "or", "expected": True, "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"}}},
                {"mode": "assert", "input": "{{DESCRIPTION}}", "match": {"type": "keyword", "pattern": "无法导出基镜像", "mode": "or", "expected": True, "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"}}},
            ],
        },
    }
    raw_task_payload = [
        {
            "action_type": 0, "alert_type": "新建虚拟机", "bcancel": 0,
            "description": "无法导出基镜像，所有可用基镜像副本均导出失败；无法导出基镜像副本，虚拟存储上拷贝文件失败；服务异常（0x1900006c）",
            "errcode_tracing": "0x1900006c", "event_id": 1048595, "host": "SVR_aCloud_681",
            "object_name": "Windows-DISK", "pid": "UPID:host-80615f1e94a6", "status": 2,
        }
    ]
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qkv_variable_processing",
        unit_ref={"signal_id": signal["id"]}, verification_scope="signal",
        dataset={"dataset_id": "preview-qkv", "source_type": "pasted", "source_ref": "user-input", "payload": raw_task_payload},
        signal=signal, support_id="41398", kbd_revision=18,
    )

    result = await evaluate_signal_dry_run(request, ai_client=None, trace_id="e" * 32)

    assert result.status == "PASS"
    # 核心断言：30 多个冗余字段已被剔除，输出值只包含声明的 produces 变量！
    assert result.value == [
        {
            "errcode_tracing": "0x1900006c",
            "description": "无法导出基镜像，所有可用基镜像副本均导出失败；无法导出基镜像副本，虚拟存储上拷贝文件失败；服务异常（0x1900006c）",
        }
    ]



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
        return SimpleNamespace(
            value="FAILED", evidence_line_numbers=[1], evidence_lines=["FAILED"], candidate_count=1,
            prompt_revision="prompt-v1", reason="已定位",
            raw_response={"status": "success", "output": "FAILED", "evidence": [{"ref": "line:1", "quote": "FAILED"}], "reason": "已定位"},
        )

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
    assert result.ai_raw_response and result.ai_raw_response["status"] == "success"


@pytest.mark.asyncio
async def test_qkv_dry_run_pure_producer_success() -> None:
    signal = {
        "id": "sig_qkv_producer",
        "acquire": {"tool": "qkv_task", "args": {"keyword": "0x1900006c"}},
        "orchestrate": {
            "produces": [
                {"name": "HOST", "path": "host"},
                {"name": "VM", "path": "vm"},
                {"name": "REQUEST_ID", "path": "request_id"},
            ]
        },
    }
    # 测试包含 {"data": [...]} 包装对象的自适应解析与变量提取
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qkv_variable_processing",
        unit_ref={"signal_id": signal["id"]}, verification_scope="signal",
        dataset={
            "dataset_id": "fixture-1",
            "source_type": "fixture",
            "source_ref": "sha256:abcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcdabcd",
            "payload": {
                "data": [
                    {
                        "type": "0x1900006c",
                        "host": "SIM-HCI-NODE-01",
                        "vm": "SIM-VM-41398",
                        "request_id": "SIM-REQUEST-41398",
                    }
                ]
            },
        },
        signal=signal, support_id="41398", kbd_revision=7,
    )

    result = await evaluate_signal_dry_run(request, ai_client=None, trace_id="e" * 32)

    assert result.status == "PASS"
    assert result.value == [{"host": "SIM-HCI-NODE-01", "vm": "SIM-VM-41398", "request_id": "SIM-REQUEST-41398"}]
    assert result.derivation["produces"] == ["host", "vm", "request_id"]
    assert result.derivation["record_count"] == 1


@pytest.mark.asyncio
async def test_qkv_dry_run_pure_producer_fail_when_no_variables_matched() -> None:
    signal = {
        "id": "sig_qkv_producer",
        "acquire": {"tool": "qkv_task", "args": {}},
        "orchestrate": {
            "produces": [
                {"name": "HOST", "path": "host"},
                {"name": "VM", "path": "vm"},
            ]
        },
    }
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qkv_variable_processing",
        unit_ref={"signal_id": signal["id"]}, verification_scope="signal",
        dataset={
            "dataset_id": "preview-fail",
            "source_type": "pasted",
            "source_ref": "user-input",
            "payload": [{"unrelated_field": "foo"}],
        },
        signal=signal, support_id="41398", kbd_revision=7,
    )

    result = await evaluate_signal_dry_run(request, ai_client=None, trace_id="f" * 32)

    assert result.status == "FAIL"
    assert result.value == []
    assert "未能按 produces 规格提取出任何有效变量" in result.evidence


@pytest.mark.asyncio
async def test_qkv_dry_run_pure_producer_rejects_ai_step() -> None:
    signal = {
        "id": "sig_qkv_producer",
        "acquire": {"tool": "qkv_task", "args": {}},
        "orchestrate": {
            "produces": [{"name": "HOST", "path": "host"}]
        },
    }
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qkv_variable_processing",
        unit_ref={"signal_id": signal["id"]}, verification_scope="ai_step",
        dataset={
            "dataset_id": "preview-qkv",
            "source_type": "pasted",
            "source_ref": "user-input",
            "payload": [{"host": "NODE-1"}],
        },
        signal=signal, support_id="41398", kbd_revision=7,
    )

    with pytest.raises(ValueError, match="AI_STEP_TARGET_REQUIRED"):
        await evaluate_signal_dry_run(request, ai_client=None, trace_id="1" * 32)


@pytest.mark.asyncio
async def test_qkv_dry_run_multi_records_stream_filtering_passes_and_cleans_output() -> None:
    """验证多条告警队列输入时，包含无关告警不会误判 FAIL，且输出值仅保留命中的记录。"""
    signal = {
        "id": "sig_qkv_alert",
        "acquire": {"tool": "qkv_alert", "args": {"instruction": "获取告警"}},
        "orchestrate": {
            "produces": [
                {"name": "VM", "path": "object_id"},
                {"name": "DESCRIPTION", "path": "description"},
            ],
            "output_processing": [
                {
                    "mode": "assert",
                    "input": "{{DESCRIPTION}}",
                    "match": {
                        "type": "keyword",
                        "pattern": "存在虚拟机，请迁移后再删除",
                        "mode": "or",
                        "expected": True,
                        "extract": {"type": "text", "rows": {"mode": "all"}, "cardinality": "all", "source": "stdout"},
                    },
                }
            ],
        },
    }
    raw_alerts_payload = [
        {
            "action_type": 0,
            "alert_type": "删除虚拟机",
            "description": "存在虚拟机，请迁移后再删除",
            "object_id": "7903385510955",
            "object_name": "Windows-DISK",
        },
        {
            "action_type": 0,
            "alert_type": "启动虚拟机",
            "description": "启动虚拟机（Rocky-IMG）失败，错误信息：虚拟机镜像忙，正在执行其他操作！",
            "object_id": "18864231143",
            "object_name": "Rocky-IMG",
        },
    ]
    request = SignalDryRunRequest(
        draft_revision=_revision(signal), scope="qkv_variable_processing",
        unit_ref={"signal_id": signal["id"]}, verification_scope="signal",
        dataset={"dataset_id": "preview-multi-qkv", "source_type": "pasted", "source_ref": "user-input", "payload": raw_alerts_payload},
        signal=signal, support_id="41398", kbd_revision=20,
    )

    result = await evaluate_signal_dry_run(request, ai_client=None, trace_id="8" * 32)

    assert result.status == "PASS"
    # 核心断言：输出值只保留命中的删除虚拟机记录，启动虚拟机的无关记录被流式过滤剔除！
    assert len(result.value) == 1
    assert result.value == [{"vm": "7903385510955", "description": "存在虚拟机，请迁移后再删除"}]


