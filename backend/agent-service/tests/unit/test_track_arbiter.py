"""结论级融合（arbiter）测试：两套独立结论 → 一个最终结论。

核心不变量：
1. **恒等变换**：某条链路对该候选没有信号时「无发言权」，融合结果必须逐字等于另一条链路的结论；
2. **纯函数**：不修改任何输入对象；
3. **EXCLUDE 全局否决**：排除项与证据来源无关，任一路成立即整体 REJECTED；
4. **shadow 只让关键链路参与结论**：根因链路照跑照记指标，但不影响线上判定。
"""

from __future__ import annotations

import pytest
from app.adapters.agents.htp.track_arbiter import (
    DEFAULT_TRACK_MODE,
    MODE_DUAL,
    MODE_PROCESS_ONLY,
    MODE_ROOT_ONLY,
    MODE_SHADOW,
    PROCESS_TRACK,
    ROOT_TRACK,
    TRACK_MODE_ENV,
    _is_conflict,
    _merge_states,
    conclusion_tracks,
    exclude_rejected,
    fuse_tracks,
    resolve_track_mode,
    runnable_tracks,
)
from shared.cdd import CandidateState, SignalOutcome, compile_signal_plan, decide_conclusion
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates

EXTRACT = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}


def _signal(signal_id: str, *, role: str | None = None, pattern: str = "core dump") -> dict:
    signal = {
        "id": signal_id,
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": pattern, "mode": "or", "expected": True, "extract": EXTRACT},
    }
    if role:
        signal["role"] = role
    return signal


def _kbd(kbd_id: str, *, role: str | None = None, signal_id: str = "s1", pattern: str = "core dump") -> dict:
    return {
        "id": kbd_id,
        "support_id": f"K{kbd_id}",
        "category_id": "c",
        "resource_revision": {"revision": "1"},
        "signals": [_signal(signal_id, role=role, pattern=pattern)],
    }


def _kbd_with_exclude(kbd_id: str) -> dict:
    """MUST + EXCLUDE 两条信号：只有具备 required 信号的 KBD 才能通过编译。"""
    return {
        "id": kbd_id,
        "support_id": f"K{kbd_id}",
        "category_id": "c",
        "resource_revision": {"revision": "1"},
        "signals": [_signal("s1"), _signal("x1", role="exclude", pattern="not this")],
    }


def _kbd_without_signals(kbd_id: str) -> dict:
    return {"id": kbd_id, "support_id": f"K{kbd_id}", "category_id": "c", "signals": []}


def _track(kbds: list[dict], outcomes: dict[str, dict[str, SignalOutcome]], snapshot: str):
    """编译一条链路的计划并按（kbd_id → signal_id → outcome）回填，归约到终态。"""
    from shared.cdd.kbd_model import kbd_from_dict

    plan = compile_signal_plan([kbd_from_dict(item) for item in kbds], snapshot_id=snapshot)
    assessments = initial_assessments(plan)
    for ref in plan.signals.values():
        outcome = outcomes.get(ref.kbd_id, {}).get(ref.signal_id)
        if outcome is not None:
            assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = outcome
    reduce_candidates(plan, assessments, finalize=True)
    return plan, assessments


def _state(kbds: list[dict], kbd_id: str, outcome: SignalOutcome, snapshot: str):
    plan, assessments = _track(kbds, {kbd_id: {"s1": outcome}}, snapshot)
    return plan, assessments


# ── 1. 模式与链路选择 ──────────────────────────────────────────────────────
def test_default_mode_is_dual():
    assert DEFAULT_TRACK_MODE == MODE_DUAL
    assert resolve_track_mode(None) == MODE_DUAL


def test_resolve_mode_reads_env(monkeypatch):
    monkeypatch.setenv(TRACK_MODE_ENV, MODE_SHADOW)
    assert resolve_track_mode() == MODE_SHADOW
    assert resolve_track_mode(" ROOT_ONLY ") == MODE_ROOT_ONLY


def test_resolve_mode_falls_back_on_invalid(monkeypatch):
    monkeypatch.setenv(TRACK_MODE_ENV, "not-a-mode")
    assert resolve_track_mode() == MODE_DUAL


@pytest.mark.parametrize(
    "mode,expected_runnable",
    [
        (MODE_PROCESS_ONLY, (PROCESS_TRACK,)),
        (MODE_ROOT_ONLY, (ROOT_TRACK,)),
        (MODE_SHADOW, (PROCESS_TRACK, ROOT_TRACK)),
        (MODE_DUAL, (PROCESS_TRACK, ROOT_TRACK)),
    ],
)
def test_runnable_tracks(mode, expected_runnable):
    assert runnable_tracks(mode) == expected_runnable


@pytest.mark.parametrize(
    "mode,expected_conclusion",
    [
        (MODE_PROCESS_ONLY, (PROCESS_TRACK,)),
        (MODE_ROOT_ONLY, (ROOT_TRACK,)),
        # shadow：两条都跑，但结论只用关键链路
        (MODE_SHADOW, (PROCESS_TRACK,)),
        (MODE_DUAL, (PROCESS_TRACK, ROOT_TRACK)),
    ],
)
def test_conclusion_tracks(mode, expected_conclusion):
    assert conclusion_tracks(mode) == expected_conclusion


# ── 2. EXCLUDE 判定 ───────────────────────────────────────────────────────
def test_exclude_rejected_detects_satisfied_exclude_signal():
    plan, assessments = _track(
        [_kbd_with_exclude("7")], {"7": {"s1": SignalOutcome.SATISFIED, "x1": SignalOutcome.SATISFIED}}, "t"
    )
    assert assessments["7"].state is CandidateState.REJECTED
    assert exclude_rejected(plan, assessments["7"]) is True


def test_exclude_rejected_false_for_must_contradiction():
    """MUST 被否证也是 REJECTED，但不是「排除项成立」，不触发全局否决。"""
    plan, assessments = _state([_kbd("7")], "7", SignalOutcome.CONTRADICTED, "t")
    assert assessments["7"].state is CandidateState.REJECTED
    assert exclude_rejected(plan, assessments["7"]) is False


def test_exclude_rejected_false_when_not_rejected():
    plan, assessments = _track(
        [_kbd_with_exclude("7")], {"7": {"s1": SignalOutcome.NOT_RUN, "x1": SignalOutcome.NOT_RUN}}, "t"
    )
    assert exclude_rejected(plan, assessments["7"]) is False


def test_exclude_rejected_handles_missing_inputs():
    assert exclude_rejected(None, None) is False


# ── 3. 状态归并真值表 ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "states,excludes,expected",
    [
        ([], [], CandidateState.NOT_EXECUTABLE),
        ([CandidateState.SUPPORTED], [False], CandidateState.SUPPORTED),
        ([CandidateState.CANDIDATE], [False], CandidateState.CANDIDATE),
        # 任一支持即支持：另一路还没跑完不能把已命中的拖回候选态
        ([CandidateState.SUPPORTED, CandidateState.CANDIDATE], [False, False], CandidateState.SUPPORTED),
        ([CandidateState.SUPPORTED, CandidateState.NOT_EXECUTABLE], [False, False], CandidateState.SUPPORTED),
        # 未完成不等于拒绝
        ([CandidateState.CANDIDATE, CandidateState.REJECTED], [False, False], CandidateState.CANDIDATE),
        ([CandidateState.INCONCLUSIVE, CandidateState.REJECTED], [False, False], CandidateState.INCONCLUSIVE),
        ([CandidateState.REJECTED, CandidateState.REJECTED], [False, False], CandidateState.REJECTED),
        ([CandidateState.NOT_EXECUTABLE, CandidateState.NOT_EXECUTABLE], [False, False], CandidateState.NOT_EXECUTABLE),
        # EXCLUDE 全局否决压过一切，包括另一路的 SUPPORTED
        ([CandidateState.SUPPORTED, CandidateState.REJECTED], [False, True], CandidateState.REJECTED),
        ([CandidateState.SUPPORTED], [True], CandidateState.REJECTED),
    ],
)
def test_merge_states_truth_table(states, excludes, expected):
    assert _merge_states(states, excludes) is expected


def test_is_conflict_only_for_opposite_definitive_states():
    assert _is_conflict(CandidateState.SUPPORTED, CandidateState.REJECTED) is True
    assert _is_conflict(CandidateState.REJECTED, CandidateState.SUPPORTED) is True
    assert _is_conflict(CandidateState.SUPPORTED, CandidateState.CANDIDATE) is False
    assert _is_conflict(None, CandidateState.REJECTED) is False


# ── 4. 恒等变换：某一路无信号时融合结果逐字等于另一路 ───────────────────────
@pytest.mark.parametrize(
    "outcome,expected",
    [
        (SignalOutcome.SATISFIED, CandidateState.SUPPORTED),
        (SignalOutcome.CONTRADICTED, CandidateState.REJECTED),
        (SignalOutcome.NOT_RUN, CandidateState.INCONCLUSIVE),
        (SignalOutcome.BLOCKED, CandidateState.INCONCLUSIVE),
    ],
)
def test_fusion_identity_when_only_process_has_signals(outcome, expected):
    """只有关键信号、没有根因信号的 KBD：上线双链路对它是零行为变化。"""
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", outcome, "p")
    assert process_assessments["7"].state is expected

    fused = fuse_tracks(mode=MODE_DUAL, process_plan=process_plan, process_assessments=process_assessments)

    assert fused.assessments["7"].state is expected
    assert fused.details["7"].root_state is None
    assert fused.decision.level is decide_conclusion(process_assessments).level


def test_fusion_identity_when_only_root_has_signals():
    kbds = [_kbd("7")]
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "r")
    fused = fuse_tracks(mode=MODE_DUAL, root_plan=root_plan, root_assessments=root_assessments)

    assert fused.assessments["7"].state is CandidateState.SUPPORTED
    assert fused.details["7"].process_state is None
    assert fused.decision.supported_ids == ("7",)


def test_kbd_without_any_signal_in_a_track_has_no_voice():
    """有评估对象但一条信号都没编译出来（如语义型 KBD）→ 视为无发言权。"""
    from shared.cdd.kbd_model import kbd_from_dict

    process_plan, process_assessments = _state([_kbd("7")], "7", SignalOutcome.SATISFIED, "p")
    root_plan = compile_signal_plan([kbd_from_dict(_kbd_without_signals("8"))], snapshot_id="r")
    root_assessments = initial_assessments(root_plan)

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    # KBD 8 在根因链路无信号 → 不出现在融合结果里
    assert set(fused.assessments) == {"7"}


# ── 5. dual 融合语义 ──────────────────────────────────────────────────────
def test_dual_supported_wins_over_root_inconclusive():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.NOT_RUN, "r")

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    assert fused.assessments["7"].state is CandidateState.SUPPORTED
    assert fused.details["7"].conflict is False
    assert fused.decision.level.value in {"DEFINITIVE", "PARTIAL"}


def test_dual_root_supported_rescues_unfinished_process():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.NOT_RUN, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "r")

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    assert fused.assessments["7"].state is CandidateState.SUPPORTED
    assert fused.decision.supported_ids == ("7",)


def test_dual_conflict_is_recorded_but_support_kept():
    """一路支持一路拒绝：融合取支持（互为备份），但必须记录冲突供人工复核。"""
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "r")

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    assert fused.assessments["7"].state is CandidateState.SUPPORTED
    assert fused.conflicts == ("7",)
    assert fused.details["7"].conflict is True


def test_dual_exclude_in_root_vetoes_process_support():
    """根因链路的排除项成立 → 全局否决，尽管关键链路已支持。"""
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "p")
    root_plan, root_assessments = _track(
        [_kbd_with_exclude("7")], {"7": {"s1": SignalOutcome.SATISFIED, "x1": SignalOutcome.SATISFIED}}, "r"
    )

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    assert fused.assessments["7"].state is CandidateState.REJECTED
    assert fused.details["7"].exclude_tracks == (ROOT_TRACK,)


def test_dual_both_rejected_is_rejected():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "r")

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    assert fused.assessments["7"].state is CandidateState.REJECTED
    assert fused.details["7"].conflict is False


# ── 6. shadow / root_only 的结论归属 ──────────────────────────────────────
def test_shadow_ignores_root_conclusion_but_still_reports_it():
    """shadow = 赛马最安全的观测模式：根因结论只进指标，不进判定。"""
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.NOT_RUN, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "r")

    fused = fuse_tracks(
        mode=MODE_SHADOW,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    assert fused.assessments["7"].state is CandidateState.INCONCLUSIVE
    assert fused.decision.supported_ids == ()
    # 根因链路的结论仍然被记录，供赛马对比
    assert fused.details["7"].root_state == CandidateState.SUPPORTED.value


def test_root_only_ignores_process_conclusion():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "r")

    fused = fuse_tracks(
        mode=MODE_ROOT_ONLY,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    assert fused.assessments["7"].state is CandidateState.REJECTED
    assert fused.details["7"].process_state == CandidateState.SUPPORTED.value


# ── 7. 纯函数性 & 审计载荷 ────────────────────────────────────────────────
def test_fusion_does_not_mutate_inputs():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "r")
    process_before = (process_assessments["7"].state, dict(process_assessments["7"].signal_outcomes))
    root_before = (root_assessments["7"].state, dict(root_assessments["7"].signal_outcomes))

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )

    assert (process_assessments["7"].state, process_assessments["7"].signal_outcomes) == process_before
    assert (root_assessments["7"].state, root_assessments["7"].signal_outcomes) == root_before
    assert fused.assessments["7"] is not process_assessments["7"]
    assert fused.assessments["7"] is not root_assessments["7"]


def test_fusion_reasons_carry_track_attribution():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.NOT_RUN, "r")

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    reasons = fused.assessments["7"].reasons
    assert any(reason.startswith("[process]") for reason in reasons)
    assert any(reason.startswith("[root]") for reason in reasons)
    assert any(reason.startswith("[fusion]") for reason in reasons)


def test_fusion_merges_signal_outcomes_from_both_tracks():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "r")

    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    outcomes = fused.assessments["7"].signal_outcomes
    assert len(outcomes) == 2
    assert set(outcomes.values()) == {SignalOutcome.SATISFIED, SignalOutcome.CONTRADICTED}


def test_empty_fusion_is_inconclusive_with_reason():
    fused = fuse_tracks(mode=MODE_DUAL)
    assert fused.assessments == {}
    assert fused.decision.level.value == "INCONCLUSIVE"
    assert "no candidate" in fused.decision.reason


def test_detail_map_is_json_safe():
    kbds = [_kbd("7")]
    process_plan, process_assessments = _state(kbds, "7", SignalOutcome.SATISFIED, "p")
    root_plan, root_assessments = _state(kbds, "7", SignalOutcome.CONTRADICTED, "r")
    fused = fuse_tracks(
        mode=MODE_DUAL,
        process_plan=process_plan,
        process_assessments=process_assessments,
        root_plan=root_plan,
        root_assessments=root_assessments,
    )
    import json

    encoded = json.dumps(fused.detail_map(), ensure_ascii=False)
    assert '"conflict": true' in encoded
    assert fused.mode == MODE_DUAL
