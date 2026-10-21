"""双链路结论级融合（arbiter）：两套独立结论 → 一个最终结论。

## 为什么必须是「结论级」而不是「候选级」

``candidate_reducer`` 的职责是「把一条链路的证据归并成该链路的候选状态」。若让它同时
理解两条链路，它就同时承担了「归约」与「融合」两种职责，任何一方的语义变更都会污染
另一方——这正是上一版（#1172）把根因概念塞进共享内核后出现的耦合。

本模块把融合收敛为**一个纯函数**：输入是两条链路各自已经算完的 ``SignalPlan`` +
``CandidateAssessment``，输出是第三条全新的 assessments（绝不原地修改输入）。
因此：

- 关键链路与根因链路的归约逻辑永远互不感知；
- 换融合策略只改本文件，两条链路零改动；
- 删掉本文件 + ``root_track.py`` 即根因链路整体下线，关键链路编译与行为不受影响。

## 融合语义（与 #1172 行为等价，仅实现位置迁移）

1. **EXCLUDE 全局否决**：排除项表达「这个 KBD 一定不是」，与证据来自哪条链路无关，
   任一链路的排除项成立即整体 REJECTED。
2. **任一路 SUPPORTED 即 SUPPORTED**：两条链路是同一目标的两种策略，互为备份而非
   互为前置条件；某一路迟迟跑不完不得把另一路已命中的结论拖回候选态。
3. **未完成不等于拒绝**：任一路仍为 CANDIDATE 时保留候选态。
4. **全部可执行路径都被否证才判 REJECTED**；全部不可执行才判 NOT_EXECUTABLE。

## 缺席语义（关键不变量）

某条链路**没有该 KBD 的任何信号**时，视为「该链路对此候选无发言权」，不参与融合。
因此：

- 只有关键信号、没有根因信号的 KBD → 融合结果恒等于关键链路自身结论（**恒等变换**）；
- 只有根因信号、没有关键信号的 KBD → 结论完全来自根因链路。

这条不变量保证了「上线双链路」对既有绝大多数 KBD 是零行为变化。
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any

from shared.cdd import (
    CandidateAssessment,
    CandidateState,
    ConclusionDecision,
    ConclusionLevel,
    EvidenceRole,
    SignalOutcome,
    SignalPlan,
    decide_conclusion,
)
from shared.observability.logger import get_logger

logger = get_logger("kbd-track-arbiter")

PROCESS_TRACK = "process"
ROOT_TRACK = "root"
KNOWN_TRACKS: tuple[str, ...] = (PROCESS_TRACK, ROOT_TRACK)

# 链路运行模式（进程环境变量 KB_SIGNAL_TRACK_MODE）。
# - process_only：只跑关键链路，根因链路不实例化、不采集（零额外成本，一键回滚）；
# - dual        ：两条链路都跑并融合（默认，与 #1172 的线上语义一致）。
#
# 赛马期曾用 root_only（根因单跑）与 shadow（两路都跑但只采信关键路）两种实验档位，
# 2026-10-21 按「只留能跑与能回滚两档」收敛掉（实验已完成、两者从未在生产启用）。
# 环境变量若仍配旧值，走非法值回落 dual 并输出 signal_track_mode_unknown 告警日志。
MODE_PROCESS_ONLY = "process_only"
MODE_DUAL = "dual"
TRACK_MODES: tuple[str, ...] = (MODE_PROCESS_ONLY, MODE_DUAL)
DEFAULT_TRACK_MODE = MODE_DUAL
TRACK_MODE_ENV = "KB_SIGNAL_TRACK_MODE"

# 模式 → 真正需要执行的链路。process_only 下根因链路连实例都不创建。
RUNNABLE_TRACKS: dict[str, tuple[str, ...]] = {
    MODE_PROCESS_ONLY: (PROCESS_TRACK,),
    MODE_DUAL: (PROCESS_TRACK, ROOT_TRACK),
}

# 模式 → 参与最终结论的链路。
CONCLUSION_TRACKS: dict[str, tuple[str, ...]] = {
    MODE_PROCESS_ONLY: (PROCESS_TRACK,),
    MODE_DUAL: KNOWN_TRACKS,
}


def resolve_track_mode(raw: str | None = None) -> str:
    """解析链路模式；非法值回落默认，避免一个错误环境变量打挂诊断链路。

    已废弃的 `shadow` / `root_only` 也走非法值回落（→ dual）——赛马实验结束后两者已删除；
    若某环境仍配旧值，必须留一条可查的日志，避免静默改变线上判定语义（shadow 原本只采信
    关键路，回落 dual 后变成两路融合）。
    """
    value = str(raw if raw is not None else os.environ.get(TRACK_MODE_ENV) or "").strip().lower()
    if value and value not in TRACK_MODES:
        logger.warning(
            event="signal_track_mode_unknown",
            message="KB_SIGNAL_TRACK_MODE 取值不受支持，已回落默认模式",
            raw=value,
            supported=list(TRACK_MODES),
            resolved=DEFAULT_TRACK_MODE,
        )
    return value if value in TRACK_MODES else DEFAULT_TRACK_MODE


def runnable_tracks(mode: str) -> tuple[str, ...]:
    return RUNNABLE_TRACKS.get(mode, RUNNABLE_TRACKS[DEFAULT_TRACK_MODE])


def conclusion_tracks(mode: str) -> tuple[str, ...]:
    return CONCLUSION_TRACKS.get(mode, CONCLUSION_TRACKS[DEFAULT_TRACK_MODE])


@dataclass(frozen=True)
class CandidateFusion:
    """单候选的融合明细，供赛马度量与审计追溯。"""

    kbd_id: str
    process_state: str | None
    root_state: str | None
    merged_state: str
    # 两条链路给出相反结论（一路支持、一路拒绝）：赛马最关心的信号。
    conflict: bool
    # 该候选在哪些链路因 EXCLUDE 成功而被否决（全局否决来源）。
    exclude_tracks: tuple[str, ...]


@dataclass(frozen=True)
class FusionOutcome:
    decision: ConclusionDecision
    assessments: dict[str, CandidateAssessment]
    details: dict[str, CandidateFusion]
    mode: str

    @property
    def conflicts(self) -> tuple[str, ...]:
        return tuple(sorted(kbd_id for kbd_id, item in self.details.items() if item.conflict))

    def detail_map(self) -> dict[str, dict[str, Any]]:
        return {
            kbd_id: {
                "process": item.process_state,
                "root": item.root_state,
                "merged": item.merged_state,
                "conflict": item.conflict,
                "exclude_tracks": list(item.exclude_tracks),
            }
            for kbd_id, item in self.details.items()
        }


def _track_view(plan: SignalPlan | None, assessments: dict[str, CandidateAssessment] | None) -> dict[str, CandidateAssessment]:
    """某条链路的「有发言权」候选集合：必须至少编译出一条信号。"""
    if not plan or not assessments:
        return {}
    with_signals = {ref.kbd_id for ref in plan.signals.values()}
    return {kbd_id: item for kbd_id, item in assessments.items() if kbd_id in with_signals}


def exclude_rejected(plan: SignalPlan | None, assessment: CandidateAssessment | None) -> bool:
    """该候选是否因「排除项成立」被否证（EXCLUDE 全局否决的判据）。

    ``decide_conclusion`` 不区分 must 否证与 exclude 否证，故此处从计划 + 逐信号
    结果独立回算，避免解析 reducer 的 reasons 文本（那是实现细节，不是契约）。
    """
    if plan is None or assessment is None:
        return False
    if assessment.state is not CandidateState.REJECTED:
        return False
    for ref in plan.signals.values():
        if ref.kbd_id != assessment.kbd_id or ref.evidence_role is not EvidenceRole.EXCLUDE:
            continue
        if assessment.signal_outcomes.get(ref.ref_id) is SignalOutcome.SATISFIED:
            return True
    return False


def _merge_states(
    states: list[CandidateState],
    exclude_flags: list[bool],
) -> CandidateState:
    """按「EXCLUDE 全局否决 → 任一支持 → 任一未完成 → 全部拒绝 → 全部不可执行」归并。"""
    if not states:
        return CandidateState.NOT_EXECUTABLE
    if any(flag for flag in exclude_flags):
        return CandidateState.REJECTED
    if any(state is CandidateState.SUPPORTED for state in states):
        return CandidateState.SUPPORTED
    if any(state is CandidateState.CANDIDATE for state in states):
        return CandidateState.CANDIDATE
    if all(state is CandidateState.REJECTED for state in states):
        return CandidateState.REJECTED
    if all(state is CandidateState.NOT_EXECUTABLE for state in states):
        return CandidateState.NOT_EXECUTABLE
    return CandidateState.INCONCLUSIVE


def _is_conflict(process_state: CandidateState | None, root_state: CandidateState | None) -> bool:
    """两路方向相反：一路明确支持、另一路明确拒绝。"""
    if process_state is None or root_state is None:
        return False
    pair = {process_state, root_state}
    return pair == {CandidateState.SUPPORTED, CandidateState.REJECTED}


def _merge_outcomes(present: list[tuple[str, CandidateAssessment]]) -> dict[str, SignalOutcome]:
    """合并两条链路的逐信号结果；ref_id 碰撞时加轨道前缀保留两路证据。

    无碰撞时键形态不变（下游按 ref_id 查表仍然有效），只在真碰撞时做命名空间隔离——
    两条链路对同一 KBD 常常同名（如 ``s1``），静默覆盖会让审计丢掉一路现场证据。
    """
    merged: dict[str, SignalOutcome] = {}
    for track, item in present:
        for key, value in item.signal_outcomes.items():
            if key in merged:
                merged[f"{track}:{key}"] = value
            else:
                merged[key] = value
    return merged


def fuse_tracks(
    *,
    mode: str,
    process_plan: SignalPlan | None = None,
    process_assessments: dict[str, CandidateAssessment] | None = None,
    root_plan: SignalPlan | None = None,
    root_assessments: dict[str, CandidateAssessment] | None = None,
) -> FusionOutcome:
    """把两条链路的独立结论融合为一个最终结论（纯函数，不修改任何输入）。

    ``assessments`` 只含**参与结论**的链路视图；``details`` 则记录两条链路的实际观测值
    （含不参与结论的那一路），供审计追溯。
    """
    resolved_mode = mode if mode in TRACK_MODES else DEFAULT_TRACK_MODE
    active_tracks = conclusion_tracks(resolved_mode)
    # 观测视图（两条链路各自的实际结论）与结论视图（仅 active_tracks）必须分开：
    # 前者是审计数据源，后者是线上判定输入，混用会让「只跑不判」的观测能力失真。
    observed_process = _track_view(process_plan, process_assessments)
    observed_root = _track_view(root_plan, root_assessments)
    process_view = observed_process if PROCESS_TRACK in active_tracks else {}
    root_view = observed_root if ROOT_TRACK in active_tracks else {}

    kbd_ids = sorted(set(process_view) | set(root_view))
    merged: dict[str, CandidateAssessment] = {}
    details: dict[str, CandidateFusion] = {}
    for kbd_id in kbd_ids:
        process_item = process_view.get(kbd_id)
        root_item = root_view.get(kbd_id)
        present: list[tuple[str, CandidateAssessment]] = [
            (track, item) for track, item in ((PROCESS_TRACK, process_item), (ROOT_TRACK, root_item)) if item
        ]
        plan_of = {PROCESS_TRACK: process_plan, ROOT_TRACK: root_plan}
        # EXCLUDE 全局否决：逐链路独立回算，不从 reducer 的 reasons 文本推断。
        exclude_by = {track: exclude_rejected(plan_of[track], item) for track, item in present}
        merged_state = _merge_states(
            [item.state for _track, item in present],
            [exclude_by[track] for track, _item in present],
        )
        reasons: list[str] = []
        for track, item in present:
            for reason in item.reasons:
                reasons.append(f"[{track}] {reason}")
        # 融合态必须显式携带来源，否则报告的“为什么不确定”会丢失链路归属。
        if merged_state is not CandidateState.SUPPORTED:
            reasons.append(f"[fusion] merged from tracks={','.join(track for track, _ in present) or 'none'}")
        merged[kbd_id] = CandidateAssessment(
            kbd_id=kbd_id,
            state=merged_state,
            reasons=reasons,
            # 逐信号结果按链路合并，供报告与审计回溯到具体证据。
            # 不能假设 ref_id 跨路唯一：它是 ``kbd_id/revision/signal_id``，两条链路
            # 对同名 signal_id 会算出同一个 key，不处理就会让后一路静默覆盖前一路。
            signal_outcomes=_merge_outcomes(present),
        )
        details[kbd_id] = CandidateFusion(
            kbd_id=kbd_id,
            process_state=(observed_process.get(kbd_id).state.value if observed_process.get(kbd_id) else None),
            root_state=(observed_root.get(kbd_id).state.value if observed_root.get(kbd_id) else None),
            merged_state=merged_state.value,
            conflict=_is_conflict(
                observed_process.get(kbd_id).state if observed_process.get(kbd_id) else None,
                observed_root.get(kbd_id).state if observed_root.get(kbd_id) else None,
            ),
            exclude_tracks=tuple(track for track, flag in exclude_by.items() if flag),
        )

    # 直接复用未修改的结论门禁：融合后的候选状态对门禁而言与「单链路归约结果」同构。
    decision = decide_conclusion(merged) if merged else ConclusionDecision(
        level=ConclusionLevel.INCONCLUSIVE,
        supported_ids=(),
        rejected_ids=(),
        inconclusive_ids=(),
        not_executable_ids=(),
        reason="no candidate has signals in the active tracks",
    )
    return FusionOutcome(decision=decision, assessments=merged, details=details, mode=resolved_mode)
