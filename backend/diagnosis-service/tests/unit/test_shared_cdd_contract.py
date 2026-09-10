"""在线 ORM 与离线 SQL 共用 CDD 模型的输入兼容测试。"""

import pytest
from shared.cdd import (
    Acquisition,
    AcquisitionRunResult,
    CandidateAssessment,
    CandidateState,
    ConclusionLevel,
    EvidenceRole,
    ProducedVariable,
    SignalOutcome,
    SignalPlan,
    SignalRef,
    compile_signal_plan,
    decide_conclusion,
    execute_acquisition_plan,
)
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import kbd_from_dict


def test_kbd_model_normalizes_database_identity_types():
    """整数主键和 NULL 分类必须归一为稳定字符串身份。"""

    kbd = kbd_from_dict(
        {
            "id": 587,
            "support_id": 27123,
            "name": "测试 KBD",
            "category_id": None,
            "signals": [],
        }
    )
    assert kbd.id == "587"
    assert kbd.support_id == "27123"
    assert kbd.category_id == ""


@pytest.mark.asyncio
async def test_provider_results_use_compiled_signal_ref_identity():
    """Provider 必须按编译后的 kbd/revision/signal 引用回填结果。"""

    kbd = kbd_from_dict(
        {
            "id": 587,
            "support_id": "K1",
            "resource_revision": {"revision": 3},
            "signals": [{"id": "s1", "acquire": {"tool": "qkv_task", "args": {"keyword": "失败"}}}],
        }
    )
    plan = compile_signal_plan([kbd], snapshot_id="frozen")
    assessments = initial_assessments(plan)
    ref = next(iter(plan.signals.values()))

    class Provider:
        async def acquire(self, _acquisition, *, variable_values):
            assert variable_values == {}
            return AcquisitionRunResult(outcomes={ref.ref_id: SignalOutcome.SATISFIED})

    await execute_acquisition_plan(plan, assessments, Provider())
    reduce_candidates(plan, assessments, finalize=True)

    assert ref.ref_id == "587/3/s1"
    assert assessments["587"].state is CandidateState.SUPPORTED


@pytest.mark.asyncio
async def test_provider_passes_produced_value_and_evidence_provenance_to_dependent_acquisition():
    """下游采集只能消费生产者的实际值，且来源必须随值一起传递。"""

    kbd = kbd_from_dict(
        {
            "id": 588,
            "support_id": "K2",
            "resource_revision": {"revision": 1},
            "signals": [
                {
                    "id": "producer",
                    "acquire": {"tool": "qkv_task", "args": {"keyword": "迁移"}},
                    "orchestrate": {"produces": [{"name": "HOST", "path": "host"}]},
                },
                {
                    "id": "consumer",
                    "acquire": {
                        "tool": "qfk_log",
                        "args": {"resource_keyword": "迁移", "file": "migration.log", "host": "{{HOST}}"},
                    },
                    "match": {"type": "keyword", "pattern": "done", "expected": True},
                    "orchestrate": {"requires": ["HOST"]},
                },
            ],
        }
    )
    plan = compile_signal_plan([kbd], snapshot_id="value-flow")
    assessments = initial_assessments(plan)
    seen_values: list[dict[str, ProducedVariable]] = []

    class Provider:
        async def acquire(self, acquisition, *, variable_values):
            seen_values.append(dict(variable_values))
            ref = acquisition.signal_refs[0]
            if ref.signal_id == "producer":
                return AcquisitionRunResult(
                    outcomes={ref.ref_id: SignalOutcome.SATISFIED},
                    produced_values={
                        "host": ProducedVariable(
                            value="node-a",
                            evidence_refs=("evidence-producer",),
                            source_signal_refs=(ref.ref_id,),
                        )
                    },
                )
            return AcquisitionRunResult(outcomes={ref.ref_id: SignalOutcome.SATISFIED})

    await execute_acquisition_plan(plan, assessments, Provider())

    assert [set(values) for values in seen_values] == [set(), {"host"}]
    assert seen_values[1]["host"].value == "node-a"
    assert seen_values[1]["host"].evidence_refs == ("evidence-producer",)


@pytest.mark.asyncio
async def test_conflicting_produced_value_stays_latched_and_blocks_dependent_acquisition():
    """同名变量冲突后不得因后续结果或执行顺序重新解锁消费者。"""

    producer = SignalRef(
        ref_id="1/1/producer", kbd_id="1", support_id="K3", revision="1", signal_id="producer",
        signal={}, required_for_support=True, evidence_role=EvidenceRole.MUST, failure_effect="reject",
        requires=(), produces=("host",), phase="diagnostic", matcher_fingerprint="producer",
    )
    consumer = SignalRef(
        ref_id="1/1/consumer", kbd_id="1", support_id="K3", revision="1", signal_id="consumer",
        signal={}, required_for_support=True, evidence_role=EvidenceRole.MUST, failure_effect="reject",
        requires=("host",), produces=(), phase="diagnostic", matcher_fingerprint="consumer",
    )
    producer_acquisition = Acquisition(
        template_key="producer", tool_name="qkv_task", args_template={}, signal_refs=[producer], produces={"host"},
    )
    consumer_acquisition = Acquisition(
        template_key="consumer", tool_name="qfk_log", args_template={}, signal_refs=[consumer], requires={"host"},
    )
    plan = SignalPlan(
        plan_id="conflict", category_id="", snapshot_id="snapshot", candidates={},
        signals={producer.ref_id: producer, consumer.ref_id: consumer},
        acquisitions={"producer": producer_acquisition, "consumer": consumer_acquisition},
    )
    assessments = {"1": CandidateAssessment(kbd_id="1")}
    calls: list[str] = []

    class Provider:
        async def acquire(self, acquisition, *, variable_values):
            calls.append(acquisition.template_key)
            assert acquisition.template_key == "producer"
            return AcquisitionRunResult(
                outcomes={producer.ref_id: SignalOutcome.SATISFIED},
                produced_values={"host": ProducedVariable(value="node-new")},
                evaluations=(
                    {
                        "support_id": "K3",
                        "signal_id": "kbd:K3:producer",
                        "state": "MATCHED",
                        "reason": "生产变量已提取",
                        "required_for_conclusion": True,
                        "evidence_status": "available",
                        "evidence_refs": [],
                        "matcher_snapshot": {},
                    },
                ),
            )

    variables, evaluations = await execute_acquisition_plan(
        plan,
        assessments,
        Provider(),
        available_variable_values={"host": ProducedVariable(value="node-old")},
    )

    assert calls == ["producer"]
    assert "host" not in variables
    assert assessments["1"].signal_outcomes[consumer.ref_id] is SignalOutcome.BLOCKED
    assert evaluations[0]["variable_pool_conflicts"] == ["host"]


def test_multiple_supported_candidates_are_not_definitive():
    """多个同时受支持的根因不能进入唯一 Confirmed 门禁。"""

    assessments = {
        "1": CandidateAssessment(kbd_id="1", state=CandidateState.SUPPORTED),
        "2": CandidateAssessment(kbd_id="2", state=CandidateState.SUPPORTED),
    }

    assert decide_conclusion(assessments).level is ConclusionLevel.PARTIAL
