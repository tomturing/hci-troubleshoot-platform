"""在线 ORM 与离线 SQL 共用 CDD 模型的输入兼容测试。"""

import pytest
from shared.cdd import (
    AcquisitionRunResult,
    CandidateAssessment,
    CandidateState,
    ConclusionLevel,
    SignalOutcome,
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
        async def acquire(self, _acquisition):
            return AcquisitionRunResult(outcomes={ref.ref_id: SignalOutcome.SATISFIED})

    await execute_acquisition_plan(plan, assessments, Provider())
    reduce_candidates(plan, assessments, finalize=True)

    assert ref.ref_id == "587/3/s1"
    assert assessments["587"].state is CandidateState.SUPPORTED


def test_multiple_supported_candidates_are_not_definitive():
    """多个同时受支持的根因不能进入唯一 Confirmed 门禁。"""

    assessments = {
        "1": CandidateAssessment(kbd_id="1", state=CandidateState.SUPPORTED),
        "2": CandidateAssessment(kbd_id="2", state=CandidateState.SUPPORTED),
    }

    assert decide_conclusion(assessments).level is ConclusionLevel.PARTIAL
