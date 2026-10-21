"""根因信号链路诊断器：独立计划、独立调度、独立结论。

## 为什么是独立实现而不是「给关键链路加一个轨道参数」

第一性原理把问题拆到最底层：两条链路要能「分别单独使用 + 并存 + 事后淘汰一条」，
唯一成立的前提是 **依赖方向单向**——根因链路可以依赖关键链路的执行原语与纯组件，
关键链路绝不能依赖根因链路的任何东西。

因此本模块：

- **复用**：``shared/cdd`` 的纯组件（``compile_signal_plan`` / ``ActiveDiagnosticScheduler``
  / ``reduce_candidates`` / ``decide_conclusion``）、``KBDDiagnostic`` 的采集与判定原语
  （``_execute_acquirer`` / ``_evaluate_signal_outcome`` / ``_resolve_args``）。这些原语
  不感知「轨道」概念，对两条链路行为一致，属于基础设施而非链路逻辑。
- **不碰**：``kbd_differential.diagnose`` 的控制流、``KBDDiagResult``、S2/S3/S4 会话证据项、
  关键链路的变量池与候选池。根因链路用**自己的**实例（子类）与**自己的**池。
- **隔离**：根因候选由编排层用 ``build_root_candidates`` 构造——``signals`` 替换为
  根因信号、关键信号的发布门禁元数据（``verification_contract`` / ``generation_metadata``
  / ``publish_validation``）清空。共享内核的 ``KBD`` 因此**不认识**根因信号，
  两条链路的计划在内存里是两个互不共享可变状态的实例。

删除本文件 = 根因链路整体下线，关键链路零改动、零编译错误。

## 变量自足（「单独使用」的真正缺口）

存量可执行根因信号**全部**依赖 ``{{HOST}}``（``qfk_system`` / ``qfk_log`` 的目标主机），
其中部分还依赖 ``{{DATE}}``（日志时间窗口）与 ``{{VMID}}``。这些变量在关键链路里由
``qkv_*`` 生产者写入变量池，而根因信号本身是「结论侧断言」，不含生产者，所以根因链路
必须自己解：

- 优先从关键链路已完成的 ``StepResult`` 播种同名 KBD 的变量池与物理采集缓存
  （同一条命令只采集一次，不增加现场成本）；
- 关键链路未产出时，从 ``env_context`` 派生 ``host`` / ``date`` / ``vmid`` 的缺省值，并显式
  记录 ``derived`` / ``unresolved``。派生不出来的（如没有 VM 标识的 ``{{VMID}}``）保持
  未解析 → 该信号 BLOCKED → 候选 INCONCLUSIVE，**绝不猜值**（猜错会把「无法判断」
  伪装成「已排除」）。
"""

from __future__ import annotations

import datetime as _datetime
import json
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from shared.cdd import (
    ActiveDiagnosticScheduler,
    CandidateAssessment,
    ConclusionDecision,
    ConclusionLevel,
    SignalOutcome,
    SignalPlan,
    apply_scope_results,
    compile_signal_plan,
    decide_conclusion,
)
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import KBD, KBDStep, _acquire_tool, kbd_from_dict
from shared.observability.logger import get_logger

from app.adapters.agents.htp.kbd_differential import (
    KBDDiagnostic,
    KBDDiagResult,
    StepResult,
    _tool_contract_checker,
)
from app.domain.agent_port import AgentEvent, AgentStageUpdate
from app.services.metrics import KBD_CANDIDATE_FUSION_TOTAL, KBD_SIGNAL_TRACK_OUTCOME_TOTAL

logger = get_logger("kbd-root-track")

# 根因链路的稳定标识（与关键链路的 “process” 对称）。只用于日志/指标/审计标签，
# 不写入共享内核的任何模型字段。
ROOT_TRACK = "root"

# env_context 中的候选键 → 根因信号占位符。按顺序取第一个非空值。
# 仅作关键链路未产出同名变量时的兜底，绝不复写关键链路已产出的真实值。
_ENV_ALIASES: dict[str, tuple[str, ...]] = {
    "host": ("host", "node_ip", "target_host", "ip"),
    "node_ip": ("node_ip", "host"),
    "vmid": ("vmid", "vm_id", "vm_uuid"),
    "date": ("date", "log_date"),
}


@dataclass(frozen=True)
class RootCandidate:
    """根因链路的输入单元：``kbd.signals`` 已经就是根因信号。

    共享内核不承载任何根因字段，候选的「根因形态」由本模块在编排层构造，
    因此淘汰根因链路时只需删除本文件与调用点，``shared/cdd`` 零改动。
    """

    kbd: KBD


# 关键信号的发布门禁元数据描述的是「关键信号集合」的完整性与版本，对根因信号
# 没有意义。若原样带进第二套计划，``plan_compiler`` 会拿关键信号的 signal_id 去
# 核对根因信号，报 ``verification contract references missing signal``，让全部根因
# 候选在真实数据下退化为 NOT_EXECUTABLE。故构造根因候选时统一清空。
_ISOLATED_FIELDS: dict[str, Any] = {
    "verification_contract": {},
    "generation_metadata": {},
    "publish_validation": {},
}


def build_root_candidates(raw_cases: list[dict[str, Any]] | None) -> list[RootCandidate]:
    """把 kb-service 下发的原始候选载荷转成根因链路候选。

    只接收 ``root_executable is True`` 且携带根因信号的条目：text-only 候选没有
    可执行采集路径，进入第二套计划只会被记成 NOT_EXECUTABLE，污染赛马结论分布。
    text 锚点由 ``root_display.build_anchor_entries`` 独立提取，与本函数无关。
    """
    candidates: list[RootCandidate] = []
    for case in raw_cases or []:
        if case.get("root_executable") is not True:
            continue
        root_signals = [dict(signal) for signal in (case.get("root_signals") or []) if isinstance(signal, dict)]
        if not root_signals:
            continue
        base = kbd_from_dict(case)
        kbd = base.model_copy(update={"signals": root_signals, **_ISOLATED_FIELDS})
        candidates.append(RootCandidate(kbd=kbd))
    return candidates


@dataclass
class RootTrackResult:
    """根因链路的完整产物（独立于关键链路的 KBDDiagResult）。"""

    plan: SignalPlan | None = None
    assessments: dict[str, CandidateAssessment] = field(default_factory=dict)
    decision: ConclusionDecision | None = None
    steps_executed: list[StepResult] = field(default_factory=list)
    kbds: list[KBD] = field(default_factory=list)
    # 变量自足性证据：派生成功的变量 / 派生不出来导致信号无法执行的变量。
    derived_variables: dict[str, str] = field(default_factory=dict)
    unresolved_requirements: dict[str, list[str]] = field(default_factory=dict)
    # 采集成本：复用关键链路物理采集的次数 vs 真正新执行的次数。
    reused_acquisitions: int = 0
    executed_acquisitions: int = 0
    blocked_acquisitions: int = 0
    snapshot_id: str = ""

    @property
    def ran(self) -> bool:
        """根因链路是否真的编译并执行过（用于状态区分，而非靠字段缺失推断）。"""
        return self.plan is not None

    def state_map(self) -> dict[str, str]:
        return {kbd_id: item.state.value for kbd_id, item in self.assessments.items()}


def derive_environment_variables(env_context: dict[str, Any] | None) -> dict[str, str]:
    """从环境上下文派生根因信号可能需要的缺省变量。

    只做「同名别名映射 + 日期缺省」，不做任何推断式猜测：
    - ``host``：必须是现场明确给出的目标主机/节点地址；
    - ``date``：日志时间窗口缺省取当天（``YYYY-MM-DD``，QFK 只接受绝对日期）；
    - ``vmid``：没有明确标识就不派生（返回缺失），由信号 BLOCKED 如实暴露。
    """
    context = {str(key).lower(): value for key, value in (env_context or {}).items()}
    derived: dict[str, str] = {}
    for target, aliases in _ENV_ALIASES.items():
        for alias in aliases:
            value = context.get(alias)
            text = str(value).strip() if value is not None else ""
            if text:
                derived[target] = text
                break
    derived.setdefault("date", _datetime.date.today().strftime("%Y-%m-%d"))
    return derived


def seed_pools_from_steps(steps: list[StepResult] | None) -> dict[str, dict[str, Any]]:
    """从关键链路已完成的 StepResult 重建「每 KBD 变量池」。

    ``StepResult.produced_variables`` 已是该 KBD 变量池中被声明产出的子集，
    直接按 KBD 归并即可；不读取关键链路实例的内部池，避免跨链路的可变状态共享。
    """
    pools: dict[str, dict[str, Any]] = {}
    for step in steps or []:
        if not step.kbd_id or not step.produced_variables:
            continue
        bucket = pools.setdefault(step.kbd_id, {})
        for name, value in step.produced_variables.items():
            if value is None or value == "":
                continue
            bucket.setdefault(str(name).lower(), value)
    return pools


def build_physical_cache(steps: list[StepResult] | None) -> dict[str, tuple[str | None, str | None, Any]]:
    """从关键链路已完成的 StepResult 重建「物理采集缓存」。

    键为 ``(tool_name, resolved_args)`` 的稳定序列化：同一条命令打到同一个目标，
    输出与判定无关，可被根因链路直接复用，避免重复采集现场数据。
    只缓存成功且带输出的步骤；失败/被阻塞的步骤不缓存，让根因链路自行决定是否重试。
    """
    cache: dict[str, tuple[str | None, str | None, Any]] = {}
    for step in steps or []:
        if step.error or step.raw_output is None:
            continue
        cache[_physical_key(step.tool_name, step.tool_args)] = (step.raw_output, step.error, step.ai_value)
    return cache


def _physical_key(tool_name: str, args: dict[str, Any]) -> str:
    return json.dumps(
        {"tool": tool_name, "args": args},
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    )


class RootTrackDiagnostic(KBDDiagnostic):
    """根因链路诊断器。

    继承 ``KBDDiagnostic`` 只为复用**执行/判定原语**；实例状态（变量池、候选池、
    采集缓存）由本类重新初始化，与任何关键链路实例互不可见。
    """

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self.root_result: RootTrackResult = RootTrackResult()
        # 根因链路自己的物理采集缓存（键 = tool + 已解析 args）。
        self._root_physical_cache: dict[str, tuple[str | None, str | None, Any]] = {}
        # 根因链路自己的 QKV 共享输出（当前存量根因信号无 QKV，保留以支持未来扩展）。
        self._root_qkv_outputs: dict[str, str] = {}

    # ── 公开入口 ───────────────────────────────────────────────────────────
    async def diagnose_root(
        self,
        *,
        candidates: list[RootCandidate],
        env_context: dict[str, Any],
        session_id: str,
        user_id: str = "",
        snapshot_id: str = "",
        seed_steps: list[StepResult] | None = None,
        emit_events: bool = True,
    ) -> AsyncGenerator[AgentEvent, None]:
        """独立执行根因链路。

        :param candidates: 根因候选（``RootCandidate.kbd.signals`` 已是根因信号）
        :param seed_steps: 关键链路已执行的步骤，用于复用变量与物理采集
        :param emit_events: 是否输出 ``root_diag_*`` 阶段事件（与关键链路事件名不冲突）
        """
        root_models = [item.kbd for item in candidates]
        self.root_result = RootTrackResult(kbds=list(root_models))
        if not root_models:
            if emit_events:
                yield AgentStageUpdate(
                    stage="root_diag_skipped",
                    metadata={"reason": "no_root_signals", "candidate_count": len(candidates)},
                )
            return

        ordered = sorted(candidates, key=lambda item: (item.kbd.support_id or str(item.kbd.id), str(item.kbd.id)))
        root_models = [item.kbd for item in ordered]
        resolved_snapshot = snapshot_id
        # 只编译根因信号：候选的 signals 已在构造时替换，关键链路对象不受影响。
        plan = compile_signal_plan(root_models, snapshot_id=resolved_snapshot, tool_contract_checker=_tool_contract_checker)
        assessments = initial_assessments(plan)
        scope_results = apply_scope_results(plan, assessments, env_context)
        scheduler = ActiveDiagnosticScheduler(plan)

        # 变量与采集播种：关键链路已采集/已产出的优先，环境派生兜底。
        derived = derive_environment_variables(env_context)
        seed_pools = seed_pools_from_steps(seed_steps)
        self._root_physical_cache = build_physical_cache(seed_steps)
        self._seed_pools(root_models, derived, seed_pools)
        external_variables = {str(key).lower() for key in (env_context or {})}

        self.root_result.plan = plan
        self.root_result.assessments = assessments
        self.root_result.derived_variables = dict(derived)
        self.root_result.snapshot_id = resolved_snapshot
        self.root_result.unresolved_requirements = self._requirements_without_source(
            root_models, derived, seed_pools
        )

        if emit_events:
            yield AgentStageUpdate(
                stage="root_diag_start",
                metadata={
                    "candidate_count": len(ordered),
                    "plan_id": plan.plan_id,
                    "snapshot_id": resolved_snapshot,
                    "acquisition_count": len(plan.acquisitions),
                    "signal_count": len(plan.signals),
                    "derived_variables": sorted(derived),
                    "unresolved_requirements": self.root_result.unresolved_requirements,
                    "scope_states": {kbd_id: result.state.value for kbd_id, result in scope_results.items()},
                },
            )

        steps_executed: list[StepResult] = []
        while True:
            reduce_candidates(plan, assessments)
            available_by_candidate = {
                kbd_id: (external_variables | set(pool)) - conflicts
                for kbd_id, (pool, _priorities, conflicts) in self._candidate_pools.items()
            }
            selected = scheduler.choose(assessments, external_variables, available_by_candidate)
            if selected is None:
                break
            acquisition, score = selected
            active_refs = scheduler.ready_refs(acquisition, assessments, external_variables, available_by_candidate)
            if not active_refs:
                scheduler.mark_completed(acquisition)
                continue
            representative = sorted(active_refs, key=lambda ref: ref.ref_id)[0]
            self._activate_candidate_pool(representative.kbd_id)
            resolved_args = self._resolve_args(acquisition.args_template, env_context, self._variable_pool)
            exec_id = self._stable_acquisition_exec_id(session_id, _physical_key(acquisition.tool_name, resolved_args))

            if emit_events:
                yield AgentStageUpdate(
                    stage="root_scheduler_decision",
                    metadata={
                        "plan_id": plan.plan_id,
                        "acquisition_id": acquisition.template_key,
                        "tool_name": acquisition.tool_name,
                        "args": resolved_args,
                        "utility": score.utility,
                        "linked_signal_refs": [ref.ref_id for ref in active_refs],
                    },
                )

            unresolved = self._unresolved_placeholders(resolved_args)
            raw_output: str | None = None
            error: str | None = None
            pre_matched: bool | None = None
            ai_value: Any | None = None
            if unresolved:
                # 变量无法自足：如实 BLOCKED，不猜值、不降级成 CONTRADICTED。
                error = f"未解析变量: {', '.join(sorted(unresolved))}"
                self.root_result.blocked_acquisitions += 1
            else:
                cache_key = _physical_key(acquisition.tool_name, resolved_args)
                cached = self._root_physical_cache.get(cache_key)
                if cached is not None:
                    raw_output, error, ai_value = cached
                    self.root_result.reused_acquisitions += 1
                else:
                    step = KBDStep(
                        tool_name=acquisition.tool_name,
                        tool_args_template=resolved_args,
                        matcher=representative.signal.get("match"),
                    )
                    try:
                        raw_output, error, pre_matched, ai_value = await self._execute_acquirer(
                            step,
                            env_context,
                            session_id,
                            user_id,
                            signal=representative.signal,
                            exec_id=exec_id,
                        )
                    except Exception as exc:  # 采集异常不得中断整条链路
                        error = str(exc)
                    self.root_result.executed_acquisitions += 1
                    if error is None:
                        self._root_physical_cache[cache_key] = (raw_output, error, ai_value)

            for ref in active_refs:
                self._activate_candidate_pool(ref.kbd_id)
                evaluation_id = self._stable_evaluation_id(exec_id, ref.ref_id)
                ref_pre_matched = pre_matched if ref.matcher_fingerprint == representative.matcher_fingerprint else None
                outcome = (
                    SignalOutcome.BLOCKED
                    if unresolved
                    else self._evaluate_signal_outcome(ref.signal, raw_output, error, ref_pre_matched)
                )
                assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = outcome
                result = StepResult(
                    tool_name=acquisition.tool_name,
                    tool_args=resolved_args,
                    raw_output=raw_output,
                    error=error,
                    match_kbd_ids={ref.kbd_id} if outcome is SignalOutcome.SATISFIED else set(),
                    kbd_id=ref.kbd_id,
                    signal_id=ref.signal_id,
                    exec_id=exec_id,
                    evaluation_id=evaluation_id,
                    acquisition_id=acquisition.template_key,
                    outcome=outcome,
                    required=ref.required_for_support,
                    ai_value=ai_value,
                )
                steps_executed.append(result)
                if emit_events:
                    yield AgentStageUpdate(
                        stage="root_tool_result",
                        metadata=self._tool_result_metadata(result),
                    )
            scheduler.mark_completed(acquisition, {ref.ref_id for ref in active_refs})

        # 收尾：无可执行采集路径的引用如实标记 BLOCKED（变量缺失 / 无执行器）。
        for ref in list(scheduler.remaining_signal_refs(assessments)):
            if ref.ref_id in assessments[ref.kbd_id].signal_outcomes:
                continue
            self._activate_candidate_pool(ref.kbd_id)
            args = self._resolve_args((ref.signal.get("acquire") or {}).get("args") or {}, env_context, self._variable_pool)
            available = (external_variables | set(self._variable_pool)) - self._variable_pool_conflicts
            missing = sorted(set(ref.requires) - available)
            result = StepResult(
                tool_name=_acquire_tool(ref.signal),
                tool_args=args,
                raw_output=None,
                error=f"依赖变量缺失: {', '.join(missing)}" if missing else "无可执行采集路径",
                kbd_id=ref.kbd_id,
                signal_id=ref.signal_id,
                exec_id=self._stable_acquisition_exec_id(session_id, ref.ref_id),
                evaluation_id=self._stable_evaluation_id(ref.ref_id, ref.ref_id),
                outcome=SignalOutcome.BLOCKED,
                required=ref.required_for_support,
            )
            assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = SignalOutcome.BLOCKED
            steps_executed.append(result)
            # 变量缺失导致的阻塞必须计入 blocked 指标，否则「根因链路为何没结论」
            # 在度量上表现为「什么都没发生」，运维无从区分「无信号」与「变量不全」。
            if missing:
                self.root_result.blocked_acquisitions += 1
            if emit_events:
                yield AgentStageUpdate(stage="root_tool_result", metadata=self._tool_result_metadata(result))

        reduce_candidates(plan, assessments, finalize=True)
        decision = decide_conclusion(assessments)
        supported = [plan.candidates[kbd_id] for kbd_id in decision.supported_ids]
        report = await self._render_root_report(
            plan=plan,
            assessments=assessments,
            decision=decision,
            supported=supported,
            steps_executed=steps_executed,
            user_id=user_id,
        )

        self.root_result.decision = decision
        self.root_result.steps_executed = steps_executed
        self._record_root_metrics(plan, assessments, decision)
        # 关键链路无任何结果时，统一结果入口由根因链路提供（仅根因信号的 KBD 走这条链路）。
        self._result = KBDDiagResult(
            matched_kbds=supported,
            steps_executed=steps_executed,
            is_definitive=decision.level is ConclusionLevel.DEFINITIVE,
            diagnosis_report=report,
            conclusion_level=decision.level.value,
            candidate_states=self.root_result.state_map(),
        )

        logger.info(
            event="root_track_conclusion",
            message="根因信号链路结论完成",
            session_id=session_id,
            snapshot_id=resolved_snapshot,
            conclusion_level=decision.level.value,
            supported_ids=list(decision.supported_ids),
            rejected_ids=list(decision.rejected_ids),
            inconclusive_ids=list(decision.inconclusive_ids),
            candidate_states=self.root_result.state_map(),
            derived_variables=sorted(derived),
            unresolved_requirements=self.root_result.unresolved_requirements,
            reused_acquisitions=self.root_result.reused_acquisitions,
            executed_acquisitions=self.root_result.executed_acquisitions,
            blocked_acquisitions=self.root_result.blocked_acquisitions,
        )

        if emit_events:
            yield AgentStageUpdate(
                stage="root_diag_complete",
                metadata={
                    "plan_id": plan.plan_id,
                    "conclusion_level": decision.level.value,
                    "supported_kbds": list(decision.supported_ids),
                    "supported_support_ids": [kbd.support_id or str(kbd.id) for kbd in supported],
                    "rejected_kbds": list(decision.rejected_ids),
                    "inconclusive_kbds": list(decision.inconclusive_ids),
                    "not_executable_kbds": list(decision.not_executable_ids),
                    "steps_count": len(steps_executed),
                    "reused_acquisitions": self.root_result.reused_acquisitions,
                    "executed_acquisitions": self.root_result.executed_acquisitions,
                    "stop_reason": decision.reason,
                },
            )

    # ── 内部：计划与变量播种 ────────────────────────────────────────────────
    def _seed_pools(
        self,
        ordered: list[KBD],
        derived: dict[str, str],
        seed_pools: dict[str, dict[str, Any]],
    ) -> None:
        """为每个根因候选建立独立变量池：环境派生兜底 + 关键链路真实产出优先。

        优先级语义与 ``_set_pool_var`` 一致：播种值不写 priority，后续生产者
        若给出不同值会触发 conflict 门闩（宁可阻塞也不让执行顺序决定现场参数）。
        """
        for kbd in ordered:
            pool: dict[str, Any] = dict(derived)
            for name, value in (seed_pools.get(kbd.id) or {}).items():
                if value is None or value == "":
                    continue
                pool[str(name).lower()] = value
            self._candidate_pools[kbd.id] = (pool, {}, set())

    @staticmethod
    def _requirements_without_source(
        ordered: list[KBD],
        derived: dict[str, str],
        seed_pools: dict[str, dict[str, Any]],
    ) -> dict[str, list[str]]:
        """列出「声明了依赖但没有任何来源」的变量，作为可观测的独立运行缺口证据。"""
        gaps: dict[str, list[str]] = {}
        for kbd in ordered:
            available = set(derived) | set(seed_pools.get(kbd.id) or {})
            missing: set[str] = set()
            for signal in kbd.signals:
                for raw in _iter_placeholders(signal):
                    name = raw.lower()
                    if name not in available:
                        missing.add(name)
            if missing:
                gaps[str(kbd.id)] = sorted(missing)
        return gaps

    # ── 内部：报告与度量 ───────────────────────────────────────────────────
    async def _render_root_report(
        self,
        *,
        plan: SignalPlan,
        assessments: dict[str, CandidateAssessment],
        decision: ConclusionDecision,
        supported: list[KBD],
        steps_executed: list[StepResult],
        user_id: str,
    ) -> str:
        """根因链路自己的报告。

        复用基类的报告生成器（它只依赖 KBD + StepResult，不感知轨道），
        但由根因链路独立调用，关键链路的报告内容不受影响。
        """
        exclusion_reasons: dict[str, list[str]] = {}
        for kbd in plan.candidates.values():
            if kbd.id in decision.supported_ids:
                continue
            reasons: list[str] = []
            reasons.extend(plan.compile_errors.get(kbd.id) or [])
            assessment = assessments.get(kbd.id)
            if assessment is not None:
                reasons.extend(assessment.reasons)
            if reasons:
                exclusion_reasons[str(kbd.id)] = reasons
        pending = [kbd for kbd in plan.candidates.values() if kbd.id not in decision.supported_ids]
        if decision.level is ConclusionLevel.PARTIAL:
            # 基类两个报告入口的异步形态**不同**：``_generate_report`` 是 async 方法，
            # ``_build_partial_report`` 是同步 staticmethod。这里必须严格照基类契约调用，
            # 给后者加 await 会把 str 当 awaitable 而抛 TypeError。
            return self._build_partial_report(
                supported,
                steps_executed,
                evaluated_kbds=pending,
                exclusion_reasons=exclusion_reasons,
            )
        if decision.level is ConclusionLevel.DEFINITIVE:
            return await self._generate_report(
                supported,
                steps_executed,
                evaluated_kbds=list(plan.candidates.values()),
                user_id=user_id,
                exclusion_reasons=exclusion_reasons,
            )
        return await self._generate_report(
            [],
            steps_executed,
            evaluated_kbds=pending,
            user_id=user_id,
            exclusion_reasons=exclusion_reasons,
        )

    @staticmethod
    def _record_root_metrics(
        plan: SignalPlan,
        assessments: dict[str, CandidateAssessment],
        decision: ConclusionDecision,
    ) -> None:
        """按根因链路记录信号结果与候选结论，与关键链路指标完全分离。

        指标失败绝不影响排障主流程（与关键链路同一容错契约）。
        """
        try:
            refs_by_kbd: dict[str, list] = {}
            for ref in plan.signals.values():
                refs_by_kbd.setdefault(ref.kbd_id, []).append(ref)
            for kbd_id, assessment in assessments.items():
                for ref in refs_by_kbd.get(kbd_id, []):
                    outcome = assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)
                    KBD_SIGNAL_TRACK_OUTCOME_TOTAL.labels(
                        track=ROOT_TRACK,
                        outcome=outcome.value,
                        tool=_acquire_tool(ref.signal) or "unknown",
                    ).inc()
                KBD_CANDIDATE_FUSION_TOTAL.labels(
                    fusion_mode="root",
                    state=assessment.state.value,
                    track_states=f"{ROOT_TRACK}={assessment.state.value}",
                    active_tracks=ROOT_TRACK,
                ).inc()
        except Exception as exc:
            logger.warning(event="kbd_root_track_metrics_failed", error=str(exc))


def _iter_placeholders(signal: dict[str, Any]) -> list[str]:
    """收集单条根因信号 acquire.args 中的 ``{{NAME}}`` 占位符。"""
    import re as _re

    pattern = _re.compile(r"\{\{\s*([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")
    found: list[str] = []

    def _walk(value: Any) -> None:
        if isinstance(value, str):
            found.extend(pattern.findall(value))
        elif isinstance(value, dict):
            for item in value.values():
                _walk(item)
        elif isinstance(value, list):
            for item in value:
                _walk(item)

    _walk((signal.get("acquire") or {}).get("args") or {})
    return found
