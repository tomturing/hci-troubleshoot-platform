"""Active CDD scheduler and conclusion-gate invariants."""

import pytest
from app.adapters.agents.htp.cdd import (
    ActiveDiagnosticScheduler,
    CandidateState,
    CaseVerdict,
    ConclusionLevel,
    ReplayError,
    ScopeState,
    SignalEvaluation,
    SignalOutcome,
    apply_scope_results,
    compile_signal_plan,
    decide_conclusion,
    replay_evaluations,
)
from app.adapters.agents.htp.cdd.candidate_reducer import initial_assessments, reduce_candidates
from app.adapters.agents.htp.kbd_model import KBD
from shared.schemas.signal_generation import build_signal_generation_metadata, current_tool_contract_revision


def signal(
    signal_id: str,
    tool: str,
    pattern: str,
    *,
    requires=(),
    produces=(),
    required=True,
):
    args = {}
    if tool.startswith("qkv_"):
        args["keyword"] = pattern
    elif tool == "qfk_system":
        args["command"] = "ps auxf"
    return {
        "id": signal_id,
        "acquire": {"tool": tool, "args": args},
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


def test_instruction_text_does_not_split_identical_runtime_acquisition():
    first = signal("gpu-type", "qfk_system", "gpu_type=")
    second = signal("slice-type", "qfk_system", "slice_type=")
    for item, instruction in ((first, "检查 GPU 类型"), (second, "检查切分方式")):
        item["acquire"]["args"].update({
            "host": "{{HOST}}",
            "command": "cat",
            "command_args": ["/sf/cfg/gpu_info.ini"],
            "instruction": instruction,
        })
        item["orchestrate"]["requires"] = ["HOST"]

    plan = compile_signal_plan([kbd("30880", [first, second])])

    assert plan.compile_errors == {}
    assert len(plan.signals) == 2
    assert len(plan.acquisitions) == 1
    acquisition = next(iter(plan.acquisitions.values()))
    assert acquisition.args_template["instruction"] == "检查 GPU 类型"
    assert {ref.signal_id for ref in acquisition.signal_refs} == {"gpu-type", "slice-type"}


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


def _contract_kbd() -> KBD:
    signals = [
        signal("anchor", "qkv_task", "启动虚拟机", produces=("HOST",)),
        signal("detail", "qfk_system", "占用", requires=("HOST",), required=False),
        signal("normal-owner", "qfk_system", "平台正常进程", requires=("HOST",), required=False),
    ]
    return KBD(
        id="37150",
        support_id="37150",
        category_id="虚拟机-003",
        signals=signals,
        verification_contract={
            "scope": {"products": ["HCI"]},
            "evidence_policy": {
                "must": ["anchor"],
                "should": ["detail"],
                "exclude": ["normal-owner"],
                "minimum_should": 1,
            },
        },
    )


def _evaluation(ref, outcome, index):
    return SignalEvaluation(
        evaluation_id=f"eval-{index}",
        signal_ref_id=ref.ref_id,
        exec_id=f"exec-{index}",
        outcome=outcome,
    )


def test_contract_replay_confirms_only_after_must_should_and_exclude_are_resolved():
    plan = compile_signal_plan([_contract_kbd()])
    refs = {ref.signal_id: ref for ref in plan.signals.values()}

    assessments, coverage = replay_evaluations(plan, [
        _evaluation(refs["anchor"], SignalOutcome.SATISFIED, 1),
        _evaluation(refs["detail"], SignalOutcome.SATISFIED, 2),
        _evaluation(refs["normal-owner"], SignalOutcome.CONTRADICTED, 3),
    ], {"product": "HCI"})

    assert assessments["37150"].verdict is CaseVerdict.CONFIRMED
    row = coverage.candidates[0]
    assert row.must_satisfied == row.must_total == 1
    assert row.should_satisfied == row.minimum_should == 1
    assert row.exclude_cleared == row.exclude_total == 1
    assert row.observation_ratio == 1.0


def test_verification_contract_role_overrides_stale_signal_proposal_role():
    candidate = kbd("39471", [signal("backup-config", "qkv_task", "设置备份策略")])
    candidate.signals[0]["role"] = "should"
    candidate.verification_contract = {
        "scope": {"products": ["HCI"]},
        "evidence_policy": {
            "must": ["backup-config"],
            "should": [],
            "exclude": [],
            "context": [],
            "minimum_should": 0,
        },
    }

    plan = compile_signal_plan([candidate])
    ref = next(iter(plan.signals.values()))

    assert plan.compile_errors == {}
    assert ref.evidence_role.value == "must"
    assert ref.required_for_support is True


def test_satisfied_exclude_rejects_even_when_supporting_evidence_matches():
    plan = compile_signal_plan([_contract_kbd()])
    refs = {ref.signal_id: ref for ref in plan.signals.values()}

    assessments, _ = replay_evaluations(plan, [
        _evaluation(refs["anchor"], SignalOutcome.SATISFIED, 1),
        _evaluation(refs["detail"], SignalOutcome.SATISFIED, 2),
        _evaluation(refs["normal-owner"], SignalOutcome.SATISFIED, 3),
    ], {"product": "HCI"})

    assert assessments["37150"].verdict is CaseVerdict.REJECTED


def test_unknown_error_and_not_applicable_remain_inconclusive_in_contract_replay():
    for outcome in (SignalOutcome.UNKNOWN, SignalOutcome.ERROR, SignalOutcome.NOT_APPLICABLE):
        plan = compile_signal_plan([_contract_kbd()])
        anchor = next(ref for ref in plan.signals.values() if ref.signal_id == "anchor")

        assessments, coverage = replay_evaluations(
            plan,
            [_evaluation(anchor, outcome, 1)],
            {"product": "HCI"},
        )

        assert assessments["37150"].verdict is CaseVerdict.INCONCLUSIVE
        assert "anchor" in coverage.candidates[0].unresolved_signal_ids


def test_replay_rejects_duplicate_or_untraceable_evaluations():
    plan = compile_signal_plan([_contract_kbd()])
    anchor = next(ref for ref in plan.signals.values() if ref.signal_id == "anchor")
    evaluation = _evaluation(anchor, SignalOutcome.SATISFIED, 1)

    with pytest.raises(ReplayError, match="duplicate evaluation"):
        replay_evaluations(plan, [evaluation, evaluation], {"product": "HCI"})
    with pytest.raises(ReplayError, match="evaluation_id and exec_id"):
        replay_evaluations(
            plan,
            [SignalEvaluation("", anchor.ref_id, "", SignalOutcome.SATISFIED)],
            {"product": "HCI"},
        )


def test_compiler_calls_real_qfk_contract_before_scheduling():
    missing_file = signal("log", "qfk_log", "failed")
    missing_file["acquire"]["args"] = {}
    invalid = compile_signal_plan([kbd("bad", [missing_file])])

    assert "file" in " ".join(invalid.compile_errors["bad"])

    valid_log = signal("log", "qfk_log", "failed")
    valid_log["acquire"]["args"] = {"file": "sfvt_numa-server.log"}
    valid = compile_signal_plan([kbd("good", [valid_log])])

    assert "good" not in valid.compile_errors


def test_compiler_accepts_safe_blackbox_txt_log_file():
    txt_log = signal("blackbox", "qfk_log", "dropped")
    txt_log["acquire"]["args"] = {
        "file": "LOG_ethtool_statistic.txt",
        "path": "/sf/log/blackbox/today/",
    }

    plan = compile_signal_plan([kbd("blackbox", [txt_log])])

    assert "blackbox" not in plan.compile_errors


def test_solution_context_reference_exists_but_is_not_scheduled():
    diagnostic = signal("check", "qfk_system", "failed")
    solution = signal("repair", "qfk_system", "done")
    solution["orchestrate"]["phase"] = "solution"
    candidate = kbd("solution-context", [diagnostic, solution])
    candidate.verification_contract = {
        "evidence_policy": {
            "must": ["check"],
            "context": ["repair"],
        }
    }

    plan = compile_signal_plan([candidate])

    assert "solution-context" not in plan.compile_errors
    assert {ref.signal_id for ref in plan.signals.values()} == {"check"}


def test_compiler_rejects_closed_variable_dependency_cycle():
    first = signal("first", "qfk_system", "x", requires=("B",), produces=("A",))
    first["match"] = None
    second = signal("second", "qfk_system", "y", requires=("A",), produces=("B",))
    second["match"] = None

    plan = compile_signal_plan([kbd("cycle", [first, second])])

    assert "dependency cycle" in " ".join(plan.compile_errors["cycle"])


def test_contract_requires_external_variables_to_be_declared():
    consumer = signal("storage", "qfk_system", "slow", requires=("STORAGE_PATH",))
    missing = kbd("missing-external", [consumer])
    missing.verification_contract = {
        "evidence_policy": {"must": ["storage"]},
        "variables": {},
    }

    invalid = compile_signal_plan([missing])

    assert "undeclared external variables" in " ".join(
        invalid.compile_errors["missing-external"]
    )

    declared = missing.model_copy(deep=True)
    declared.id = "declared-external"
    declared.verification_contract["variables"] = {
        "STORAGE_PATH": {"type": "string"}
    }

    valid = compile_signal_plan([declared])

    assert "declared-external" not in valid.compile_errors


def test_compiler_rejects_stale_generation_or_tool_contract_revision():
    candidate = _contract_kbd()
    candidate.generation_metadata = build_signal_generation_metadata(
        source={"case": "37150"},
        prompt_template="prompt-v1",
        model_id="model-v1",
    )

    current = compile_signal_plan([candidate])
    assert candidate.id not in current.compile_errors

    candidate.generation_metadata["status"] = "stale"
    candidate.generation_metadata["tool_contract_revision"] = "0" * 64
    stale = compile_signal_plan([candidate])

    assert "generation metadata is stale" in " ".join(stale.compile_errors[candidate.id])
    assert "tool contract revision is stale" in " ".join(stale.compile_errors[candidate.id])


def test_compiler_accepts_current_expert_publish_stamp_without_overwriting_generation_origin():
    candidate = _contract_kbd()
    candidate.generation_metadata = build_signal_generation_metadata(
        source={"case": "37150"},
        prompt_template="prompt-v1",
        model_id="model-v1",
    )
    candidate.generation_metadata["tool_contract_revision"] = "0" * 64
    candidate.publish_validation = {
        "schema_version": 1,
        "status": "passed",
        "tool_contract_revision": current_tool_contract_revision(),
        "validator": "expert_publish_gate",
    }

    plan = compile_signal_plan([candidate])

    assert candidate.id not in plan.compile_errors
    assert candidate.generation_metadata["tool_contract_revision"] == "0" * 64


def test_scope_mismatch_rejects_without_becoming_signal_contradiction():
    candidate = _contract_kbd()
    plan = compile_signal_plan([candidate])
    assessments = initial_assessments(plan)

    results = apply_scope_results(
        plan,
        assessments,
        {"product": "HCI", "components": ["虚拟机"], "topology": []},
    )
    assert results["37150"].state is ScopeState.APPLICABLE

    assessments = initial_assessments(plan)
    results = apply_scope_results(
        plan,
        assessments,
        {"product": "SCP", "components": ["虚拟机"], "topology": []},
    )
    assert results["37150"].state is ScopeState.NOT_APPLICABLE
    assert assessments["37150"].verdict is CaseVerdict.REJECTED
    assert assessments["37150"].signal_outcomes == {}


def test_missing_scope_dimension_is_unknown_not_rejected():
    plan = compile_signal_plan([_contract_kbd()])
    assessments = initial_assessments(plan)

    results = apply_scope_results(plan, assessments, {})

    assert results["37150"].state is ScopeState.UNKNOWN
    assert assessments["37150"].verdict is CaseVerdict.INCONCLUSIVE

    # 后续即使错误地写入了全部满足结果，scope UNKNOWN 仍必须 fail closed。
    for ref in plan.signals.values():
        if ref.evidence_role.value in {"must", "should"}:
            assessments["37150"].signal_outcomes[ref.ref_id] = SignalOutcome.SATISFIED
        elif ref.evidence_role.value == "exclude":
            assessments["37150"].signal_outcomes[ref.ref_id] = SignalOutcome.CONTRADICTED
    reduce_candidates(plan, assessments, finalize=True)

    assert assessments["37150"].verdict is CaseVerdict.INCONCLUSIVE
