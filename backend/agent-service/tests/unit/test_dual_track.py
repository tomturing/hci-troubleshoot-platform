"""编排层测试：四种模式、无根因候选的恒等退化、报告与结果写回。

核心不变量：
1. ``process_only``：根因链路连实例都不创建（淘汰根因链路时一行改回）；
2. ``dual``/``shadow`` 但没有任何根因候选：完全退化为关键链路单跑，
   ``engine._result`` 保持原对象不变（上线双链路对绝大多数 KBD 零行为变化）；
3. ``shadow``：两路都跑，结论只取关键链路；
4. ``root_only``：关键链路不执行，结论与结果写回全部来自根因链路。

根因候选以 **kb-service 原始载荷 dict** 传入，编排层负责构造「根因形态」——
共享内核的 ``KBD`` 因此不认识任何根因字段，淘汰根因链路无需改共享内核。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from app.adapters.agents.htp.dual_track import (
    TrackRunSummary,
    diagnose_with_tracks,
    fusion_evidence,
    rebuild_process_track,
)
from app.adapters.agents.htp.kbd_differential import KBDDiagResult, StepResult
from app.adapters.agents.htp.root_track import RootTrackResult, build_root_candidates
from app.adapters.agents.htp.track_arbiter import MODE_DUAL, MODE_PROCESS_ONLY, MODE_ROOT_ONLY, MODE_SHADOW
from app.domain.agent_port import AgentStageUpdate
from shared.cdd import CandidateState, SignalOutcome, compile_signal_plan
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import KBD

EXTRACT = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}


def _signal(signal_id: str, pattern: str = "core dump") -> dict:
    return {
        "id": signal_id,
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": pattern, "mode": "or", "expected": True, "extract": EXTRACT},
    }


def _kbd(kbd_id: str = "7") -> KBD:
    """关键链路候选：共享内核的 KBD 不认识根因字段。"""
    return KBD(
        id=kbd_id,
        support_id=f"K{kbd_id}",
        name=f"案例{kbd_id}",
        category_id="c",
        resource_revision={"revision": "1"},
        signals=[_signal("k1")],
    )


def _root_case(
    kbd_id: str = "7",
    *,
    executable: bool = True,
    anchors: list[dict] | None = None,
) -> dict:
    """kb-service 下发的根因载荷（编排层的解耦边界）。

    刻意携带关键信号的 ``signals`` 与 ``verification_contract``：根因链路必须
    只用 ``root_signals`` 编译、且不得继承关键信号的发布门禁元数据。
    """
    return {
        "id": kbd_id,
        "support_id": f"K{kbd_id}",
        "name": f"案例{kbd_id}",
        "signals": [_signal("k1")],
        "root_signals": [_signal("rs1", "KVM_EXIT_SHUTDOWN")] if executable else [],
        "root_executable": executable,
        "root_snapshot_id": f"rootsig-{kbd_id}",
        "root_display_anchors": anchors or [],
        "verification_contract": {"evidence_policy": {"must": ["k1"]}},
    }


def _step(kbd_id: str = "7", signal_id: str = "k1", outcome: SignalOutcome = SignalOutcome.SATISFIED) -> StepResult:
    return StepResult(
        tool_name="qfk_system",
        tool_args={"command": "dmesg"},
        raw_output="core dump",
        error=None,
        match_kbd_ids={kbd_id} if outcome is SignalOutcome.SATISFIED else set(),
        kbd_id=kbd_id,
        signal_id=signal_id,
        outcome=outcome,
    )


def _process_result(
    *,
    steps: list | None = None,
    level: str = "DEFINITIVE",
    report: str = "关键链路报告",
    supported: list[KBD] | None = None,
) -> KBDDiagResult:
    return KBDDiagResult(
        matched_kbds=supported or [],
        steps_executed=steps if steps is not None else [],
        is_definitive=level == "DEFINITIVE",
        diagnosis_report=report,
        conclusion_level=level,
        candidate_states={},
    )


def _root_result(
    cases: list[dict],
    outcome: SignalOutcome = SignalOutcome.SATISFIED,
) -> RootTrackResult:
    """构造一份已完成归约的根因链路结果（真实 plan + assessments）。"""
    models = [item.kbd for item in build_root_candidates(cases)]
    plan = compile_signal_plan(models, snapshot_id="rootsig-test")
    assessments = initial_assessments(plan)
    for ref in plan.signals.values():
        assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = outcome
    reduce_candidates(plan, assessments, finalize=True)
    from shared.cdd import decide_conclusion

    return RootTrackResult(
        plan=plan,
        assessments=assessments,
        decision=decide_conclusion(assessments),
        kbds=models,
        snapshot_id="rootsig-test",
    )


class FakeProcessEngine:
    def __init__(self, result: KBDDiagResult | None) -> None:
        self._result = result
        self.calls: list[dict] = []

    async def diagnose(self, *, candidates, **kwargs):
        self.calls.append({"candidates": candidates, **kwargs})
        yield AgentStageUpdate(stage="kbd_diag_complete", metadata={})

    def get_result(self):
        return self._result


class FakeRootEngine:
    def __init__(self, result: RootTrackResult) -> None:
        self.root_result = result
        self.calls: list[dict] = []
        # root_only 下编排层会把本结果写入 engine._result，故此处也要有 get_result。
        self._result = KBDDiagResult(
            matched_kbds=[],
            steps_executed=[],
            is_definitive=False,
            diagnosis_report="根因链路报告",
            conclusion_level=result.decision.level.value if result.decision else "INCONCLUSIVE",
            candidate_states=result.state_map(),
        )

    async def diagnose_root(self, **kwargs):
        self.calls.append(kwargs)
        yield AgentStageUpdate(stage="root_diag_complete", metadata={})

    def get_result(self):
        return self._result


async def _run(engine, **kwargs) -> list[AgentStageUpdate]:
    events = []
    async for event in diagnose_with_tracks(engine, **kwargs):
        events.append(event)
    return events


# ── 1. rebuild_process_track：纯组件复算 ────────────────────────────────────
def test_rebuild_process_track_replays_step_results():
    kbd = _kbd("7")
    plan, assessments = rebuild_process_track([kbd], {}, [_step("7", "k1", SignalOutcome.SATISFIED)], snapshot_id="s")

    assert {ref.signal_id for ref in plan.signals.values()} == {"k1"}
    assert assessments["7"].state is CandidateState.SUPPORTED


def test_rebuild_process_track_marks_contradiction():
    kbd = _kbd("7")
    _, assessments = rebuild_process_track([kbd], {}, [_step("7", "k1", SignalOutcome.CONTRADICTED)], snapshot_id="s")
    assert assessments["7"].state is CandidateState.REJECTED


def test_rebuild_process_track_tolerates_unknown_step_shape():
    """编排层不向关键链路索取接口契约：缺属性的步骤直接跳过。"""
    kbd = _kbd("7")
    _, assessments = rebuild_process_track(
        [kbd], {}, [SimpleNamespace(whatever=1), {"kbd_id": "7"}], snapshot_id="s"
    )
    assert assessments["7"].state is CandidateState.INCONCLUSIVE


def test_rebuild_process_track_without_steps_is_inconclusive():
    kbd = _kbd("7")
    _, assessments = rebuild_process_track([kbd], {}, [], snapshot_id="s")
    assert assessments["7"].state is CandidateState.INCONCLUSIVE


@pytest.mark.asyncio
async def test_rebuild_reproduces_real_diagnose_states(monkeypatch):
    """复算必须与关键链路真实收尾等价（状态集合与支持集逐一相同）。

    这是「零侵入」的代价侧守卫：编排层为了不改 ``kbd_differential`` 而复算关键
    链路的 plan/assessments，一旦关键链路收尾语义变化（新增状态、调整 finalize
    规则），本用例会先炸，而不是让 dual 模式的融合结论悄悄漂移。
    """
    from app.adapters.agents.htp.kbd_differential import KBDDiagnostic

    # 7 命中必须信号；8 被反证；9 依赖缺失变量（只能 BLOCKED）。
    supported = _kbd("7")
    rejected = _kbd("8")
    rejected.signals = [_signal("k1", "KVM_EXIT_SHUTDOWN")]
    blocked = _kbd("9")
    blocked.signals = [{**_signal("k1"), "requires": ["vmid"]}]
    candidates = [supported, rejected, blocked]

    diag = KBDDiagnostic(ai_registry=None, tool_executor=None)

    async def _execute(step, env_context, session_id, user_id, *, signal=None, exec_id=None):
        # 现场固定输出「core dump」：7 的 pattern 命中，8 的 pattern 被反证。
        return ("core dump", None, None, None)

    async def _report(*args, **kwargs):
        return "关键链路报告"

    monkeypatch.setattr(diag, "_execute_acquirer", _execute)
    monkeypatch.setattr(diag, "_generate_report", _report)

    _events = [event async for event in diag.diagnose(candidates, {}, "rebuild-equivalence")]
    result = diag.get_result()
    assert result is not None

    plan, assessments = rebuild_process_track(
        candidates, {}, list(result.steps_executed), snapshot_id="runtime"
    )
    rebuilt_states = {kbd_id: item.state.value for kbd_id, item in assessments.items()}

    # 1) 状态逐一等价（含 finalize 后的 INCONCLUSIVE 归因）。
    assert rebuilt_states == result.candidate_states
    # 2) 支持集等价（复算结果交给融合时不得多命中/少命中）。
    from shared.cdd import decide_conclusion

    assert set(decide_conclusion(assessments).supported_ids) == {str(kbd.id) for kbd in result.matched_kbds}
    # 3) 计划编译错误也等价：复算不能把编译不过的候选算成可执行。
    assert dict(plan.compile_errors) == {}


# ── 2. process_only：根因链路完全不参与 ────────────────────────────────────
@pytest.mark.asyncio
async def test_process_only_never_builds_root_engine():
    kbd = _kbd("7")
    engine = FakeProcessEngine(_process_result(steps=[_step()]))
    factory_calls = 0

    def _factory():
        nonlocal factory_calls
        factory_calls += 1
        return FakeRootEngine(_root_result([_root_case("7")]))

    events = await _run(
        engine,
        candidates=[kbd],
        root_cases=[_root_case("7")],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_PROCESS_ONLY,
        root_engine_factory=_factory,
    )

    assert factory_calls == 0
    assert [e.stage for e in events] == ["kbd_diag_complete"]
    # 结果对象保持原样（未被重建/改写）
    assert engine.get_result().diagnosis_report == "关键链路报告"


@pytest.mark.asyncio
async def test_process_only_does_not_append_root_anchors():
    anchors = [{"signal_text": "低微码", "keywords": ["低微码"], "match_type": "keyword"}]
    engine = FakeProcessEngine(_process_result(steps=[_step()]))
    await _run(
        engine,
        candidates=[_kbd("7")],
        root_cases=[_root_case("7", executable=False, anchors=anchors)],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_PROCESS_ONLY,
    )
    assert "根因特征（请人工核对）" not in engine.get_result().diagnosis_report


# ── 3. 无根因候选：dual/shadow 退化为关键链路单跑 ──────────────────────────
@pytest.mark.asyncio
@pytest.mark.parametrize("mode", [MODE_DUAL, MODE_SHADOW])
async def test_without_root_candidates_degrades_to_process_only(mode):
    """上线双链路对绝大多数 KBD 必须零行为变化：连结果对象都不重建。"""
    kbd = _kbd("7")
    process_result = _process_result(steps=[_step()], report="原始报告")
    engine = FakeProcessEngine(process_result)
    factory_calls = 0

    def _factory():
        nonlocal factory_calls
        factory_calls += 1
        return FakeRootEngine(_root_result([_root_case("7")]))

    await _run(
        engine,
        candidates=[kbd],
        root_cases=[],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=mode,
        root_engine_factory=_factory,
    )

    assert factory_calls == 0
    assert engine.get_result() is process_result  # 同一对象，未被重建


# ── 4. root_only：关键链路不执行 ──────────────────────────────────────────
@pytest.mark.asyncio
async def test_root_only_skips_process_track_and_writes_root_result():
    kbd = _kbd("7")
    engine = FakeProcessEngine(_process_result(steps=[_step()]))
    root_engine = FakeRootEngine(_root_result([_root_case("7")]))

    events = await _run(
        engine,
        candidates=[kbd],
        root_cases=[_root_case("7")],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_ROOT_ONLY,
        root_engine_factory=lambda: root_engine,
    )

    assert engine.calls == []  # 关键链路根本没被调用
    assert [e.stage for e in events] == ["root_diag_complete"]
    assert engine.get_result() is root_engine.get_result()
    assert root_engine.calls[0]["write_diagnostic_items"] is True


# ── 5. shadow：两路都跑，结论只取关键链路 ────────────────────────────────
@pytest.mark.asyncio
async def test_shadow_runs_both_but_keeps_process_conclusion():
    kbd = _kbd("7")
    process_result = _process_result(steps=[_step("7", "k1", SignalOutcome.CONTRADICTED)], level="INCONCLUSIVE")
    engine = FakeProcessEngine(process_result)
    root_engine = FakeRootEngine(_root_result([_root_case("7")], SignalOutcome.SATISFIED))

    events = await _run(
        engine,
        candidates=[kbd],
        root_cases=[_root_case("7")],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_SHADOW,
        root_engine_factory=lambda: root_engine,
    )

    assert len(engine.calls) == 1 and len(root_engine.calls) == 1
    assert [e.stage for e in events] == ["kbd_diag_complete", "root_diag_complete"]
    # 结论保持关键链路（INCONCLUSIVE），根因链路的 SUPPORTED 不影响线上判定
    assert engine.get_result().conclusion_level == "INCONCLUSIVE"
    # 并存时证据链归关键链路
    assert root_engine.calls[0]["write_diagnostic_items"] is False


# ── 6. dual：融合结论写回 ────────────────────────────────────────────────
@pytest.mark.asyncio
async def test_dual_fuses_and_writes_back_conclusion():
    kbd = _kbd("7")
    process_result = _process_result(steps=[_step("7", "k1", SignalOutcome.CONTRADICTED)], level="INCONCLUSIVE")
    engine = FakeProcessEngine(process_result)
    root_engine = FakeRootEngine(_root_result([_root_case("7")], SignalOutcome.SATISFIED))

    await _run(
        engine,
        candidates=[kbd],
        root_cases=[_root_case("7")],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_DUAL,
        root_engine_factory=lambda: root_engine,
    )

    fused = engine.get_result()
    assert fused.conclusion_level in {"DEFINITIVE", "PARTIAL"}
    assert [kbd.id for kbd in fused.matched_kbds] == ["7"]
    assert fused.diagnosis_report.startswith("关键链路报告")
    assert "根因信号链路（并行策略）核对结果" in fused.diagnosis_report


@pytest.mark.asyncio
async def test_dual_appends_text_anchors_to_report():
    anchors = [{"signal_text": "低微码致VM关机", "keywords": ["低微码"], "match_type": "keyword"}]
    engine = FakeProcessEngine(_process_result(steps=[_step()]))
    root_engine = FakeRootEngine(_root_result([_root_case("7", anchors=anchors)]))

    await _run(
        engine,
        candidates=[_kbd("7")],
        root_cases=[_root_case("7", anchors=anchors)],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_DUAL,
        root_engine_factory=lambda: root_engine,
    )

    assert "根因特征（请人工核对）" in engine.get_result().diagnosis_report
    # 锚点只是文本展示，绝不进入 candidate_states / 结论
    assert engine.get_result().candidate_states


@pytest.mark.asyncio
async def test_text_only_root_case_appends_anchors_without_running_root_track():
    """只有 text 根因信号的 KBD：不编译第二套计划，但锚点仍必须展示。"""
    anchors = [{"signal_text": "低微码致VM关机", "keywords": ["低微码"], "match_type": "keyword"}]
    engine = FakeProcessEngine(_process_result(steps=[_step()]))
    factory_calls = 0

    def _factory():
        nonlocal factory_calls
        factory_calls += 1
        return FakeRootEngine(_root_result([]))

    await _run(
        engine,
        candidates=[_kbd("7")],
        root_cases=[_root_case("7", executable=False, anchors=anchors)],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_DUAL,
        root_engine_factory=_factory,
    )

    assert factory_calls == 0
    assert "根因特征（请人工核对）" in engine.get_result().diagnosis_report


@pytest.mark.asyncio
async def test_dual_passes_seed_steps_to_root_track():
    steps = [_step("7", "k1", SignalOutcome.SATISFIED)]
    engine = FakeProcessEngine(_process_result(steps=steps))
    root_engine = FakeRootEngine(_root_result([_root_case("7")]))

    await _run(
        engine,
        candidates=[_kbd("7")],
        root_cases=[_root_case("7")],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_DUAL,
        root_engine_factory=lambda: root_engine,
    )
    assert root_engine.calls[0]["seed_steps"] is steps


# ── 7. 隔离：根因链路的候选集与关键链路分开 ──────────────────────────────
@pytest.mark.asyncio
async def test_root_engine_receives_only_root_executable_cases():
    engine = FakeProcessEngine(_process_result(steps=[_step()]))
    root_engine = FakeRootEngine(_root_result([_root_case("7")]))

    await _run(
        engine,
        candidates=[_kbd("7"), _kbd("8")],
        root_cases=[_root_case("7"), _root_case("8", executable=False)],
        env_context={"host": "10.0.0.1"},
        session_id="s",
        mode=MODE_DUAL,
        root_engine_factory=lambda: root_engine,
    )
    assert [item.kbd.id for item in root_engine.calls[0]["candidates"]] == ["7"]
    # 关键链路仍拿到全量候选
    assert [kbd.id for kbd in engine.calls[0]["candidates"]] == ["7", "8"]


@pytest.mark.asyncio
async def test_root_failure_does_not_break_process_result(monkeypatch):
    """根因链路抛错不得吞掉关键链路已产出的结论。"""
    process_result = _process_result(steps=[_step()], report="关键链路报告")
    engine = FakeProcessEngine(process_result)

    class ExplodingRoot:
        root_result = RootTrackResult()

        async def diagnose_root(self, **kwargs):
            raise RuntimeError("root boom")
            yield  # pragma: no cover

        def get_result(self):
            return None

    with pytest.raises(RuntimeError):
        await _run(
            engine,
            candidates=[_kbd("7")],
            root_cases=[_root_case("7")],
            env_context={"host": "10.0.0.1"},
            session_id="s",
            mode=MODE_DUAL,
            root_engine_factory=lambda: ExplodingRoot(),
        )
    # 关键链路结果仍然完好（未被清空/改写）
    assert engine.get_result() is process_result


# ── 8. 可观测摘要 ────────────────────────────────────────────────────────
def test_track_run_summary_is_json_safe():
    summary = TrackRunSummary(mode=MODE_DUAL, ran_tracks=("process", "root"), conclusion_tracks=("process", "root"))
    payload = summary.as_dict()
    assert payload["mode"] == MODE_DUAL
    assert payload["ran_tracks"] == ["process", "root"]
    assert payload["root_unresolved_requirements"] == {}


def test_fusion_evidence_empty_when_no_fusion():
    assert fusion_evidence(None) == ""
