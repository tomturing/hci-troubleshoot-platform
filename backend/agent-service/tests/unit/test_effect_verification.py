"""qkv_effect 条件型效果验证生产者的单元测试。

覆盖：FrontendSignal/qmap 映射、parse_frontend_value 分支、_signal_to_qkv
显式拒绝、执行层策略开关与三态判定合成（achieved/not_achieved/inconclusive）。
"""

from __future__ import annotations

import asyncio
import json

from app.tools.effect import adapter as effect_adapter
from app.tools.effect import store as effect_store
from app.tools.qkv.parser import parse_frontend_value
from app.tools.qkv.signal import FrontendQueryType, FrontendSignal


def _effect_signal_v2(**args_overrides) -> dict:
    args = {
        "usage": "remediation_verify",
        "expectation": {
            "observation": {"tool": "qkv_alert", "args": {"keyword": "内存不足"}},
            "matcher": {"type": "exists", "expected": False, "extract": {"type": "text", "rows": {"mode": "all"}}},
            "settle_seconds": 0,
            "window_seconds": 60,
            "max_recheck": 0,
        },
        "host": "{{HOST}}",
        "timeout": 60,
    }
    args.update(args_overrides)
    return {
        "id": "s_effect_mem_alert_cleared",
        "acquire": {"tool": "qkv_effect", "args": args},
        "match": None,
        "orchestrate": {
            "phase": "remediation",
            "requires": ["HOST"],
            "produces": [
                {"name": "EFFECT_STATUS", "path": "verdict"},
                {"name": "EFFECT_CHECKED_AT", "path": "checked_at"},
                {"name": "EFFECT_EVIDENCE", "path": "evidence_ref"},
            ],
        },
        "provenance": {"category": "frontend", "evidence": "效果复核样例"},
    }


# ─── FrontendSignal / qmap ─────────────────────────────────────────────────


def test_frontend_signal_from_dict_maps_qkv_effect():
    signal = FrontendSignal.from_dict(_effect_signal_v2())
    assert signal.query == FrontendQueryType.EFFECT
    assert signal.usage == "remediation_verify"
    assert signal.expectation is not None
    assert signal.expectation["observation"]["tool"] == "qkv_alert"


def test_frontend_signal_effect_requires_expectation():
    import pytest

    with pytest.raises(ValueError):
        FrontendSignal.from_dict(
            {
                "acquire": {"tool": "qkv_effect", "args": {"usage": "remediation_verify"}},
                "orchestrate": {"produces": []},
            }
        )


# ─── parse_frontend_value 分支 ──────────────────────────────────────────────


def test_parse_frontend_value_effect_hardcoded_fallback():
    payload = json.dumps({"verdict": "not_achieved", "checked_at": "t1", "evidence_ref": "ev"})
    values = parse_frontend_value(FrontendQueryType.EFFECT, payload, None)
    assert values == [{"effect_status": "not_achieved", "effect_checked_at": "t1", "effect_evidence": "ev"}]


def test_parse_frontend_value_effect_respects_produces():
    payload = json.dumps({"verdict": "achieved", "checked_at": "t2", "evidence_ref": "ev2"})
    values = parse_frontend_value(
        FrontendQueryType.EFFECT, payload, [{"name": "EFFECT_STATUS", "path": "verdict"}]
    )
    assert values and values[0]["effect_status"] == "achieved"


def test_parse_frontend_value_effect_inconclusive_never_collapses():
    payload = json.dumps({"verdict": "inconclusive", "checked_at": "t3", "evidence_ref": "ev3"})
    values = parse_frontend_value(FrontendQueryType.EFFECT, payload, None)
    assert values[0]["effect_status"] == "inconclusive"


# ─── _signal_to_qkv 显式拒绝 ────────────────────────────────────────────────


def test_signal_to_qkv_never_builds_free_text_signal_for_effect():
    """_signal_to_qkv 必须显式拒绝 qkv_effect，防止落入 qkv_exec 自由文本路径。"""
    from app.adapters.agents.htp.kbd_differential import KBDDiagnostic

    diag = KBDDiagnostic(ai_registry=None, tool_executor=None)
    assert diag._signal_to_qkv(_effect_signal_v2(), {}) is None


# ─── 执行层策略开关 ─────────────────────────────────────────────────────────


class _FakeSession:
    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


def _patch_store(monkeypatch):
    async def _noop(*args, **kwargs):
        return None

    for name in ("create_verification_record", "update_verification_status", "insert_check_record"):
        monkeypatch.setattr(effect_store, name, _noop)


def test_adapter_disabled_by_policy(monkeypatch):
    monkeypatch.delenv("EFFECT_VERIFICATION_ENABLED", raising=False)
    result = asyncio.run(
        effect_adapter.run_effect_verification_signal(
            _effect_signal_v2(),
            {"HOST": "SVR_aCloud_670"},
            conversation_id="conv-1",
            case_id="Q1",
            db_session_factory=lambda: _FakeSession(),
        )
    )
    assert result.success is False
    assert result.error_code == "EFFECT_POLICY_DISABLED"


# ─── 三态判定合成 ───────────────────────────────────────────────────────────


def _run_adapter_with_observation(monkeypatch, observation_result: tuple[bool, str, str | None]):
    monkeypatch.setenv("EFFECT_VERIFICATION_ENABLED", "true")
    _patch_store(monkeypatch)

    async def _fake_observation(*args, **kwargs):
        return observation_result

    monkeypatch.setattr(effect_adapter, "_run_observation", _fake_observation)
    return asyncio.run(
        effect_adapter.run_effect_verification_signal(
            _effect_signal_v2(),
            {"HOST": "SVR_aCloud_670"},
            conversation_id="conv-1",
            case_id="Q1",
            db_session_factory=lambda: _FakeSession(),
        )
    )


def test_adapter_verdict_achieved_when_expectation_met(monkeypatch):
    # 观测有效且确无记录（空文本）→ exists/expected=false 通过 → achieved。
    result = _run_adapter_with_observation(monkeypatch, (True, "", None))
    assert result.success is True
    assert result.resolution["verdict"] == "achieved"
    assert result.values and result.values[0]["effect_status"] == "achieved"


def test_adapter_verdict_not_achieved_when_expectation_violated(monkeypatch):
    # 观测有效但告警仍存在 → exists/expected=false 未通过 → not_achieved。
    result = _run_adapter_with_observation(monkeypatch, (True, '[{"name": "内存不足告警"}]', None))
    assert result.success is True
    assert result.resolution["verdict"] == "not_achieved"
    assert result.values and result.values[0]["effect_status"] == "not_achieved"


def test_adapter_verdict_inconclusive_when_observation_failed(monkeypatch):
    # 观测失败只能出 inconclusive，禁止坍缩为 not_achieved。
    result = _run_adapter_with_observation(monkeypatch, (False, "", "acli 查询超时"))
    assert result.success is True
    assert result.resolution["verdict"] == "inconclusive"
    assert result.resolution["error_code"] == "OBSERVATION_ERROR"
    assert result.values and result.values[0]["effect_status"] == "inconclusive"


def test_resolve_effect_match_negative_evidence_only_for_exists_on_empty_valid_observation():
    """观测自证有效且结果确为空：exists 负证据确定性成立，其他类型保持不可定值。"""
    from shared.signals.matcher import evaluate_matcher

    exists_matcher = {"type": "exists", "expected": False, "extract": {"type": "text", "rows": {"mode": "all"}}}
    result = evaluate_matcher(exists_matcher, "")
    assert result.matched is None  # QFK 上下文不可定值
    assert effect_adapter._resolve_effect_match(exists_matcher, "", result) is True

    exists_positive = {**exists_matcher, "expected": True}
    result2 = evaluate_matcher(exists_positive, "")
    assert effect_adapter._resolve_effect_match(exists_positive, "", result2) is False

    # 非 exists 类型在空观测上仍保持 inconclusive（禁止坍缩）。
    keyword_matcher = {
        "type": "keyword",
        "pattern": "恢复",
        "expected": True,
        "extract": {"type": "text", "rows": {"mode": "all"}},
    }
    result3 = evaluate_matcher(keyword_matcher, "")
    assert effect_adapter._resolve_effect_match(keyword_matcher, "", result3) is None


def test_adapter_blocked_when_expectation_variables_unresolved(monkeypatch):
    # {{HOST}} 未提供 → NEEDS_PROBE → fail-closed，不执行观测。
    monkeypatch.setenv("EFFECT_VERIFICATION_ENABLED", "true")
    _patch_store(monkeypatch)
    result = asyncio.run(
        effect_adapter.run_effect_verification_signal(
            _effect_signal_v2(),
            {},  # 无 HOST
            conversation_id="conv-1",
            case_id="Q1",
            db_session_factory=lambda: _FakeSession(),
        )
    )
    assert result.success is False
    assert result.error_code == "EXPECTATION_SOURCE_MISSING"
