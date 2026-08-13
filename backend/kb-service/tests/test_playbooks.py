"""S0 分类驱动的完整知识清单接口测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes import playbooks


def _text_extract() -> dict:
    return {
        "type": "text",
        "rows": {"mode": "all"},
        "cardinality": "all",
        "source": "stdout",
        "value_mode": "string",
    }


class _ScalarResult:
    def __init__(self, rows):
        self._rows = rows

    def scalars(self):
        return self

    def all(self):
        return self._rows


class _SessionContext:
    def __init__(self, session):
        self._session = session

    async def __aenter__(self):
        return self._session

    async def __aexit__(self, exc_type, exc, traceback):
        return False


@pytest.mark.asyncio
async def test_inventory_returns_published_kbd_without_embedding_gate():
    signal = {
        "id": "sig_001",
        "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机失败"}},
        "match": None,
        "orchestrate": {"produces": [{"name": "VM", "path": "vm"}], "requires": []},
    }
    entry = SimpleNamespace(
        id=1,
        support_id="27123",
        title="虚拟机开机失败",
        category_id="虚拟机-003",
        status="published",
        signals_json={"schema_version": 2, "signals": [signal]},
        root_cause="镜像被占用",
        solution="解除占用",
        problem_description="虚拟机镜像忙",
        embedding=None,
        embedding_model=None,
    )
    session = SimpleNamespace(
        execute=AsyncMock(side_effect=[_ScalarResult([]), _ScalarResult([entry])]),
        commit=AsyncMock(),
    )
    db = SimpleNamespace(async_session_factory=lambda: _SessionContext(session))
    publisher = MagicMock()
    publisher.ensure_published = AsyncMock(return_value=SimpleNamespace())

    with (
        patch.object(playbooks, "_db_manager", db),
        patch.object(playbooks, "DynamicResourcePublisher", return_value=publisher),
        patch.object(playbooks, "kbd_resource_payload", return_value={}),
        patch.object(playbooks, "snapshot_revision_metadata", return_value={"revision": 4}),
    ):
        response = await playbooks.get_category_playbooks("虚拟机-003")

    assert response["sops"] == []
    assert response["kbds"][0]["support_id"] == "27123"
    assert response["kbds"][0]["executable"] is True
    assert response["kbds"][0]["signals"] == [signal]
    statements = [str(call.args[0]) for call in session.execute.await_args_list]
    sql = "\n".join(statements).lower()
    assert "embedding is not null" not in sql
    assert "embedding_model =" not in sql
    assert "tsv @@" not in sql


def test_backend_signal_without_matcher_or_output_is_visible_but_not_executable():
    signals = [{"id": "sig_002", "acquire": {"tool": "qfk_system", "args": {"command": "ps"}}}]
    assert playbooks._execution_issues(signals) == ["sig_002 必须且只能配置确定性 matcher 或有效产出变量"]


def test_qkv_with_residual_match_is_visible_but_not_executable():
    signals = [
        {
            "id": "sig_001",
            "acquire": {"tool": "qkv_task", "args": {"keyword": "启动虚拟机失败"}},
            "match": {"type": "keyword", "pattern": "", "expected": True, "extract": _text_extract()},
            "orchestrate": {"produces": [{"name": "VM", "path": "vm"}]},
        }
    ]
    assert playbooks._execution_issues(signals) == ["sig_001 的 QKV 必须配置有效产出变量且 match 为 null"]


def test_backend_signal_with_output_variables_is_executable_without_matcher():
    """QFK 产出变量模式的 match=null 是 v2 合法执行契约，不能在快照阶段被过滤。"""
    signals = [
        {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "lsof"}},
            "match": None,
            "orchestrate": {
                "produces": [
                    {
                        "name": "PID",
                        "type": "string",
                        "extract": {**_text_extract(), "cardinality": "first"},
                    }
                ]
            },
        }
    ]

    assert playbooks._execution_issues(signals) == []


def test_backend_signal_with_match_and_output_is_not_executable():
    """二义的 QFK 信号必须在执行前被拒绝，和保存 v2 契约保持一致。"""
    signals = [
        {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "lsof"}},
            "match": {"type": "keyword", "pattern": "busy", "expected": True, "extract": _text_extract()},
            "orchestrate": {"produces": [{"name": "PID", "type": "string", "path": ""}]},
        }
    ]

    assert playbooks._execution_issues(signals) == ["sig_002 必须且只能配置确定性 matcher 或有效产出变量"]


def test_stale_generation_metadata_is_visible_but_not_executable():
    signals = [
        {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
            "match": {"type": "exists", "expected": True, "extract": _text_extract()},
        }
    ]
    metadata = {
        "schema_version": 1,
        "status": "stale",
        "source_fingerprint": "0" * 64,
        "prompt_revision": "1" * 64,
        "model_id": "model-v1",
        "tool_contract_revision": playbooks.current_tool_contract_revision(),
        "generation_fingerprint": "2" * 64,
    }

    issues = playbooks._execution_issues(
        signals,
        {
            "schema_version": 2,
            "signals": signals,
            "generation_metadata": metadata,
        },
    )

    assert issues == ["Signal/Contract 生成输入已变化，必须重新抽取或完成人工复核"]


def test_current_expert_publish_stamp_overrides_old_generation_contract_revision():
    signals = [
        {
            "id": "sig_002",
            "acquire": {"tool": "qfk_system", "args": {"command": "ps"}},
            "match": {"type": "exists", "expected": True, "extract": _text_extract()},
        }
    ]
    document = {
        "schema_version": 2,
        "signals": signals,
        "generation_metadata": {
            "schema_version": 1,
            "status": "current",
            "source_fingerprint": "0" * 64,
            "prompt_revision": "1" * 64,
            "model_id": "model-v1",
            "tool_contract_revision": "2" * 64,
            "generation_fingerprint": "3" * 64,
        },
        "publish_validation": {
            "schema_version": 1,
            "status": "passed",
            "tool_contract_revision": playbooks.current_tool_contract_revision(),
            "validator": "expert_publish_gate",
        },
    }

    assert playbooks._execution_issues(signals, document) == []


def _threshold_match_with_metric() -> dict:
    """带 match.metric 的确定性阈值判定信号（qfk 工具、无产出变量）。"""
    return {
        "id": "sig_metric_001",
        "acquire": {"tool": "qfk_hardware", "args": {"command": "free"}},
        "match": {
            "type": "threshold",
            "expected": True,
            "metric": "used_memory_ratio",
            "value": 0.9,
            "operator": ">=",
            "extract": _text_extract(),
        },
    }


def test_signal_match_metric_is_allowed_by_schema():
    """回归：match.metric 已被 signal.v2.schema.json 放行，带 metric 的确定性
    matcher 不应再触发 additionalProperties 拒绝盲区。"""
    from shared.schemas.signal_schema import validate_publishable_signals_json

    document = {"schema_version": 2, "signals": [_threshold_match_with_metric()]}
    # 不抛异常即通过；此前会在契约校验阶段报
    # 'Additional properties are not allowed ('metric' was unexpected)'。
    validate_publishable_signals_json(document)


def test_signal_match_metric_keeps_executable():
    """闭环：带 match.metric 的确定性 matcher 经过契约校验后 executable=True
    （_execution_issues 为空），kb-service 不会再把可执行的 metric 信号误判为
    executable=False。"""
    issues = playbooks._execution_issues([_threshold_match_with_metric()])
    assert issues == []


def test_signal_unknown_extra_property_still_rejected():
    """防御：additionalProperties 约束未被整体关闭，未定义的字段仍被拒绝，
    证明 metric 是精准放行而非放开整段 match。"""
    from shared.schemas.signal_schema import validate_publishable_signals_json

    bad_signal = _threshold_match_with_metric()
    bad_signal["match"] = {
        **bad_signal["match"],
        "bogus_field": "should be rejected",
    }
    document = {"schema_version": 2, "signals": [bad_signal]}
    try:
        validate_publishable_signals_json(document)
    except Exception as exc:
        assert "bogus_field" in str(getattr(exc, "message", exc))
    else:
        raise AssertionError("未定义的 match 字段未被 additionalProperties 拒绝")


# ─────────────────────────────────────────────────────────────────────────────
# 契约门禁分级判定（结构/语义指纹，见 ADR 2026-08-13-kbd-contract-gate-structural-semantic-fingerprint）
# ─────────────────────────────────────────────────────────────────────────────

from shared.schemas.signal_generation import (
    FP_ALGO_VERSION,
    current_semantic_fingerprint,
    current_structural_fingerprint,
    current_tool_contract_revision,
)


def _contract_doc(structural: str, semantic: str, fp_algo: int = FP_ALGO_VERSION) -> dict:
    """构造一个通过内部信号校验的合法 qfk 信号文档（确定性 matcher）。"""
    return {
        "schema_version": 2,
        "signals": [
            {
                "id": "s1",
                "acquire": {"tool": "qfk_vm", "args": {"command": "echo"}},
                "match": {"type": "keyword", "pattern": "x"},
                "orchestrate": {"produces": []},
            }
        ],
        "publish_validation": {
            "schema_version": 1,
            "status": "passed",
            "tool_contract_revision": current_tool_contract_revision(),
            "validator": "expert_publish_gate",
            "structural_revision": structural,
            "semantic_revision": semantic,
            "fp_algo_version": fp_algo,
        },
    }


def _exec(doc: dict) -> list[str]:
    return playbooks._execution_issues(doc["signals"], doc, support_id="X", category_id="虚拟机-015")


def test_contract_new_snapshot_executable():
    """新快照指纹全匹配 -> 可执行，无契约 issue。"""
    cs, sem = current_structural_fingerprint(), current_semantic_fingerprint()
    with patch.object(playbooks, "validate_publishable_signals_json", lambda d: None):
        issues = _exec(_contract_doc(cs, sem))
    assert not any("契约" in i or "重新发布" in i for i in issues)


def test_contract_semantic_drift_soft_stale():
    """结构兼容、语义漂移 -> 放行（不阻断），仅观测。"""
    cs, sem = current_structural_fingerprint(), current_semantic_fingerprint()
    with patch.object(playbooks, "validate_publishable_signals_json", lambda d: None):
        issues = _exec(_contract_doc(cs, "0" * 64))
    assert not issues  # 结构相等，仅语义漂移，仍可执行


def test_contract_struct_change_compatible_passes():
    """结构不等但旧信号仍能通过当前 schema 校验（如新增可选属性）-> 放行。"""
    sem = current_semantic_fingerprint()
    with patch.object(playbooks, "validate_publishable_signals_json", lambda d: None):
        issues = _exec(_contract_doc("0" * 64, sem))
    assert not issues


def test_contract_struct_change_breaking_hard_break():
    """结构不等且旧信号无法在新契约下执行 -> 真阻断（hard_break）。"""
    sem = current_semantic_fingerprint()
    with patch.object(
        playbooks, "validate_publishable_signals_json", side_effect=Exception("schema mismatch")
    ):
        issues = _exec(_contract_doc("0" * 64, sem))
    assert any("破坏性变更" in i for i in issues)


def test_contract_fp_algo_mismatch_soft_stale():
    """指纹算法版本不符 -> 不阻断（提示全量重算刷新）。"""
    cs, sem = current_structural_fingerprint(), current_semantic_fingerprint()
    with patch.object(playbooks, "validate_publishable_signals_json", lambda d: None):
        issues = _exec(_contract_doc(cs, sem, fp_algo=FP_ALGO_VERSION + 1))
    assert not issues


def test_contract_legacy_snapshot_fallback_stale():
    """旧快照（仅 tool_contract_revision 且 hash 不等）-> 回退原门禁过期。"""
    doc = {
        "schema_version": 2,
        "signals": [
            {
                "id": "s1",
                "acquire": {"tool": "qfk_vm", "args": {"command": "echo"}},
                "match": {"type": "keyword", "pattern": "x"},
                "orchestrate": {"produces": []},
            }
        ],
        "publish_validation": {
            "schema_version": 1,
            "status": "passed",
            "tool_contract_revision": "0" * 64,
            "validator": "expert_publish_gate",
        },
    }
    issues = playbooks._execution_issues(doc["signals"], doc, support_id="23821", category_id="虚拟机-015")
    assert any("工具契约版本已过期" in i for i in issues)
