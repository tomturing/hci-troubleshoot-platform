"""Active CDD scheduler and conclusion-gate invariants."""

from app.adapters.agents.htp.cdd import (
    ActiveDiagnosticScheduler,
    CandidateState,
    ConclusionLevel,
    SignalOutcome,
    compile_signal_plan,
    decide_conclusion,
)
from app.adapters.agents.htp.cdd.candidate_reducer import initial_assessments, reduce_candidates
from app.adapters.agents.htp.kbd_model import KBD


def signal(
    signal_id: str,
    tool: str,
    pattern: str,
    *,
    requires=(),
    produces=(),
    required=True,
):
    return {
        "id": signal_id,
        "acquire": {"tool": tool, "args": {}},
        "match": {"type": "keyword", "pattern": pattern, "expected": True},
        "required_for_support": required,
        "orchestrate": {
            "phase": "diagnostic",
            "requires": list(requires),
            "produces": [{"name": name, "path": name.lower()} for name in produces],
        },
    }


def kbd(kbd_id: str, signals: list[dict]) -> KBD:
    return KBD(id=kbd_id, support_id=kbd_id, category_id="虚拟机-003", signals=signals)


def test_producer_unlock_value_schedules_before_consumer():
    plan = compile_signal_plan(
        [kbd("1", [signal("producer", "qkv_task", "x", produces=("HOST",)), signal("consumer", "qfk_system", "y", requires=("HOST",))])]
    )
    assessments = initial_assessments(plan)
    scheduler = ActiveDiagnosticScheduler(plan)

    selected = scheduler.choose(assessments, set())

    assert selected is not None
    assert selected[0].tool_name == "qkv_task"
    assert selected[1].unlock == 1


def test_discriminating_shared_acquisition_wins_over_plain_coverage():
    candidates = [
        kbd("1", [signal("shared-1", "shared", "A"), signal("plain-1", "plain", "same")]),
        kbd("2", [signal("shared-2", "shared", "B"), signal("plain-2", "plain", "same")]),
    ]
    plan = compile_signal_plan(candidates)
    selected = ActiveDiagnosticScheduler(plan).choose(initial_assessments(plan), set())

    assert selected is not None
    assert selected[0].tool_name == "shared"
    assert selected[1].discrimination > 0


def test_required_fail_rejects_candidate_and_removes_exclusive_work():
    plan = compile_signal_plan([kbd("1", [signal("a", "first", "x"), signal("b", "exclusive", "y")])])
    assessments = initial_assessments(plan)
    first_ref = next(ref for ref in plan.signals.values() if ref.signal_id == "a")
    assessments["1"].signal_outcomes[first_ref.ref_id] = SignalOutcome.FAIL

    reduce_candidates(plan, assessments)
    selected = ActiveDiagnosticScheduler(plan).choose(assessments, set())

    assert assessments["1"].state is CandidateState.REJECTED
    assert selected is None


def test_shared_acquisition_remains_for_other_active_candidate():
    candidates = [
        kbd("1", [signal("reject", "first", "x"), signal("shared-1", "shared", "same")]),
        kbd("2", [signal("shared-2", "shared", "same")]),
    ]
    plan = compile_signal_plan(candidates)
    assessments = initial_assessments(plan)
    reject_ref = next(ref for ref in plan.signals.values() if ref.signal_id == "reject")
    assessments["1"].signal_outcomes[reject_ref.ref_id] = SignalOutcome.FAIL

    reduce_candidates(plan, assessments)
    selected = ActiveDiagnosticScheduler(plan).choose(assessments, set())

    assert assessments["1"].state is CandidateState.REJECTED
    assert selected is not None
    assert selected[0].tool_name == "shared"
    assert [ref.kbd_id for ref in selected[0].signal_refs] == ["1", "2"]


def test_unknown_error_and_blocked_never_support_or_reject():
    for outcome in (SignalOutcome.UNKNOWN, SignalOutcome.ERROR, SignalOutcome.BLOCKED):
        plan = compile_signal_plan([kbd("1", [signal("required", "a", "x")])])
        assessments = initial_assessments(plan)
        ref = next(iter(plan.signals.values()))
        assessments["1"].signal_outcomes[ref.ref_id] = outcome

        reduce_candidates(plan, assessments, finalize=True)

        assert assessments["1"].state is CandidateState.INCONCLUSIVE
        assert decide_conclusion(assessments).level is ConclusionLevel.INCONCLUSIVE


def test_optional_signal_does_not_block_support():
    plan = compile_signal_plan(
        [kbd("1", [signal("required", "a", "x"), signal("optional", "b", "y", required=False)])]
    )
    assessments = initial_assessments(plan)
    required_ref = next(ref for ref in plan.signals.values() if ref.signal_id == "required")
    assessments["1"].signal_outcomes[required_ref.ref_id] = SignalOutcome.PASS

    reduce_candidates(plan, assessments)

    assert assessments["1"].state is CandidateState.SUPPORTED


def test_supported_plus_unresolved_is_partial_not_definitive():
    plan = compile_signal_plan([kbd("1", [signal("a", "a", "x")]), kbd("2", [signal("b", "b", "y")])])
    assessments = initial_assessments(plan)
    ref1 = next(ref for ref in plan.signals.values() if ref.kbd_id == "1")
    ref2 = next(ref for ref in plan.signals.values() if ref.kbd_id == "2")
    assessments["1"].signal_outcomes[ref1.ref_id] = SignalOutcome.PASS
    assessments["2"].signal_outcomes[ref2.ref_id] = SignalOutcome.ERROR

    reduce_candidates(plan, assessments, finalize=True)
    decision = decide_conclusion(assessments)

    assert decision.level is ConclusionLevel.PARTIAL


def test_supported_plus_rejected_is_definitive():
    plan = compile_signal_plan([kbd("1", [signal("a", "a", "x")]), kbd("2", [signal("b", "b", "y")])])
    assessments = initial_assessments(plan)
    refs = {ref.kbd_id: ref for ref in plan.signals.values()}
    assessments["1"].signal_outcomes[refs["1"].ref_id] = SignalOutcome.PASS
    assessments["2"].signal_outcomes[refs["2"].ref_id] = SignalOutcome.FAIL

    reduce_candidates(plan, assessments, finalize=True)
    decision = decide_conclusion(assessments)

    assert decision.level is ConclusionLevel.DEFINITIVE


def test_all_rejected_is_no_match():
    plan = compile_signal_plan([kbd("1", [signal("a", "a", "x")])])
    assessments = initial_assessments(plan)
    ref = next(iter(plan.signals.values()))
    assessments["1"].signal_outcomes[ref.ref_id] = SignalOutcome.FAIL

    reduce_candidates(plan, assessments, finalize=True)

    assert decide_conclusion(assessments).level is ConclusionLevel.NO_MATCH


def test_candidate_input_order_does_not_change_plan_or_schedule():
    one = kbd("1", [signal("a", "shared", "A")])
    two = kbd("2", [signal("b", "shared", "B")])
    plan_a = compile_signal_plan([one, two], snapshot_id="same")
    plan_b = compile_signal_plan([two, one], snapshot_id="same")
    selected_a = ActiveDiagnosticScheduler(plan_a).choose(initial_assessments(plan_a), set())
    selected_b = ActiveDiagnosticScheduler(plan_b).choose(initial_assessments(plan_b), set())

    assert plan_a.plan_id == plan_b.plan_id
    assert selected_a is not None and selected_b is not None
    assert selected_a[0].template_key == selected_b[0].template_key
