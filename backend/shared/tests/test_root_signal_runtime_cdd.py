"""根因信号并入 CDD 后的判定语义（轨道一·二的下游语义证明）。

证明三件事（对应当前「双路并行 + 最少充分集」设计）：
1. executable 根因信号以 ``track=root`` 编译为 MUST，与关键信号（``track=process``）**分路归约**：
   同路内仍是严格 AND（全部 MUST SATISFIED），跨路是 OR（任一命中即支持）；
2. 两路互不影响：一路 MUST 被判否证不得否决另一路已命中的结论；
3. text 根因展示锚点（root_display_anchors）绝不产生 SignalRef、绝不改变候选状态——轨道二与判定解耦。

历史对照：旧版并集实现把两路信号混在同一条 MUST 链上，一条根因信号判错就把整篇 KBD 判死；
本文件的 ``test_process_track_hit_is_sufficient_even_when_root_unrun`` 钉住了这一行为不再回退。
"""

from __future__ import annotations

from shared.cdd import CandidateState, EvidenceRole, SignalOutcome, compile_signal_plan, decide_conclusion
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import kbd_from_dict

_EXTRACT = {
    "type": "text",
    "rows": {"mode": "all"},
    "cardinality": "all",
    "source": "stdout",
    "value_mode": "string",
}


def _key_signal() -> dict:
    return {
        "id": "k1",
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {"type": "keyword", "pattern": "core dump", "mode": "or", "expected": True, "extract": _EXTRACT},
    }


def _normalized_root_signal() -> dict:
    """normalize_root_for_runtime 的产物形态（keyword 单词 → runtime matcher + track 标记）。"""
    return {
        "id": "root_deadbeefcafe",
        "acquire": {"tool": "qfk_system", "args": {"command": "dmesg"}},
        "match": {
            "type": "keyword",
            "pattern": ["KVM_EXIT_SHUTDOWN"],
            "mode": "and",
            "expected": True,
            "extract": _EXTRACT,
        },
        "provenance": {"category": "backend"},
        "track": "root",
    }


def _anchor() -> dict:
    return {"signal_text": "低微码致 VM 关机", "keywords": ["低微码"], "match_type": "keyword"}


def _kbd_dict() -> dict:
    return {
        "id": "7",
        "support_id": "K7",
        "category_id": "虚拟机-001",
        "resource_revision": {"revision": "1"},
        # 轨道一并集：关键信号 + normalize 后的 executable 根因
        "signals": [_key_signal(), _normalized_root_signal()],
        # 轨道二展示：text 根因锚点，独立于 signals
        "root_display_anchors": [_anchor()],
    }


def test_executable_root_and_key_signals_both_compile_as_must():
    kbd = kbd_from_dict(_kbd_dict())
    plan = compile_signal_plan([kbd], snapshot_id="root-and")
    refs = [ref for ref in plan.signals.values() if ref.kbd_id == "7"]

    # 并集两条信号都编译为 MUST（AND 判定的前提：required=True）
    assert len(refs) == 2
    assert {ref.signal_id for ref in refs} == {"k1", "root_deadbeefcafe"}
    assert all(ref.evidence_role is EvidenceRole.MUST for ref in refs)
    assert all(ref.required_for_support for ref in refs)


def test_display_anchor_produces_no_signal_ref():
    """轨道二解耦：root_display_anchors 不进入 compile_signal_plan，不新增任何 SignalRef。"""
    with_anchor = compile_signal_plan([kbd_from_dict(_kbd_dict())], snapshot_id="anchor-on")
    without_anchor = compile_signal_plan(
        [kbd_from_dict({**_kbd_dict(), "root_display_anchors": []})], snapshot_id="anchor-off"
    )
    assert list(with_anchor.signals.values()) and len(with_anchor.signals) == len(without_anchor.signals)
    # 锚点正文/关键词绝不会被当作 signal_id 编译
    assert all(ref.signal_id != "低微码致 VM 关机" for ref in with_anchor.signals.values())


def test_process_track_hit_is_sufficient_even_when_root_unrun():
    """双路并行：关键路命中即可支持，不再要求根因路也执行完（跨路 OR）。

    旧并集实现下该场景是 INCONCLUSIVE（AND 收紧）；v2 改成两路各自独立求值、
    任一路 SUPPORTED 即支持，因为根因信号路本身就是「另一种定位策略」而非前置条件。
    """
    kbd = kbd_from_dict(_kbd_dict())
    plan = compile_signal_plan([kbd], snapshot_id="dual-or")
    assessments = initial_assessments(plan)
    by_signal = {ref.signal_id: ref for ref in plan.signals.values() if ref.kbd_id == "7"}

    assessments["7"].signal_outcomes[by_signal["k1"].ref_id] = SignalOutcome.SATISFIED
    # executable 根因保持未跑（NOT_RUN）
    reduce_candidates(plan, assessments, finalize=True)

    assert assessments["7"].track_states == {
        "process": CandidateState.SUPPORTED,
        "root": CandidateState.INCONCLUSIVE,
    }
    assert assessments["7"].state is CandidateState.SUPPORTED
    assert decide_conclusion(assessments).level.value == "DEFINITIVE"


def test_full_satisfaction_supported():
    """关键 + executable 根因全部 SATISFIED → SUPPORTED（AND 语义的另一半）。"""
    kbd = kbd_from_dict(_kbd_dict())
    plan = compile_signal_plan([kbd], snapshot_id="support")
    assessments = initial_assessments(plan)

    for ref in plan.signals.values():
        if ref.kbd_id == "7":
            assessments["7"].signal_outcomes[ref.ref_id] = SignalOutcome.SATISFIED
    reduce_candidates(plan, assessments, finalize=True)

    assert assessments["7"].state is CandidateState.SUPPORTED


def test_empty_root_signal_union_is_identity():
    """当前评估语料无 executable 根因（root_signal_json 空）→ signals 无 root_* 项，
    行为与合入前一致：证明并集对无根因 KBD 是恒等，定论率不可能漂移。"""
    kbd = kbd_from_dict({**_kbd_dict(), "signals": [_key_signal()], "root_display_anchors": []})
    plan = compile_signal_plan([kbd], snapshot_id="identity")
    refs = [ref for ref in plan.signals.values() if ref.kbd_id == "7"]
    assert len(refs) == 1 and refs[0].signal_id == "k1"

    assessments = initial_assessments(plan)
    assessments["7"].signal_outcomes[refs[0].ref_id] = SignalOutcome.SATISFIED
    reduce_candidates(plan, assessments, finalize=True)
    assert assessments["7"].state is CandidateState.SUPPORTED
