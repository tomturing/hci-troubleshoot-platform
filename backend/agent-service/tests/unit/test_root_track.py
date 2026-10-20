"""根因链路诊断器测试：独立计划、独立调度、独立结论、变量自足。

核心不变量：
1. **独立编译**：根因链路只把 ``RootCandidate.kbd.signals``（已由编排层替换为根因信号）
   编进第二套计划；关键信号的候选对象与关键信号的发布契约都不被传递进来；
2. **变量自足**：``root_only`` 靠 ``env_context`` 派生 host/date；派生不出来的变量必须
   让信号 BLOCKED（绝不猜值，否则会把「无法判断」伪装成「已排除」）；
3. **采集复用**：``dual``/``shadow`` 下同一条命令只采集一次，根因链路复用关键链路的输出；
4. **证据链归属**：只有 ``root_only`` 写 S2/S3/S4，并存时证据链归关键链路。
"""

from __future__ import annotations

import datetime
import inspect
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.adapters.agents.htp.kbd_differential import KBDDiagnostic
from app.adapters.agents.htp.root_track import (
    ROOT_TRACK,
    RootCandidate,
    RootTrackDiagnostic,
    RootTrackResult,
    _iter_placeholders,
    _physical_key,
    build_physical_cache,
    build_root_candidates,
    derive_environment_variables,
    seed_pools_from_steps,
)
from shared.cdd import CandidateState, SignalOutcome, compile_signal_plan
from shared.cdd.candidate_reducer import initial_assessments
from shared.cdd.kbd_model import KBD

EXTRACT = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}


def _root_signal(
    signal_id: str = "rs1",
    *,
    command: str = "dmesg",
    host: str | None = "{{HOST}}",
    pattern: str = "KVM_EXIT_SHUTDOWN",
) -> dict:
    args: dict = {"command": command}
    if host is not None:
        args["host"] = host
    return {
        "id": signal_id,
        "acquire": {"tool": "qfk_system", "args": args},
        "match": {"type": "keyword", "pattern": [pattern], "mode": "and", "expected": True, "extract": EXTRACT},
        "provenance": {"category": "backend"},
    }


def _key_signal() -> dict:
    return {
        "id": "k1",
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": "core dump", "mode": "or", "expected": True, "extract": EXTRACT},
    }


def _root_case(kbd_id: str, *, roots: list[dict] | None = None) -> dict:
    """kb-service 下发的根因载荷（原始 dict）。

    刻意同时携带关键信号与关键信号的发布契约，用于验证构造根因候选时的隔离：
    只有 ``root_signals`` 能进第二套计划。
    """
    return {
        "id": kbd_id,
        "support_id": f"K{kbd_id}",
        "name": f"案例{kbd_id}",
        "category_id": "c",
        "resource_revision": {"revision": "1"},
        "signals": [_key_signal()],
        "root_signals": roots if roots is not None else [_root_signal()],
        "root_executable": True,
        "root_snapshot_id": f"rootsig-{kbd_id}",
        "root_cause": "根因",
        "solution": "方案",
        "verification_contract": {"evidence_policy": {"must": ["k1"]}},
        "generation_metadata": {"status": "stale"},
        "publish_validation": {"status": "failed"},
    }


def _cand(kbd_id: str, *, roots: list[dict] | None = None, snapshot_id: str | None = None) -> RootCandidate:
    """构造根因候选：``kbd.signals`` 已经是根因信号。"""
    case = _root_case(kbd_id, roots=roots)
    if snapshot_id is not None:
        case["root_snapshot_id"] = snapshot_id
    return build_root_candidates([case])[0]


# ── 0. 根因候选构造：与共享内核/关键信号元数据隔离 ─────────────────────────
def test_build_root_candidates_replaces_signals_and_strips_key_metadata():
    case = _root_case("7")
    cand = build_root_candidates([case])[0]

    assert [signal["id"] for signal in cand.kbd.signals] == ["rs1"]
    # 关键信号的发布门禁元数据不得带进第二套计划（否则编译期会把关键信号
    # 的 signal_id 与根因信号比对，报 references missing signal）
    assert cand.kbd.verification_contract == {}
    assert cand.kbd.generation_metadata == {}
    assert cand.kbd.publish_validation == {}
    # 输入载荷不被修改：关键链路后续仍可用它
    assert [signal["id"] for signal in case["signals"]] == ["k1"]
    assert case["verification_contract"]["evidence_policy"]["must"] == ["k1"]


def test_build_root_candidates_skips_non_executable_and_empty():
    assert build_root_candidates(None) == []
    assert build_root_candidates([]) == []
    assert build_root_candidates([_root_case("7") | {"root_executable": False}]) == []
    assert build_root_candidates([_root_case("7") | {"root_signals": []}]) == []


def _diag(**kwargs) -> RootTrackDiagnostic:
    return RootTrackDiagnostic(MagicMock(), MagicMock(), **kwargs)


async def _run(diag: RootTrackDiagnostic, **kwargs):
    events = []
    async for event in diag.diagnose_root(**kwargs):
        events.append(event)
    return events


# ── 1. 环境变量派生 ────────────────────────────────────────────────────────
@pytest.mark.parametrize("alias", ["host", "node_ip", "target_host", "ip"])
def test_derive_host_from_aliases(alias):
    assert derive_environment_variables({alias: "10.0.0.9"})["host"] == "10.0.0.9"


def test_derive_date_defaults_to_today():
    derived = derive_environment_variables({})
    assert derived["date"] == datetime.date.today().strftime("%Y-%m-%d")


def test_derive_does_not_guess_vmid():
    """没有明确 VM 标识就不派生：猜值会把「无法判断」伪装成「已排除」。"""
    assert "vmid" not in derive_environment_variables({"host": "h"})


def test_derive_vmid_from_explicit_marker():
    assert derive_environment_variables({"vmid": "9001"})["vmid"] == "9001"


def test_derive_ignores_blank_values():
    derived = derive_environment_variables({"host": "   ", "node_ip": "10.0.0.1"})
    assert derived["host"] == "10.0.0.1"


def test_derive_is_case_insensitive_on_keys():
    assert derive_environment_variables({"HOST": "10.0.0.2"})["host"] == "10.0.0.2"


# ── 2. 变量池与采集缓存播种 ────────────────────────────────────────────────
def _step(**overrides) -> SimpleNamespace:
    base = {
        "kbd_id": "7",
        "signal_id": "k1",
        "produced_variables": {"host": "10.0.0.1"},
        "tool_name": "qfk_system",
        "tool_args": {"command": "dmesg", "host": "10.0.0.1"},
        "raw_output": "KVM_EXIT_SHUTDOWN",
        "error": None,
        "ai_value": None,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_seed_pools_groups_by_cand():
    """回归钉子：曾因 step.kdb_id 拼写错误在播种时抛 AttributeError。"""
    pools = seed_pools_from_steps([_step(), _step(signal_id="k2", produced_variables={"vmid": "9001"})])
    assert pools == {"7": {"host": "10.0.0.1", "vmid": "9001"}}


def test_seed_pools_skips_blank_and_missing():
    pools = seed_pools_from_steps(
        [
            _step(produced_variables={"host": "", "vmid": None, "date": "2026-01-01"}),
            _step(produced_variables=None),
            _step(kbd_id=""),
        ]
    )
    assert pools == {"7": {"date": "2026-01-01"}}


def test_seed_pools_normalizes_variable_case():
    pools = seed_pools_from_steps([_step(produced_variables={"HOST": "10.0.0.1"})])
    assert pools == {"7": {"host": "10.0.0.1"}}


def test_physical_key_is_order_insensitive():
    assert _physical_key("qfk_system", {"a": 1, "b": 2}) == _physical_key("qfk_system", {"b": 2, "a": 1})
    assert _physical_key("qfk_system", {"a": 1}) != _physical_key("qfk_log", {"a": 1})


def test_build_physical_cache_only_keeps_successful_outputs():
    cache = build_physical_cache(
        [
            _step(),
            _step(tool_args={"command": "df"}),
            _step(tool_args={"command": "failed"}, error="boom"),
            _step(tool_args={"command": "empty"}, raw_output=None),
        ]
    )
    assert len(cache) == 2
    assert cache[_physical_key("qfk_system", {"command": "dmesg", "host": "10.0.0.1"})][0] == "KVM_EXIT_SHUTDOWN"


def test_build_physical_cache_handles_none():
    assert build_physical_cache(None) == {}


# ── 3. 占位符收集 ─────────────────────────────────────────────────────────
def test_iter_placeholders_walks_nested_args():
    signal = {
        "acquire": {
            "args": {
                "host": "{{HOST}}",
                "nested": {"path": "{{PATH}}/x"},
                "list": ["{{A}}", {"k": "{{B}}"}],
                "plain": "no placeholder",
            }
        }
    }
    assert set(_iter_placeholders(signal)) == {"HOST", "PATH", "A", "B"}


def test_iter_placeholders_tolerates_missing_acquire():
    assert _iter_placeholders({}) == []


# ── 4. 快照指纹 ──────────────────────────────────────────────────────────
def test_snapshot_id_depends_only_on_root_snapshot_ids():
    a = RootTrackDiagnostic._snapshot_id_of([_cand("7")])
    b = RootTrackDiagnostic._snapshot_id_of([_cand("7")])
    c = RootTrackDiagnostic._snapshot_id_of([_cand("8")])
    assert a == b
    assert a != c
    assert a.startswith("rootsig-")


def test_snapshot_id_falls_back_to_kbd_id():
    assert RootTrackDiagnostic._snapshot_id_of([_cand("9", snapshot_id="")]).startswith("rootsig-")


def test_unresolved_requirements_lists_placeholder_without_source():
    kbd = _cand("7", roots=[_root_signal(host="{{VMID}}")])
    gaps = RootTrackDiagnostic._requirements_without_source([kbd.kbd], {"host": "h"}, {})
    assert gaps == {"7": ["vmid"]}


def test_unresolved_requirements_cleared_by_seed_pool():
    kbd = _cand("7", roots=[_root_signal(host="{{HOST}}")])
    assert RootTrackDiagnostic._requirements_without_source([kbd.kbd], {}, {"7": {"host": "10.0.0.1"}}) == {}


# ── 5. diagnose_root 端到端 ──────────────────────────────────────────────
@pytest.mark.asyncio
async def test_no_root_candidates_skips_without_compiling():
    diag = _diag()
    events = await _run(
        diag,
        candidates=[],
        env_context={"host": "10.0.0.1"},
        session_id="s",
    )
    assert [event.stage for event in events] == ["root_diag_skipped"]
    assert events[0].metadata["reason"] == "no_root_signals"
    assert diag.root_result.plan is None  # 未编译，不是「编译失败」
    assert diag.root_result.ran is False


@pytest.mark.asyncio
async def test_root_only_runs_independently_and_supports_candidate():
    """root_only：只有根因信号也能完整跑通（关键链路不参与）。"""
    diag = _diag()
    kbd = _cand("7")
    execute = AsyncMock(return_value=("KVM_EXIT_SHUTDOWN detected", None, None, None))
    with (
        patch.object(diag, "_execute_acquirer", execute),
        patch.object(diag, "_generate_report", AsyncMock(return_value="根因报告")),
    ):
        events = await _run(
            diag,
            candidates=[kbd],
            env_context={"host": "10.0.0.1"},
            session_id="s",
            write_diagnostic_items=False,
        )

    assert diag.root_result.decision.supported_ids == ("7",)
    assert diag.root_result.assessments["7"].state is CandidateState.SUPPORTED
    assert diag.root_result.executed_acquisitions == 1
    assert diag.root_result.reused_acquisitions == 0
    # 统一结果入口同步（root_only 下它是唯一结果）
    assert diag.get_result().conclusion_level == diag.root_result.decision.level.value
    assert [event.stage for event in events][0] == "root_diag_start"
    assert events[-1].stage == "root_diag_complete"


@pytest.mark.asyncio
async def test_root_plan_compiles_only_root_signals_and_ignores_key_contract():
    """根因计划只含根因信号，且不继承关键信号的 verification_contract。"""
    diag = _diag()
    case = _root_case("7")
    cand = build_root_candidates([case])[0]
    with (
        patch.object(diag, "_execute_acquirer", AsyncMock(return_value=("KVM_EXIT_SHUTDOWN", None, None, None))),
        patch.object(diag, "_generate_report", AsyncMock(return_value="r")),
    ):
        await _run(diag, candidates=[cand], env_context={"host": "10.0.0.1"}, session_id="s")

    plan = diag.root_result.plan
    signal_ids = {ref.signal_id for ref in plan.signals.values()}
    assert signal_ids == {"rs1"}
    # 关键信号的发布契约不得造成编译错误（否则真实数据下全部根因候选 NOT_EXECUTABLE）
    assert plan.compile_errors == {}
    # 输入载荷未被修改：关键信号与关键信号的发布契约都还在原始 dict 里
    assert [signal["id"] for signal in case["signals"]] == ["k1"]
    assert case["verification_contract"]["evidence_policy"]["must"] == ["k1"]


@pytest.mark.asyncio
async def test_dual_shape_reuses_key_track_acquisition():
    """seed_steps 命中同一物理采集 → 不重复执行，reused 计数 +1。"""
    diag = _diag()
    kbd = _cand("7")
    execute = AsyncMock(side_effect=AssertionError("不应重复采集"))
    seed = _step(tool_args={"command": "dmesg", "host": "10.0.0.1"}, raw_output="KVM_EXIT_SHUTDOWN")
    with (
        patch.object(diag, "_execute_acquirer", execute),
        patch.object(diag, "_generate_report", AsyncMock(return_value="r")),
    ):
        await _run(diag, candidates=[kbd], env_context={"host": "10.0.0.1"}, session_id="s", seed_steps=[seed])

    assert diag.root_result.reused_acquisitions == 1
    assert diag.root_result.executed_acquisitions == 0
    assert diag.root_result.assessments["7"].state is CandidateState.SUPPORTED


@pytest.mark.asyncio
async def test_unresolved_vmid_blocks_signal_instead_of_guessing():
    """VMID 无来源：如实 BLOCKED → 候选 INCONCLUSIVE，绝不猜一个值。"""
    diag = _diag()
    kbd = _cand("7", roots=[_root_signal(host="{{VMID}}")])
    with (
        patch.object(diag, "_execute_acquirer", AsyncMock(side_effect=AssertionError("不应执行"))),
        patch.object(diag, "_generate_report", AsyncMock(return_value="r")),
    ):
        await _run(diag, candidates=[kbd], env_context={}, session_id="s")

    assert diag.root_result.blocked_acquisitions == 1
    assert diag.root_result.assessments["7"].state is CandidateState.INCONCLUSIVE
    assert diag.root_result.decision.supported_ids == ()
    assert diag.root_result.unresolved_requirements == {"7": ["vmid"]}
    # 每个信号结果都必须是 BLOCKED（不得降级为 CONTRADICTED，否则等于「已排除」）
    assert set(diag.root_result.assessments["7"].signal_outcomes.values()) == {SignalOutcome.BLOCKED}


@pytest.mark.asyncio
async def test_candidate_pools_are_isolated_between_kbds():
    """变量属于案例：两个 KBD 的 HOST 不同不得互相串味。

    用不同 command 让编译器分出两条 acquisition——共享内核按「工具 + 模板 args」
    分组，同模板会被合并为一次采集（既有语义，与轨道无关）。
    """
    diag = _diag()
    kbds = [
        _cand("7", roots=[_root_signal("rs1", command="dmesg", host="{{HOST}}")]),
        _cand("8", roots=[_root_signal("rs2", command="lsblk", host="{{HOST}}")]),
    ]
    # 用 seed 覆盖同名变量（真实产出优先于环境派生）
    seeds = [
        _step(kbd_id="7", tool_args={"command": "setup", "host": "10.0.0.7"}, raw_output="ok"),
        _step(kbd_id="8", tool_args={"command": "setup", "host": "10.0.0.8"}, raw_output="ok"),
    ]
    seeds[0].produced_variables = {"host": "10.0.0.7"}
    seeds[1].produced_variables = {"host": "10.0.0.8"}
    seen: list[dict] = []

    async def _capture(step, env_context, session_id, user_id, **kwargs):
        seen.append(dict(step.tool_args_template))
        return ("KVM_EXIT_SHUTDOWN", None, None, None)

    with (
        patch.object(diag, "_execute_acquirer", _capture),
        patch.object(diag, "_generate_report", AsyncMock(return_value="r")),
    ):
        await _run(diag, candidates=kbds, env_context={}, session_id="s", seed_steps=seeds)

    assert len(seen) == 2
    assert {item["host"] for item in seen} == {"10.0.0.7", "10.0.0.8"}
    assert {item["command"] for item in seen} == {"dmesg", "lsblk"}


@pytest.mark.asyncio
async def test_diagnostic_items_not_written_in_dual_shape():
    """并存时证据链归关键链路：根因链路即使有 client 也不写 S2/S3/S4。"""
    diag = _diag(diagnostic_item_client=MagicMock(), conversation_id="00000000-0000-0000-0000-000000000001")
    kbd = _cand("7")
    with (
        patch.object(diag, "_execute_acquirer", AsyncMock(return_value=("KVM_EXIT_SHUTDOWN", None, None, None))),
        patch.object(diag, "_generate_report", AsyncMock(return_value="r")),
    ):
        await _run(
            diag,
            candidates=[kbd],
            env_context={"host": "10.0.0.1"},
            session_id="s",
            write_diagnostic_items=False,
        )
    assert diag._diagnostic_item_client.batch_create_items.call_count == 0
    assert diag._diagnostic_item_client.create_item.call_count == 0


@pytest.mark.asyncio
async def test_diagnostic_items_written_in_root_only():
    """root_only：关键链路不在，根因链路承担证据链。"""
    client = MagicMock()
    client.batch_create_items = AsyncMock()
    client.create_item = AsyncMock()
    diag = _diag(diagnostic_item_client=client, conversation_id="00000000-0000-0000-0000-000000000001")
    kbd = _cand("7")
    with (
        patch.object(diag, "_execute_acquirer", AsyncMock(return_value=("KVM_EXIT_SHUTDOWN", None, None, None))),
        patch.object(diag, "_generate_report", AsyncMock(return_value="r")),
    ):
        await _run(
            diag,
            candidates=[kbd],
            env_context={"host": "10.0.0.1"},
            session_id="s",
            write_diagnostic_items=True,
        )
    assert client.batch_create_items.await_count == 1
    assert client.create_item.await_count >= 1
    stages = {call.kwargs["stage"] for call in client.create_item.await_args_list}
    assert "S3" in stages


@pytest.mark.asyncio
async def test_diagnostic_item_failure_does_not_break_diagnosis():
    client = MagicMock()
    client.batch_create_items = AsyncMock(side_effect=RuntimeError("db down"))
    client.create_item = AsyncMock(side_effect=RuntimeError("db down"))
    diag = _diag(diagnostic_item_client=client, conversation_id="00000000-0000-0000-0000-000000000001")
    with (
        patch.object(diag, "_execute_acquirer", AsyncMock(return_value=("KVM_EXIT_SHUTDOWN", None, None, None))),
        patch.object(diag, "_generate_report", AsyncMock(return_value="r")),
    ):
        await _run(
            diag,
            candidates=[_cand("7")],
            env_context={"host": "10.0.0.1"},
            session_id="s",
            write_diagnostic_items=True,
        )
    assert diag.root_result.decision.supported_ids == ("7",)


@pytest.mark.asyncio
async def test_partial_report_path_is_used_for_single_supported_of_two():
    """1 支持 + 1 无法判定 → PARTIAL，走部分报告（不调 LLM）。"""
    diag = _diag()
    kbds = [
        _cand("7"),
        _cand("8", roots=[_root_signal("rs2", command="lsblk", pattern="OTHER")]),
    ]

    async def _by_pattern(step, env_context, session_id, user_id, **kwargs):
        signal = kwargs.get("signal") or {}
        expected = ((signal.get("match") or {}).get("pattern") or [""])[0]
        if expected == "KVM_EXIT_SHUTDOWN":
            return ("KVM_EXIT_SHUTDOWN", None, None, None)
        return (None, None, None, None)  # 无法判定 → UNKNOWN → INCONCLUSIVE

    partial = MagicMock(return_value="部分报告")
    with (
        patch.object(diag, "_execute_acquirer", _by_pattern),
        patch.object(diag, "_build_partial_report", partial),
        patch.object(diag, "_generate_report", AsyncMock(return_value="完整报告")),
    ):
        await _run(diag, candidates=kbds, env_context={"host": "10.0.0.1"}, session_id="s")

    assert diag.root_result.decision.supported_ids == ("7",)
    assert diag.root_result.decision.level.value == "PARTIAL"
    assert partial.call_count == 1
    # 回归钉子：``_build_partial_report`` 在基类是**同步** staticmethod，报告必须直接是 str。
    # （若误加 await，这里会先抛 TypeError 而非断言失败）
    assert isinstance(diag.get_result().diagnosis_report, str)
    assert diag.get_result().diagnosis_report == "部分报告"


def test_report_entry_points_contract_matches_base_class():
    """两个报告入口的异步形态不同：``_generate_report`` 是 async，部分报告是同步。

    根因链路必须照基类契约调用。曾把部分报告当成 async 去 await，"真实缺陷"的结论
    其实是 e2e 的假实现写成了 async——假实现应对齐基类，而不是改产品代码。
    """
    assert inspect.iscoroutinefunction(KBDDiagnostic._generate_report)
    assert not inspect.iscoroutinefunction(KBDDiagnostic._build_partial_report)


def test_root_track_marker_is_present_only_as_label():
    """轨道标记只用于日志/指标/审计，不写进共享内核模型。"""
    assert ROOT_TRACK == "root"
    assert "track" not in KBD.model_fields
    assert "track" not in RootTrackResult.__dataclass_fields__


def test_initial_assessments_of_root_plan_are_not_key_plan():
    """同一 KBD 的两套计划是不同对象，信号集合不相交。"""
    key_kbd = KBD(id="7", support_id="K7", name="x", category_id="c", signals=[_key_signal()])
    root_kbd = build_root_candidates([_root_case("7")])[0].kbd
    key_plan = compile_signal_plan([key_kbd], snapshot_id="key")
    root_plan = compile_signal_plan([root_kbd], snapshot_id="root")
    assert key_plan is not root_plan
    assert {ref.signal_id for ref in key_plan.signals.values()} == {"k1"}
    assert {ref.signal_id for ref in root_plan.signals.values()} == {"rs1"}
    assert set(initial_assessments(key_plan)) == set(initial_assessments(root_plan)) == {"7"}


def test_evaluate_outcome_maps_blank_output_to_unknown():
    diag = _diag()
    signal = _root_signal()
    assert diag._evaluate_signal_outcome(signal, None, None, None) is SignalOutcome.UNKNOWN
    assert diag._evaluate_signal_outcome(signal, None, "boom", None) is SignalOutcome.ERROR
    assert diag._evaluate_signal_outcome(signal, "KVM_EXIT_SHUTDOWN", None, None) is SignalOutcome.SATISFIED
    assert diag._evaluate_signal_outcome(signal, "nothing", None, None) is SignalOutcome.CONTRADICTED
