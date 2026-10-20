"""双路证据晚融合：关键路(process)与根因路(root)各自独立求值后合并。

设计约束（见 docs/solution/knowledge-base/信号双路并行与最少充分集设计.md §3.5）：

1. **互不影响**：一路的 MUST CONTRADICTED 不得否决另一路，两路互为备份而非前置条件；
2. **EXCLUDE 全局否决**：排除项表达「这个 KBD 一定不是」，与证据来自哪条路无关，
   因此任一 track 的排除项成立即整体 REJECTED；
3. **未完成不等于拒绝**：任一路仍未跑完（CANDIDATE）时不得提前下结论。

本模块是纯函数，不依赖 DB / LLM / 网络，便于单测与变异验证。
"""

from __future__ import annotations

from .models import CandidateState

# 两条证据路径的稳定标识。写入 signal.track，由 plan_compiler 透传到 SignalRef。
PROCESS_TRACK = "process"
ROOT_TRACK = "root"
KNOWN_TRACKS: tuple[str, ...] = (PROCESS_TRACK, ROOT_TRACK)

# 融合模式：可运行时切换，用于灰度与回滚。
FUSION_PROCESS_ONLY = "process_only"
FUSION_ROOT_OR_PROCESS = "root_or_process"
FUSION_ROOT_ONLY = "root_only"
FUSION_SHADOW = "shadow"
FUSION_MODES: tuple[str, ...] = (
    FUSION_PROCESS_ONLY,
    FUSION_ROOT_OR_PROCESS,
    FUSION_ROOT_ONLY,
    FUSION_SHADOW,
)


def fuse_track_states(
    states: dict[str, CandidateState],
    *,
    rejected_by: dict[str, str] | None = None,
    mode: str = FUSION_ROOT_OR_PROCESS,
) -> CandidateState:
    """把各 track 的候选状态融合为单路状态。

    ``rejected_by[track]`` 记录该 track 被拒的原因：``"exclude"`` 表示排除项成立，
    ``"must"`` 表示必需证据被否证。EXCLUDE 跨路生效，MUST 仅在本路生效。
    """
    rejected_by = rejected_by or {}
    active = _active_tracks(states, mode)
    if not active:
        # 没有可参与融合的路径：沿用「无证据」语义，交由结论门禁判 INCONCLUSIVE。
        return CandidateState.CANDIDATE

    # 1) EXCLUDE 全局否决：与路径无关。
    for track in active:
        if states.get(track) is CandidateState.REJECTED and rejected_by.get(track) == "exclude":
            return CandidateState.REJECTED

    # 2) 任一路支持即支持（互为备份，而不是互为前置条件）。
    # 必须排在「未跑完」之前：否则另一路迟迟不执行会把已命中的结论拖回 CANDIDATE，
    # 等于让两路重新耦合——正是本设计要消除的早融合行为。
    if any(states.get(track) is CandidateState.SUPPORTED for track in active):
        return CandidateState.SUPPORTED

    # 3) 仍有路径未跑完：保留候选态，让调度器继续采集。
    if any(states.get(track) is CandidateState.CANDIDATE for track in active):
        return CandidateState.CANDIDATE

    # 4) 全部可执行路径都被否证 → 整体拒绝；否则保留不确定，避免把「无法执行」误判成「已排除」。
    terminal = [states.get(track) for track in active]
    if all(state is CandidateState.REJECTED for state in terminal):
        return CandidateState.REJECTED
    if all(state is CandidateState.NOT_EXECUTABLE for state in terminal):
        return CandidateState.NOT_EXECUTABLE
    return CandidateState.INCONCLUSIVE


def _active_tracks(states: dict[str, CandidateState], mode: str) -> list[str]:
    """按融合模式选出参与结论的 track（未出现的 track 不参与）。"""
    present = [track for track in KNOWN_TRACKS if track in states]
    if mode in (FUSION_PROCESS_ONLY, FUSION_SHADOW):
        # shadow：结论只用关键路，根因路照常求值并记录（供灰度观察）。
        # 只有根因路的 KBD 在该模式下视为「无可用证据」，与上线前行为一致。
        return [PROCESS_TRACK] if PROCESS_TRACK in present else []
    if mode == FUSION_ROOT_ONLY:
        return [ROOT_TRACK] if ROOT_TRACK in present else []
    return present


def active_fusion_tracks(mode: str) -> tuple[str, ...]:
    """返回该模式下**参与结论**的 track 集合，供调度与观测复用。"""
    if mode in (FUSION_PROCESS_ONLY, FUSION_SHADOW):
        return (PROCESS_TRACK,)
    if mode == FUSION_ROOT_ONLY:
        return (ROOT_TRACK,)
    return KNOWN_TRACKS
