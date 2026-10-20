"""双路证据晚融合（track 分路 + 跨路 OR）的纯函数与计划级测试。

设计依据：docs/solution/knowledge-base/信号双路并行与最少充分集设计.md §3。

覆盖三类不变量：
1. fusion 纯函数：EXCLUDE 全局否决 > 任一 SUPPORTED > 仍有 CANDIDATE > 全终止态；
2. 归约器：一路的 MUST CONTRADICTED 绝不否决另一路，同路内仍严格 AND；
3. 融合模式：process_only / shadow 只看关键路，root_only 只看根因路（灰度与回滚）。
"""

from __future__ import annotations

from shared.cdd import CandidateState, EvidenceRole, SignalOutcome, compile_signal_plan
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.fusion import (
    FUSION_MODES,
    FUSION_PROCESS_ONLY,
    FUSION_ROOT_ONLY,
    FUSION_ROOT_OR_PROCESS,
    FUSION_SHADOW,
    PROCESS_TRACK,
    ROOT_TRACK,
    active_fusion_tracks,
    fuse_track_states,
)
from shared.cdd.kbd_model import kbd_from_dict

S = CandidateState.SUPPORTED
R = CandidateState.REJECTED
C = CandidateState.CANDIDATE
NE = CandidateState.NOT_EXECUTABLE
INC = CandidateState.INCONCLUSIVE

_EXTRACT = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}


# ─── fusion 纯函数 ────────────────────────────────────────────────────────────


class TestFuseTrackStates:
    def test_any_supported_wins(self):
        assert fuse_track_states({PROCESS_TRACK: S, ROOT_TRACK: S}) is S
        assert fuse_track_states({PROCESS_TRACK: R, ROOT_TRACK: S}) is S
        assert fuse_track_states({PROCESS_TRACK: S, ROOT_TRACK: R}) is S

    def test_must_rejection_is_local_not_cross_track(self):
        """一路 MUST 被判否证只否决本路；另一路仍可独立成立（互不影响的核心）。"""
        assert (
            fuse_track_states(
                {PROCESS_TRACK: R, ROOT_TRACK: S},
                rejected_by={PROCESS_TRACK: "must"},
            )
            is S
        )
        assert (
            fuse_track_states(
                {PROCESS_TRACK: S, ROOT_TRACK: R},
                rejected_by={ROOT_TRACK: "must"},
            )
            is S
        )

    def test_exclude_vetoes_globally(self):
        """排除项表达「一定不是本 KBD」，与证据来自哪一路无关。"""
        assert (
            fuse_track_states(
                {PROCESS_TRACK: R, ROOT_TRACK: S},
                rejected_by={PROCESS_TRACK: "exclude"},
            )
            is R
        )
        assert (
            fuse_track_states(
                {PROCESS_TRACK: S, ROOT_TRACK: R},
                rejected_by={ROOT_TRACK: "exclude"},
            )
            is R
        )

    def test_unfinished_track_does_not_drag_back_supported(self):
        """已支持的一路不得被另一路的「尚未跑完」拖回候选——否则两路重新耦合。"""
        assert fuse_track_states({PROCESS_TRACK: S, ROOT_TRACK: C}) is S
        assert fuse_track_states({PROCESS_TRACK: C, ROOT_TRACK: S}) is S

    def test_all_rejected_or_not_executable(self):
        assert fuse_track_states({PROCESS_TRACK: R, ROOT_TRACK: R}) is R
        assert fuse_track_states({PROCESS_TRACK: NE, ROOT_TRACK: NE}) is NE
        # 混合终止态：既非全拒也非全能执行 → 不确定，绝不把「无法执行」当「已排除」
        assert fuse_track_states({PROCESS_TRACK: R, ROOT_TRACK: NE}) is INC

    def test_no_active_track_keeps_candidate(self):
        assert fuse_track_states({}) is C
        # 非已知 track 名称一律忽略，避免脏数据改变结论
        assert fuse_track_states({"legacy": S}) is C

    def test_process_only_ignores_root(self):
        assert fuse_track_states({PROCESS_TRACK: R, ROOT_TRACK: S}, mode=FUSION_PROCESS_ONLY) is R
        assert fuse_track_states({ROOT_TRACK: S}, mode=FUSION_PROCESS_ONLY) is C

    def test_shadow_behaves_like_process_only(self):
        assert fuse_track_states({PROCESS_TRACK: R, ROOT_TRACK: S}, mode=FUSION_SHADOW) is R

    def test_root_only_ignores_process(self):
        assert fuse_track_states({PROCESS_TRACK: S, ROOT_TRACK: R}, mode=FUSION_ROOT_ONLY) is R
        assert fuse_track_states({PROCESS_TRACK: S}, mode=FUSION_ROOT_ONLY) is C

    def test_active_fusion_tracks(self):
        assert active_fusion_tracks(FUSION_ROOT_OR_PROCESS) == (PROCESS_TRACK, ROOT_TRACK)
        assert active_fusion_tracks(FUSION_PROCESS_ONLY) == (PROCESS_TRACK,)
        assert active_fusion_tracks(FUSION_SHADOW) == (PROCESS_TRACK,)
        assert active_fusion_tracks(FUSION_ROOT_ONLY) == (ROOT_TRACK,)
        # 未知模式按默认（两路）处理，绝不静默退化为单路
        assert active_fusion_tracks("whatever") == (PROCESS_TRACK, ROOT_TRACK)
        assert FUSION_ROOT_OR_PROCESS in FUSION_MODES


# ─── 计划级：同路 AND / 跨路 OR ───────────────────────────────────────────────


def _key_signal(signal_id: str = "k1", keyword: str = "core dump") -> dict:
    return {
        "id": signal_id,
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": keyword, "mode": "or", "expected": True, "extract": _EXTRACT},
    }


def _root_signal(signal_id: str = "root_abc", keyword: str = "KVM_EXIT_SHUTDOWN") -> dict:
    return {
        "id": signal_id,
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": [keyword], "mode": "and", "expected": True, "extract": _EXTRACT},
        "provenance": {"category": "backend"},
        "track": ROOT_TRACK,
    }


def _plan(signals: list[dict]):
    kbd = kbd_from_dict(
        {
            "id": "7",
            "support_id": "K7",
            "category_id": "虚拟机-001",
            "resource_revision": {"revision": "1"},
            "signals": signals,
        }
    )
    plan = compile_signal_plan([kbd], snapshot_id="dual-track")
    return plan, initial_assessments(plan)


def _ref(plan, signal_id: str):
    return next(ref for ref in plan.signals.values() if ref.signal_id == signal_id)


def _set(assessment, plan, signal_id: str, outcome: SignalOutcome) -> None:
    assessment.signal_outcomes[_ref(plan, signal_id).ref_id] = outcome


def test_compiler_tags_track_from_signal():
    plan, _ = _plan([_key_signal(), _root_signal()])
    assert _ref(plan, "k1").track == PROCESS_TRACK
    assert _ref(plan, "root_abc").track == ROOT_TRACK
    # 同路内仍是 MUST（required），跨路才放宽为 OR
    assert _ref(plan, "root_abc").evidence_role is EvidenceRole.MUST


def test_root_missing_keyword_does_not_reject_supported_process_track():
    """根因路未命中（NOT_RUN/CONTRADICTED）不得否决已命中的关键路。"""
    plan, assessments = _plan([_key_signal(), _root_signal()])
    _set(assessments["7"], plan, "k1", SignalOutcome.SATISFIED)
    _set(assessments["7"], plan, "root_abc", SignalOutcome.CONTRADICTED)
    reduce_candidates(plan, assessments, finalize=True)

    assert assessments["7"].track_states == {PROCESS_TRACK: S, ROOT_TRACK: R}
    assert assessments["7"].track_rejected_by == {ROOT_TRACK: "must"}
    assert assessments["7"].state is S


def test_process_contradicted_does_not_reject_supported_root_track():
    """关键路判否证不得否决已命中的根因路——这正是「根因信号可独立检索到 KBD」。"""
    plan, assessments = _plan([_key_signal(), _root_signal()])
    _set(assessments["7"], plan, "k1", SignalOutcome.CONTRADICTED)
    _set(assessments["7"], plan, "root_abc", SignalOutcome.SATISFIED)
    reduce_candidates(plan, assessments, finalize=True)

    assert assessments["7"].state is S
    assert assessments["7"].track_states[ROOT_TRACK] is S


def test_all_process_signals_must_still_be_and_within_track():
    """同路内仍是严格 AND：两条关键信号只满足一条 → 未跑完（候选）而非支持。"""
    plan, assessments = _plan([_key_signal("k1", "core dump"), _key_signal("k2", "oom-killer")])
    _set(assessments["7"], plan, "k1", SignalOutcome.SATISFIED)
    reduce_candidates(plan, assessments, finalize=False)
    assert assessments["7"].state is C

    reduce_candidates(plan, assessments, finalize=True)
    assert assessments["7"].state is INC


def test_exclude_on_either_track_rejects_whole_kbd():
    plan, assessments = _plan([_key_signal(), _root_signal()])
    _set(assessments["7"], plan, "k1", SignalOutcome.SATISFIED)
    _set(assessments["7"], plan, "root_abc", SignalOutcome.SATISFIED)
    reduce_candidates(plan, assessments, finalize=True)
    assert assessments["7"].state is S

    # 把根因路信号改判为 EXCLUDE 并令其成立 → 全局否决
    plan2, assessments2 = _plan([_key_signal(), _root_signal()])
    root_ref = _ref(plan2, "root_abc")
    object.__setattr__(root_ref, "evidence_role", EvidenceRole.EXCLUDE)
    _set(assessments2["7"], plan2, "k1", SignalOutcome.SATISFIED)
    _set(assessments2["7"], plan2, "root_abc", SignalOutcome.SATISFIED)
    reduce_candidates(plan2, assessments2, finalize=True)
    assert assessments2["7"].state is R


def test_fusion_mode_process_only_restores_single_track_behavior():
    """回滚开关：process_only 下根因路即使命中也不影响结论（与上线前一致）。"""
    plan, assessments = _plan([_key_signal(), _root_signal()])
    _set(assessments["7"], plan, "k1", SignalOutcome.CONTRADICTED)
    _set(assessments["7"], plan, "root_abc", SignalOutcome.SATISFIED)
    reduce_candidates(plan, assessments, finalize=True, fusion_mode=FUSION_PROCESS_ONLY)
    assert assessments["7"].state is R


def test_fusion_mode_root_only_isolates_root_track():
    plan, assessments = _plan([_key_signal(), _root_signal()])
    _set(assessments["7"], plan, "k1", SignalOutcome.SATISFIED)
    _set(assessments["7"], plan, "root_abc", SignalOutcome.CONTRADICTED)
    reduce_candidates(plan, assessments, finalize=True, fusion_mode=FUSION_ROOT_ONLY)
    assert assessments["7"].state is R


def test_track_reasons_are_prefixed_when_both_tracks_have_evidence():
    plan, assessments = _plan([_key_signal(), _root_signal()])
    _set(assessments["7"], plan, "k1", SignalOutcome.CONTRADICTED)
    _set(assessments["7"], plan, "root_abc", SignalOutcome.SATISFIED)
    reduce_candidates(plan, assessments, finalize=True)

    reasons = assessments["7"].reasons
    assert any(reason.startswith(f"[{PROCESS_TRACK}]") for reason in reasons)
    assert any(reason.startswith(f"[{ROOT_TRACK}]") for reason in reasons)


def test_process_only_kbd_is_byte_identical_to_legacy():
    """只有关键信号的存量 KBD：track_states 仅含 process，结论与改造前一致。"""
    plan, assessments = _plan([_key_signal()])
    _set(assessments["7"], plan, "k1", SignalOutcome.SATISFIED)
    reduce_candidates(plan, assessments, finalize=True)

    assert set(assessments["7"].track_states) == {PROCESS_TRACK}
    assert assessments["7"].state is S
    # 单路原因不加来源前缀，避免污染既有报告文案
    assert not any(reason.startswith("[") for reason in assessments["7"].reasons)
