"""KBD 证据诊断引擎。

按不可变 KBD revision 中的 signal_id 执行关键信号。命令来自 KBD acquire
契约，matcher 由程序确定性求值；只有全部 required signal 为 SATISFIED 才支持
对应 KBD。UNKNOWN、ERROR、BLOCKED 和单候选均不能产生根因结论。
"""

from __future__ import annotations

import ipaddress
import json
import re
import uuid
from collections.abc import AsyncGenerator
from dataclasses import dataclass, field
from typing import Any

from shared.cdd import (
    ActiveDiagnosticScheduler,
    CandidateState,
    ConclusionLevel,
    SignalOutcome,
    apply_scope_results,
    build_kbd_replay_manifest,
    compile_signal_plan,
    decide_conclusion,
)
from shared.cdd.candidate_reducer import initial_assessments, reduce_candidates
from shared.cdd.kbd_model import KBD, KBDStep, _acquire_tool, _signal_category
from shared.clients import AIAssistantRegistry
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.schemas.acquirer_args import DEFAULT_SIGNAL_TIMEOUT_SECONDS
from shared.schemas.kbd_signal_safety import kbd_signal_read_only_violation

from app.core.utils import smart_truncate
from app.domain.agent_port import (
    AgentEvent,
    AgentStageUpdate,
    AgentTextChunk,
)

logger = get_logger("kbd-differential")


def _tool_contract_checker(tool: str, signal: dict[str, Any]) -> str | None:
    """在线编译校验：用 BackendSignal + build_acli_command 校验 QFK 信号的可执行性。

    此函数注入到 shared/cdd/plan_compiler.compile_signal_plan 的
    tool_contract_checker 回调中，保持与移动前的行为一致。
    """
    import re

    from shared.schemas.acquirer_args import DEFAULT_SIGNAL_TIMEOUT_SECONDS

    from app.tools.qfk.handlers import build_acli_command
    from app.tools.qfk.signal import BackendSignal

    if not tool.startswith("qfk_"):
        return None
    namespace = tool.removeprefix("qfk_")
    args = (signal.get("acquire") or {}).get("args") or {}
    sample_values = {
        "PID": "1", "HOST": "127.0.0.1", "VM": "golden-vm",
        "DEVICE": "/dev/sda", "STORAGE_PATH": "/sf/data/golden",
        "END": "2026-07-30 10:00:00", "REQUEST_ID": "a5ed4ad9340ce338ba1ac71d13ffcfb9",
    }

    def resolve_sample(value: Any, field_name: str = "") -> Any:
        if isinstance(value, str):
            if field_name == "file" and re.fullmatch(r"\{\{[A-Z][A-Z0-9_]*\}\}", value):
                return "sample.log"
            return re.sub(
                r"\{\{([A-Z][A-Z0-9_]*)\}\}",
                lambda match: sample_values.get(match.group(1), "value"),
                value,
            )
        if isinstance(value, dict):
            return {key: resolve_sample(item, key) for key, item in value.items()}
        if isinstance(value, list):
            return [resolve_sample(item) for item in value]
        return value

    compiled_args = resolve_sample(args)
    matcher = signal.get("match") or {}
    pattern = matcher.get("pattern") if matcher.get("type") == "keyword" else None
    keywords = (
        [pattern] if isinstance(pattern, str) and pattern
        else list(pattern or []) if isinstance(pattern, list) else []
    )
    data: dict[str, Any] = {
        "namespace": namespace,
        "host": compiled_args.get("host"),
        "timeout": compiled_args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS),
        "container": compiled_args.get("container"),
        "command": compiled_args.get("command"),
        "resource_keyword": compiled_args.get("resource_keyword"),
        "file": compiled_args.get("file"),
        "path": compiled_args.get("path"),
        "time_window": compiled_args.get("time_window"),
        "source_family": compiled_args.get("source_family", "auto"),
        "parser": compiled_args.get("parser"),
        "request_id": compiled_args.get("request_id"),
        "context_lines": compiled_args.get("context_lines", 0),
        "include_archives": compiled_args.get("include_archives", False),
        "archive_precheck": compiled_args.get("archive_precheck"),
        "matcher": matcher or None,
        "keyword": keywords,
        "match_mode": {"any": "or", "all": "and"}.get(str(matcher.get("mode") or "or"), str(matcher.get("mode") or "or")),
        "expected": bool(matcher.get("expected", True)),
    }
    if namespace == "service":
        data["service"] = compiled_args.get("service") or compiled_args.get("resource_keyword")
        data["action"] = compiled_args.get("action") or compiled_args.get("command") or "status"
    try:
        build_acli_command(BackendSignal.model_validate(data))
    except Exception as exc:
        return f"runtime command compile failed: {exc}"
    return None


# ADR-2 占位符：运行期解析统一认 {{NAME}}。大小写处理分两层（纵深防御，彻底消除脆弱性）：
#   1) 抽取/校验层强制模板占位符为大写（extract_signals.validate_placeholder_case，
#      如 {{HOST}} 合法、{{host}} 非法），保证模板书写规范；
#   2) 运行期解析对「变量名」大小写不敏感（_resolve_args 按小写查找），且生产者写入
#      变量池时 Key 强制小写（_set_pool_var），因此无论 {{HOST}}/{{host}}/{{Host}}
#      均能命中池内小写 Key，无需依赖「produces 名必须大写」的全局隐式约定；
#      未命中（如生产者尚未产出该变量）的占位符保留原样，不抛异常，交由下层处理。
_PLACEHOLDER_RE = re.compile(r"\{\{([A-Za-z0-9_.]+)\}\}")


def _signal_requires_human(signal: dict | None) -> bool:
    """写操作/处置动作信号判定：诊断阶段绝不自动执行，必须人工授权。

    判定优先级：
      1) 信号已显式标记 require_human_confirm / phase=solution（抽取层已标注）
      2) 纵深防御：共享 KBD 只读边界识别到处置阶段或明确写动作
    """
    if not signal:
        return False
    if (signal.get("review") or {}).get("require_human_confirm"):
        return True
    return kbd_signal_read_only_violation(signal) is not None


@dataclass
class StepResult:
    """单步执行结果记录（用于报告生成和审计追踪）。"""

    tool_name: str
    tool_args: dict
    raw_output: str | None  # 工具执行的原始输出字符串
    error: str | None  # 执行错误（非 None 时此步骤无法判断）
    match_kbd_ids: set[str] = field(default_factory=set)  # 判断后匹配的 KBD ID 集合
    kbd_id: str = ""
    signal_id: str = ""
    exec_id: str = ""
    evaluation_id: str = ""
    acquisition_id: str = ""
    outcome: SignalOutcome = SignalOutcome.NOT_RUN
    required: bool = True
    produced_variables: dict[str, Any] = field(default_factory=dict)
    ai_value: Any | None = None  # Matcher 命中后受控 AI 提取并已逐字溯源的值


@dataclass
class KBDDiagResult:
    """KBD 差异诊断最终结果。"""

    matched_kbds: list[KBD]  # 最终候选 KBD（按匹配度排序）
    steps_executed: list[StepResult]  # 已执行步骤序列
    is_definitive: bool  # True = supported 存在且其他候选均已 supported/rejected
    diagnosis_report: str  # 确定性模板生成的结构化诊断报告
    conclusion_level: str = ConclusionLevel.INCONCLUSIVE.value
    candidate_states: dict[str, str] = field(default_factory=dict)


class KBDDiagnostic:
    """KBD 差异诊断引擎。

    使用方式：
        diag = KBDDiagnostic(ai_registry, tool_executor)
        async for event in diag.diagnose(candidates, env_context, session_id):
            yield event
        result = diag.get_result()

    线程安全：单实例不应并发调用 diagnose()，每次对话应创建独立实例。
    """

    def __init__(
        self,
        ai_registry: AIAssistantRegistry,
        tool_executor: Any,  # 实现 ToolExecutor Protocol 的对象
        diagnostic_item_client: Any | None = None,  # DiagnosticItemClient（可选）
        conversation_id: str | None = None,  # 会话 ID（用于 INSERT）
        case_id: str | None = None,  # 工单 ID（用于 terminal_bridge 会话路由）
        assistant_type: str = "htp-agent",
        db_session_factory: Any | None = None,  # DB 会话工厂（用于从 prompt 管理加载 Prompt）
    ) -> None:
        self._ai_registry = ai_registry
        self._tool_executor = tool_executor
        self._diagnostic_item_client = diagnostic_item_client
        self._conversation_id = conversation_id
        self._case_id = case_id  # 工单 ID，用于 QFK 信号执行时路由到正确的 SSH 会话
        self._assistant_type = assistant_type
        self._db_session_factory = db_session_factory
        self._result: KBDDiagResult | None = None
        # 会话级变量池（黑板）：阶段 A 生产者(QKV)写入，阶段 B 消费者(QFK)读取
        self._variable_pool: dict[str, Any] = {}
        self._variable_pool_priority: dict[str, int] = {}
        # 当前诊断会话内缓存 HCI 节点名称到 IP 的映射，避免每个 QKV 结果重复查询节点列表。
        self._host_ip_cache: dict[str, str] = {}

    def get_result(self) -> KBDDiagResult | None:
        """获取最近一次 diagnose() 调用的结果（调用前返回 None）。"""
        return self._result

    def _set_pool_var(self, name: str, value: Any, *, producer_priority: int | None = None) -> None:
        """写入会话变量池（黑板）的规范化入口。

        生产侧静默规范化：Key 强制统一小写存储，从源头消除变量池内部大小写
        不一致（如 HoSt / host / HOST 并存），保证 debug 打印、前端展示、
        落库存储时 Key 永远干净统一（host、vm_id）。
        消费侧 _resolve_args 同步做大小写不敏感解析作为纵深，二者组合彻底消除
        大小写脆弱性。与抽取层「占位符必须大写」的校验互不冲突——模板大写仅约束
        书写方，运行期解析兼容任意大小写。

        P2 增强：记录变量池变更日志，便于追溯信号执行链路。
        """
        key = name.strip().lower()
        existing_priority = self._variable_pool_priority.get(key)
        if producer_priority is not None and existing_priority is not None and producer_priority < existing_priority:
            logger.info(
                event="variable_pool_lower_priority_ignored",
                name=name,
                key=key,
                producer_priority=producer_priority,
                existing_priority=existing_priority,
                session_id=self._conversation_id or "",
            )
            return
        old_value = self._variable_pool.get(key)
        self._variable_pool[key] = value
        if producer_priority is not None:
            self._variable_pool_priority[key] = producer_priority

        # P2: 变量池变更日志
        value_preview = str(value)[:100] if value is not None else None
        if len(str(value)) > 100:
            value_preview = value_preview + "...(截断)"
        logger.info(
            event="variable_pool_update",
            name=name,
            key=key,
            value_preview=value_preview,
            is_new=key not in self._variable_pool or old_value is None,
            session_id=self._conversation_id or "",
        )

    async def diagnose(
        self,
        candidates: list[KBD],
        env_context: dict[str, str],
        session_id: str,
        user_id: str = "",
        snapshot_id: str = "runtime",
    ) -> AsyncGenerator[AgentEvent, None]:
        """编译并调度 KBD acquisition graph，结论由封闭状态机门控。"""
        if not candidates:
            yield AgentTextChunk(content="未找到可执行 KBD，无法形成有据可查的诊断结论。")
            self._result = KBDDiagResult(
                matched_kbds=[],
                steps_executed=[],
                is_definitive=False,
                diagnosis_report="分类内没有可执行 KBD，无法形成有据可查的诊断结论。",
                conclusion_level=ConclusionLevel.INCONCLUSIVE.value,
            )
            return

        ordered = sorted(candidates, key=lambda k: (k.support_id or str(k.id), str(k.id)))
        plan = compile_signal_plan(ordered, snapshot_id=snapshot_id, tool_contract_checker=_tool_contract_checker)
        assessments = initial_assessments(plan)
        scope_results = apply_scope_results(plan, assessments, env_context)
        scheduler = ActiveDiagnosticScheduler(plan)
        steps_executed: list[StepResult] = []
        if self._diagnostic_item_client and self._conversation_id:
            hypotheses_data = [
                {
                    "content": {
                        "kbd_id": kbd.id,
                        "support_id": kbd.support_id,
                        "kbd_name": kbd.name,
                        "root_cause": kbd.root_cause,
                    },
                    "status": "pending",
                }
                for kbd in ordered
            ]
            await self._diagnostic_item_client.batch_create_items(
                conversation_id=uuid.UUID(self._conversation_id),
                stage="S2",
                type="hypothesis",
                items_data=hypotheses_data,
            )
            logger.info(
                event="s2_hypotheses_inserted",
                conversation_id=self._conversation_id,
                count=len(hypotheses_data),
                session_id=session_id,
            )

        yield AgentStageUpdate(
            stage="kbd_diag_start",
            metadata={
                "candidate_count": len(ordered),
                "session_id": session_id,
                "snapshot_id": snapshot_id,
                "plan_id": plan.plan_id,
                "acquisition_count": len(plan.acquisitions),
                "scope_states": {
                    kbd_id: result.state.value
                    for kbd_id, result in scope_results.items()
                },
            },
        )

        while True:
            reduce_candidates(plan, assessments)
            available = {str(key).lower() for key in env_context} | set(self._variable_pool)
            selected = scheduler.choose(assessments, available)
            if selected is None:
                break
            acquisition, score = selected
            active_refs = [
                ref for ref in acquisition.signal_refs if assessments[ref.kbd_id].state is CandidateState.CANDIDATE
            ]
            if not active_refs:
                scheduler.mark_completed(acquisition)
                continue
            representative = sorted(active_refs, key=lambda ref: ref.ref_id)[0]
            resolved_args = self._resolve_args(acquisition.args_template, env_context, self._variable_pool)
            runtime_key = json.dumps(
                {
                    "template_key": acquisition.template_key,
                    "resolved": self._acquisition_key(acquisition.tool_name, resolved_args, env_context),
                },
                sort_keys=True,
                separators=(",", ":"),
            )
            exec_id = self._stable_acquisition_exec_id(session_id, runtime_key)
            unresolved = self._unresolved_placeholders(resolved_args)
            blocked_reason = None
            if unresolved:
                blocked_reason = f"未解析变量: {', '.join(sorted(unresolved))}"
            elif any(_signal_requires_human(ref.signal) for ref in active_refs):
                blocked_reason = "诊断阶段禁止执行写操作/处置动作"

            yield AgentStageUpdate(
                stage="kbd_scheduler_decision",
                metadata={
                    "plan_id": plan.plan_id,
                    "acquisition_id": acquisition.template_key,
                    "runtime_acquisition_key": runtime_key,
                    "utility": score.utility,
                    "score": {
                        "discrimination": score.discrimination,
                        "required_coverage": score.required_coverage,
                        "unlock": score.unlock,
                        "reuse": score.reuse,
                        "cost": score.cost,
                        "latency": score.latency,
                        "risk": score.risk,
                    },
                    "linked_signal_refs": [ref.ref_id for ref in active_refs],
                },
            )
            yield AgentStageUpdate(
                stage="tool_call",
                metadata={
                    "exec_id": exec_id,
                    "acquisition_id": acquisition.template_key,
                    "tool_name": acquisition.tool_name,
                    # args/result 是 Custom-UI 工具卡片的标准字段；tool_args/tool_result
                    # 暂时保留为兼容别名，避免旧消费者在滚动发布期间失效。
                    "args": resolved_args,
                    "tool_args": resolved_args,
                    "status": "blocked" if blocked_reason else "running",
                    "risk_level": acquisition.risk,
                    "policy": "auto",
                    "kbd_id": representative.kbd_id,
                    "support_id": representative.support_id,
                    "signal_id": representative.signal_id,
                    "linked_signal_refs": [ref.ref_id for ref in active_refs],
                    "command_source": "KBD",
                },
            )

            raw_output: str | None = None
            error: str | None = blocked_reason
            pre_matched: bool | None = None
            ai_value: Any | None = None
            if not blocked_reason:
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
                except Exception as exc:
                    error = str(exc)

            for ref in active_refs:
                evaluation_id = self._stable_evaluation_id(exec_id, ref.ref_id)
                outcome = (
                    SignalOutcome.BLOCKED
                    if blocked_reason
                    else self._evaluate_signal_outcome(
                        ref.signal,
                        raw_output,
                        error,
                        pre_matched if ref.matcher_fingerprint == representative.matcher_fingerprint else None,
                    )
                )
                assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = outcome
                declared_produces = (
                    (ref.signal.get("orchestrate") or {}).get("produces") or ref.signal.get("produces") or ref.produces
                )
                produced_variables: dict[str, Any] = {}
                if not error:
                    for item in declared_produces:
                        name = item.get("name") if isinstance(item, dict) else item
                        key = str(name or "").strip().lower()
                        if key and key in self._variable_pool and key not in produced_variables:
                            produced_variables[key] = self._variable_pool[key]
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
                    produced_variables=produced_variables,
                    ai_value=ai_value,
                )
                steps_executed.append(result)
                yield AgentStageUpdate(stage="tool_result", metadata=self._tool_result_metadata(result))
                if self._diagnostic_item_client and self._conversation_id:
                    await self._diagnostic_item_client.create_item(
                        conversation_id=uuid.UUID(self._conversation_id),
                        stage="S3",
                        type="verification_step",
                        seq=len(steps_executed),
                        content={
                            "kbd_id": ref.kbd_id,
                            "support_id": ref.support_id,
                            "signal_id": ref.signal_id,
                            "exec_id": exec_id,
                            "evaluation_id": evaluation_id,
                            "acquisition_id": acquisition.template_key,
                            "tool_name": acquisition.tool_name,
                            "tool_args": resolved_args,
                            "raw_output": smart_truncate(raw_output or "", max_chars=500),
                            "error": error,
                            "outcome": outcome.value,
                            "produced_variables": result.produced_variables,
                        },
                        status=(
                            "confirmed"
                            if outcome is SignalOutcome.SATISFIED
                            else "rejected"
                            if outcome is SignalOutcome.CONTRADICTED
                            else "error"
                        ),
                    )
            scheduler.mark_completed(acquisition)

        # No executable acquisition remains. Preserve unresolved evidence as BLOCKED.
        for ref in list(scheduler.remaining_signal_refs(assessments)):
            if ref.ref_id in assessments[ref.kbd_id].signal_outcomes:
                continue
            args = self._resolve_args(
                (ref.signal.get("acquire") or {}).get("args") or {}, env_context, self._variable_pool
            )
            missing = sorted(set(ref.requires) - ({str(key).lower() for key in env_context} | set(self._variable_pool)))
            runtime_key = self._acquisition_key(_acquire_tool(ref.signal), args, env_context)
            exec_id = self._stable_acquisition_exec_id(session_id, runtime_key)
            evaluation_id = self._stable_evaluation_id(exec_id, ref.ref_id)
            result = StepResult(
                tool_name=_acquire_tool(ref.signal),
                tool_args=args,
                raw_output=None,
                error=f"依赖变量缺失: {', '.join(missing)}" if missing else "无可执行采集路径",
                kbd_id=ref.kbd_id,
                signal_id=ref.signal_id,
                exec_id=exec_id,
                evaluation_id=evaluation_id,
                outcome=SignalOutcome.BLOCKED,
                required=ref.required_for_support,
            )
            assessments[ref.kbd_id].signal_outcomes[ref.ref_id] = SignalOutcome.BLOCKED
            steps_executed.append(result)
            yield AgentStageUpdate(stage="tool_result", metadata=self._tool_result_metadata(result))

        reduce_candidates(plan, assessments, finalize=True)
        decision = decide_conclusion(assessments)
        supported = [plan.candidates[kbd_id] for kbd_id in decision.supported_ids]
        definitive = decision.level is ConclusionLevel.DEFINITIVE

        yield AgentStageUpdate(
            stage="kbd_diag_running",
            metadata={
                "supported_count": len(supported),
                "candidate_count": len(ordered),
                "conclusion_level": decision.level.value,
                "candidate_states": {kbd_id: item.state.value for kbd_id, item in assessments.items()},
            },
        )
        # 可观测性：候选未确认时报告必须说明原因（契约过期/编译错误/scope 拦截），
        # 否则「无可执行证据」会把数据问题与契约问题都吞成同一文案，无法现场定位。
        exclusion_reasons: dict[str, list[str]] = {}
        for kbd in ordered:
            if kbd.id in decision.supported_ids:
                continue
            reasons: list[str] = []
            reasons.extend(plan.compile_errors.get(kbd.id) or [])
            assessment = assessments.get(kbd.id)
            if assessment is not None:
                reasons.extend(assessment.reasons)
            if reasons:
                exclusion_reasons[str(kbd.id)] = reasons
        report = await self._generate_report(
            supported if definitive else [],
            steps_executed,
            evaluated_kbds=ordered,
            user_id=user_id,
            exclusion_reasons=exclusion_reasons,
        )
        if decision.level is ConclusionLevel.PARTIAL:
            supported_refs = ", ".join(
                plan.candidates[kbd_id].support_id or kbd_id for kbd_id in decision.supported_ids
            )
            report = (
                "### 诊断结论：部分证据成立，暂不能定论\n\n"
                f"参考案例 {supported_refs} 的必需关键信号均已满足，但分类内仍有未决或不可执行 KBD；"
                "这些候选仍可能成立，因此 Conclusion Gate 禁止输出根因、解决方案或进入 S4。\n\n"
                + report.replace(
                    "没有任何 KBD 的全部必需关键信号均已满足",
                    "已有 KBD 的必要证据均已满足，但候选全集尚未完成排除",
                )
            )

        self._result = KBDDiagResult(
            matched_kbds=supported,
            steps_executed=steps_executed,
            is_definitive=definitive,
            diagnosis_report=report,
            conclusion_level=decision.level.value,
            candidate_states={kbd_id: item.state.value for kbd_id, item in assessments.items()},
        )

        if definitive and self._diagnostic_item_client and self._conversation_id:
            for seq, kbd in enumerate(supported, start=1):
                evidence_ids = sorted({step.exec_id for step in steps_executed if step.kbd_id == kbd.id})
                evaluation_ids = sorted({step.evaluation_id for step in steps_executed if step.kbd_id == kbd.id})
                await self._diagnostic_item_client.create_item(
                    conversation_id=uuid.UUID(self._conversation_id),
                    stage="S4",
                    type="root_cause",
                    seq=seq,
                    content={
                        "kbd_id": kbd.id,
                        "support_id": kbd.support_id,
                        "kbd_name": kbd.name,
                        "root_cause": kbd.root_cause,
                        "solution": kbd.solution,
                        "evidence_exec_ids": evidence_ids,
                        "evidence_evaluation_ids": evaluation_ids,
                        "is_definitive": True,
                    },
                    status="confirmed",
                )

        # ``diagnostic_item`` 面向会话恢复；动态资源审计面向“某一份已发布 KBD
        # 被 Agent 怎样消费、每个 Signal 最终怎样”的长期评估。两者职责不同，不能
        # 仅因为页面已展示执行步骤就省略后者。
        await self._audit_kbd_runtime_outcomes(
            plan=plan,
            assessments=assessments,
            steps_executed=steps_executed,
            session_id=session_id,
            decision=decision,
            environment=env_context,
        )

        yield AgentStageUpdate(
            stage="kbd_diag_complete",
            metadata={
                "matched_count": len(supported),
                "evaluated_kbds": [kbd.support_id or str(kbd.id) for kbd in ordered],
                "steps_count": len(steps_executed),
                "is_definitive": definitive,
                "conclusion_level": decision.level.value,
                "supported_kbds": list(decision.supported_ids),
                "rejected_kbds": list(decision.rejected_ids),
                "inconclusive_kbds": list(decision.inconclusive_ids),
                "not_executable_kbds": list(decision.not_executable_ids),
                "stop_reason": decision.reason,
            },
        )

    async def _audit_kbd_runtime_outcomes(
        self,
        *,
        plan: Any,
        assessments: dict[str, Any],
        steps_executed: list[StepResult],
        session_id: str,
        decision: Any,
        environment: dict[str, Any],
    ) -> None:
        """按精确 KBD resource revision 写入运行结果，供评估和失败模式聚合使用。

        该审计只保存结构化 outcome、错误类别和哈希输入/输出；原始命令输出继续由
        terminal bridge artifact 管理，避免将可能含现场敏感信息的日志复制进分析表。
        审计失败不得中断客户排障主流程。
        """

        if self._db_session_factory is None:
            return
        try:
            from shared.dynamic_resource.loader import DynamicResourceLoader, ResourceNotFoundError
            from shared.dynamic_resource.models import UsageRecord, UsageStatus

            steps_by_kbd_signal = {(step.kbd_id, step.signal_id): step for step in steps_executed}
            async with self._db_session_factory() as session:
                loader = DynamicResourceLoader(session)
                for kbd_id, kbd in plan.candidates.items():
                    revision_info = kbd.resource_revision or {}
                    revision = revision_info.get("revision")
                    resource_name = str(revision_info.get("resource_name") or kbd.id or kbd_id)
                    if not isinstance(revision, int):
                        logger.warning(
                            event="kbd_runtime_audit_skipped_missing_revision",
                            kbd_id=kbd_id,
                            resource_name=resource_name,
                        )
                        continue
                    try:
                        snapshot = await loader.get_revision("kbd", resource_name, revision)
                    except ResourceNotFoundError:
                        logger.warning(
                            event="kbd_runtime_audit_skipped_unknown_revision",
                            kbd_id=kbd_id,
                            resource_name=resource_name,
                            revision=revision,
                        )
                        continue

                    assessment = assessments[kbd_id]
                    signal_rows: list[dict[str, Any]] = []
                    for ref in sorted(
                        (item for item in plan.signals.values() if item.kbd_id == kbd_id),
                        key=lambda item: item.ref_id,
                    ):
                        outcome = assessment.signal_outcomes.get(ref.ref_id, SignalOutcome.NOT_RUN)
                        step = steps_by_kbd_signal.get((kbd_id, ref.signal_id))
                        error = step.error if step is not None else None
                        if outcome is SignalOutcome.BLOCKED:
                            failure_mode = "dependency_unresolved"
                        elif outcome is SignalOutcome.ERROR:
                            failure_mode = "tool_execution_error"
                        elif outcome is SignalOutcome.UNKNOWN:
                            failure_mode = "matcher_indeterminate"
                        elif outcome is SignalOutcome.NOT_APPLICABLE:
                            failure_mode = "scope_not_applicable"
                        else:
                            failure_mode = None
                        signal_rows.append(
                            {
                                "signal_ref_id": ref.ref_id,
                                "signal_id": ref.signal_id,
                                "tool": _acquire_tool(ref.signal),
                                "role": ref.evidence_role.value,
                                "outcome": outcome.value,
                                "failure_mode": failure_mode,
                                "exec_id": step.exec_id if step is not None else None,
                                "evaluation_id": step.evaluation_id if step is not None else None,
                                "error": smart_truncate(error or "", max_chars=300) or None,
                            }
                        )
                    compile_errors = list(plan.compile_errors.get(kbd_id) or [])
                    has_execution_failure = bool(compile_errors) or any(
                        row["outcome"] in {SignalOutcome.ERROR.value, SignalOutcome.BLOCKED.value}
                        for row in signal_rows
                    )
                    audit_exec_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{plan.plan_id}:kbd:{kbd_id}"))
                    replay_manifest = build_kbd_replay_manifest(
                        resource={
                            "resource_type": snapshot.resource_type,
                            "resource_name": snapshot.resource_name,
                            "revision": snapshot.revision,
                            "checksum": snapshot.checksum,
                        },
                        plan_id=plan.plan_id,
                        snapshot_id=plan.snapshot_id,
                        environment=environment,
                        signal_outcomes=signal_rows,
                        steps_by_signal=steps_by_kbd_signal,
                        kbd_id=kbd_id,
                    )
                    await loader.audit_usage(
                        snapshot,
                        UsageRecord(
                            consumer="agent-service.kbd_differential",
                            status=UsageStatus.FAILED if has_execution_failure else UsageStatus.SUCCESS,
                            conversation_id=self._conversation_id or session_id,
                            case_id=self._case_id,
                            trace_id=get_current_trace_id(),
                            exec_id=audit_exec_id,
                            input_payload={
                                "plan_id": plan.plan_id,
                                "snapshot_id": plan.snapshot_id,
                                "candidate_id": kbd_id,
                                "signal_ids": [row["signal_id"] for row in signal_rows],
                            },
                            output_payload={
                                "conclusion_level": decision.level.value,
                                "candidate_state": assessment.state.value,
                                "compile_status": "failed" if compile_errors else "passed",
                                "signal_outcomes": [
                                    {key: row[key] for key in ("signal_id", "outcome", "failure_mode")}
                                    for row in signal_rows
                                ],
                            },
                            error="; ".join(compile_errors) if compile_errors else None,
                            metadata={
                                "schema_version": 1,
                                "event_type": "kbd_diagnostic_outcome",
                                "plan_id": plan.plan_id,
                                "snapshot_id": plan.snapshot_id,
                                "candidate_state": assessment.state.value,
                                "conclusion_level": decision.level.value,
                                "compile": {
                                    "status": "failed" if compile_errors else "passed",
                                    "compiler": "agent-service.cdd.plan_compiler",
                                    "errors": compile_errors,
                                },
                                "signal_outcomes": signal_rows,
                                # 最小 replay artifact 契约：仅存不可变版本、哈希和 artifact
                                # 查找键。它明确标为 not replayable，不能把现有运行审计说成
                                # 已完成 Evidence/Execution Replay。
                                "replay_manifest": replay_manifest,
                            },
                        ),
                    )
                await session.commit()
        except Exception as exc:
            logger.warning(event="kbd_runtime_outcome_audit_failed", error=str(exc), session_id=session_id)

    @staticmethod
    def _unresolved_placeholders(args: dict[str, Any]) -> set[str]:
        unresolved: set[str] = set()
        for value in args.values():
            if isinstance(value, str):
                unresolved.update(match.group(1) for match in _PLACEHOLDER_RE.finditer(value))
        return unresolved

    @staticmethod
    def _stable_exec_id(session_id: str, kbd: KBD, signal_id: str) -> str:
        revision = str((kbd.resource_revision or {}).get("revision") or "0")
        value = f"{session_id}:{kbd.id}:{revision}:{signal_id}"
        return str(uuid.uuid5(uuid.NAMESPACE_URL, value))

    @staticmethod
    def _stable_acquisition_exec_id(session_id: str, runtime_key: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{session_id}:acquisition:{runtime_key}"))

    @staticmethod
    def _stable_evaluation_id(exec_id: str, signal_ref_id: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"{exec_id}:evaluation:{signal_ref_id}"))

    @staticmethod
    def _acquisition_key(tool: str, args: dict[str, Any], env_context: dict[str, str]) -> str:
        material = {
            "tool": tool,
            "args": args,
            "target": env_context.get("node_ip") or args.get("host"),
            "scope": args.get("scope") or args.get("container"),
            "tool_version": "runtime",
            "policy_version": "cdd-v1",
        }
        return json.dumps(material, sort_keys=True, ensure_ascii=False, separators=(",", ":"))

    def _evaluate_signal_outcome(
        self,
        signal: dict[str, Any],
        raw_output: str | None,
        error: str | None,
        pre_matched: bool | None,
    ) -> SignalOutcome:
        if error:
            return SignalOutcome.ERROR
        if raw_output is None:
            return SignalOutcome.UNKNOWN
        matcher = signal.get("match")
        if matcher is None:
            if _signal_category(signal) == "frontend":
                return SignalOutcome.SATISFIED if pre_matched is True else SignalOutcome.UNKNOWN
            produces = (signal.get("orchestrate") or {}).get("produces") or []
            if produces:
                return SignalOutcome.SATISFIED if pre_matched is True else SignalOutcome.CONTRADICTED
            return SignalOutcome.UNKNOWN
        if pre_matched is not None:
            return SignalOutcome.SATISFIED if pre_matched else SignalOutcome.CONTRADICTED
        if not isinstance(matcher, dict):
            return SignalOutcome.UNKNOWN
        try:
            evaluated = self._evaluate_matcher(matcher, raw_output)
        except Exception as exc:
            logger.exception(
                event="matcher_evaluation_failed",
                error=exc,
                error_code="KBD_MATCHER_EVALUATION_FAILED",
                signal_id=signal.get("id") or signal.get("signal_id"),
                conversation_id=self._conversation_id,
                case_id=self._case_id,
                matcher_type=matcher.get("type"),
                expected=matcher.get("expected", True),
            )
            return SignalOutcome.UNKNOWN
        if evaluated is None:
            logger.info(
                event="matcher_evaluation_inconclusive",
                error_code="KBD_MATCHER_INCONCLUSIVE",
                signal_id=signal.get("id") or signal.get("signal_id"),
                conversation_id=self._conversation_id,
                case_id=self._case_id,
                matcher_type=matcher.get("type"),
                expected=matcher.get("expected", True),
            )
            return SignalOutcome.UNKNOWN
        return SignalOutcome.SATISFIED if evaluated else SignalOutcome.CONTRADICTED

    @staticmethod
    def _tool_result_metadata(result: StepResult) -> dict[str, Any]:
        if result.outcome in (SignalOutcome.SATISFIED, SignalOutcome.CONTRADICTED):
            status = "success"
        elif result.outcome is SignalOutcome.BLOCKED:
            status = "blocked"
        else:
            status = "failed"
        output = smart_truncate(result.raw_output or "", max_chars=2000)
        return {
            "exec_id": result.exec_id,
            "evaluation_id": result.evaluation_id,
            "acquisition_id": result.acquisition_id,
            "tool_name": result.tool_name,
            "args": result.tool_args,
            "tool_args": result.tool_args,
            "result": output,
            "tool_result": output,
            "status": status,
            "error": result.error,
            "error_type": "blocked_dependency" if result.outcome is SignalOutcome.BLOCKED else None,
            "outcome": result.outcome.value,
            "kbd_id": result.kbd_id,
            "signal_id": result.signal_id,
            "produced_variables": result.produced_variables,
            "ai_value": result.ai_value,
        }

    @staticmethod
    def _resolve_args(
        template: dict,
        env_context: dict[str, str] | None = None,
        variable_pool: dict | None = None,
    ) -> dict:
        """将 args 模板中的 {{NAME}} 替换为 env_context ∪ variable_pool 中的实际值。

        ADR-2：统一占位符为 {{VAR}}。运行期把“环境上下文”与“会话变量池（黑板）”合并后做单遍
        正则替换（同一套渲染、同一个池）；未命中（如生产者尚未产出该变量）的占位符保留原样。

        示例：
            template = {"scope": "{{HOST}}"}
            variable_pool = {"HOST": "node-001"}
            → {"scope": "node-001"}
        """
        merged: dict[str, Any] = dict(env_context or {})
        if variable_pool:
            merged.update(variable_pool)

        # 防御性归一：占位符解析大小写不敏感（ADR-2）。
        # 生产者（QKV/tool_def）可能以任意大小写写入变量池（如 HOST/host/HoSt），
        # 消费侧统一按小写查找，确保 {{HOST}}/{{host}}/{{Host}} 均可命中池内小写
        # Key，无需依赖「produces 名必须大写」的全局隐式约定。
        lower_merged = {k.lower(): v for k, v in merged.items()}

        def _rep(m: re.Match[str]) -> str:
            name = m.group(1).strip().lower()
            return str(lower_merged[name]) if name in lower_merged else m.group(0)

        resolved = {}
        for k, v in template.items():
            if isinstance(v, str):
                v = _PLACEHOLDER_RE.sub(_rep, v)
            resolved[k] = v
        return resolved

    # ─── Matcher 类型化求值（§6：5 类定型 valuator）────────────────────────────

    def _evaluate_matcher(self, matcher: dict[str, Any], actual_output: str) -> bool | None:
        """对单条 Matcher 契约做确定性（非 LLM）布尔求值（委托单一真相源）。

        返回 True/False 表示“符合期望”；返回 None 表示无法定值并保持 UNKNOWN。
        支持类型：keyword / regex / state / threshold / delta / trend / exists。

        实现已统一迁移至 app.tools.qfk.matcher.evaluate_matcher，此处仅作委托，
        确保 KBD 差异诊断与 QFK 引擎使用同一套求值逻辑、证据链与 or/and/not 语义，
        消除此前两份 keyword 实现可能漂移的隐患。
        """
        from shared.signals.matcher import evaluate_matcher

        return evaluate_matcher(matcher, actual_output).matched

    # ─── 阶段 A/B：生产者/消费者执行与变量池 ───────────────────────────────────

    async def _run_producers(
        self,
        candidates: list[KBD],
        env_context: dict[str, str],
        session_id: str,
    ) -> None:
        """阶段 A：跑全部生产者信号（QKV），填充会话变量池（黑板）。

        去重（acquirer + args 相同只跑一次）；执行失败仅告警，不阻断诊断。
        """
        producers: list[dict[str, Any]] = []
        seen: set[str] = set()
        for kbd in candidates:
            for s in kbd.signals:
                if _signal_category(s) != "frontend":
                    continue
                key = (
                    _acquire_tool(s),
                    json.dumps((s.get("acquire") or {}).get("args") or {}, sort_keys=True, ensure_ascii=False),
                )
                if key in seen:
                    continue
                seen.add(key)
                producers.append(s)

        for s in producers:
            try:
                fsignal = self._signal_to_qkv(s, env_context)
                if fsignal is None:
                    logger.warning(
                        event="kbd_diag_producer_skip",
                        acquirer=_acquire_tool(s),
                        session_id=session_id,
                    )
                    continue
                from app.tools.qkv.engine import qkv_exec

                res = await qkv_exec(
                    signal=fsignal,
                    conversation_id=self._conversation_id or session_id,
                    node_ip=env_context.get("node_ip"),
                    exec_id=None,
                )
                if res.success:
                    await self._fill_pool_from_qkv(
                        s,
                        res,
                        node_ip=env_context.get("node_ip"),
                        execution_mode=env_context.get("execution_mode"),
                        session_id=session_id,
                    )
                else:
                    logger.warning(
                        event="kbd_diag_producer_failed",
                        acquirer=_acquire_tool(s),
                        error=res.error,
                        session_id=session_id,
                    )
            except Exception as exc:
                logger.warning(
                    event="kbd_diag_producer_error",
                    acquirer=_acquire_tool(s),
                    error=str(exc),
                    session_id=session_id,
                )

    @staticmethod
    def _is_ip_address(value: Any) -> bool:
        """判断值是否已经是 IPv4/IPv6 地址。"""
        if not isinstance(value, str) or not value.strip():
            return False
        try:
            ipaddress.ip_address(value.strip())
        except ValueError:
            return False
        return True

    async def _resolve_host_ip(
        self,
        host: Any,
        *,
        node_ip: str | None,
        execution_mode: str | None = None,
        session_id: str,
    ) -> Any:
        """将 QKV 返回的节点名称/ID解析为 HCI 节点 IP。

        task/alert 接口的 ``host`` 字段是可读节点名称（如 ``SVR_aCloud_668``），
        不能直接作为终端桥的路由地址。通过同一 HCI 会话执行只读的
        ``acli --formatter json platform node list``，同时兼容按 name、hostname、id
        匹配；解析失败时保留原值并记录告警，避免破坏已有事实数据。
        """
        if not isinstance(host, str):
            return host
        host = host.strip()
        if not host or self._is_ip_address(host):
            return host
        if host in self._host_ip_cache:
            return self._host_ip_cache[host]

        # sim-ssh 的 node_ip 来自 hci-sim 权威 TestRun 上下文，通常是 K3s
        # Service DNS 而非客户节点 IP。逻辑 HOST 只用于 Fixture 变量匹配；应
        # 绑定到已认证的仿真端点，不能再套用真实 HCI 的 IP 发现流程。
        if execution_mode == "sim-ssh" and isinstance(node_ip, str) and node_ip.strip():
            endpoint = node_ip.strip()
            self._host_ip_cache[host] = endpoint
            logger.info(
                event="kbd_sim_host_bound",
                host=host,
                endpoint=endpoint,
                session_id=session_id,
            )
            return endpoint

        from app.tools.acli.executor import _executor

        if _executor is None:
            logger.warning(
                event="kbd_host_ip_resolve_skipped",
                host=host,
                reason="BridgeRelayExecutor 尚未初始化",
                session_id=session_id,
            )
            return host

        command = "acli --formatter json platform node list"
        try:
            result = await _executor.execute(
                tool_name="acli_exec",
                args={"command": command, "reason": "将 QKV 节点名称解析为节点 IP"},
                conversation_id=self._conversation_id or session_id,
                node_ip=node_ip,
                case_id=self._case_id or "",
                risk_level=1,
                policy="auto",
            )
            if result.exit_code not in (0, None):
                logger.warning(
                    event="kbd_host_ip_resolve_failed",
                    host=host,
                    node_ip=node_ip,
                    exit_code=result.exit_code,
                    error=(result.stderr or result.stdout or "").strip()[:300],
                    session_id=session_id,
                )
                return host
            payload = json.loads(result.stdout or "")
        except Exception as exc:
            logger.warning(
                event="kbd_host_ip_resolve_error",
                host=host,
                node_ip=node_ip,
                error=str(exc),
                session_id=session_id,
            )
            return host

        rows = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(rows, list):
            logger.warning(
                event="kbd_host_ip_resolve_invalid_payload",
                host=host,
                session_id=session_id,
                payload_type=type(payload).__name__,
            )
            return host

        for row in rows:
            if not isinstance(row, dict):
                continue
            aliases = {str(row.get(key) or "").strip() for key in ("name", "hostname", "id") if row.get(key)}
            address = str(row.get("ip") or "").strip()
            if host in aliases and self._is_ip_address(address):
                self._host_ip_cache[host] = address
                logger.info(
                    event="kbd_host_ip_resolved",
                    host=host,
                    ip=address,
                    session_id=session_id,
                )
                return address

        logger.warning(
            event="kbd_host_ip_not_found",
            host=host,
            session_id=session_id,
        )
        return host

    async def _normalize_qkv_values(
        self,
        values: list[dict[str, Any]],
        *,
        node_ip: str | None,
        execution_mode: str | None = None,
        session_id: str,
    ) -> list[dict[str, Any]]:
        """归一化 QKV 结果中的 HOST，并返回用于变量池与报告的结果副本。"""
        normalized: list[dict[str, Any]] = []
        for item in values:
            if not isinstance(item, dict):
                normalized.append(item)
                continue
            current = dict(item)
            for key, value in item.items():
                if str(key).strip().lower() == "host":
                    current[key] = await self._resolve_host_ip(
                        value,
                        node_ip=node_ip,
                        execution_mode=execution_mode,
                        session_id=session_id,
                    )
            normalized.append(current)
        return normalized

    async def _fill_pool_from_qkv(
        self,
        signal: dict[str, Any],
        res: Any,
        *,
        node_ip: str | None = None,
        execution_mode: str | None = None,
        session_id: str = "",
    ) -> None:
        """将 QKV 产出(produces)写入变量池：produces=[{name, path}] -> pool[name]=value。

        注意：parse_frontend_value._extract_by_produces 返回的 dict key 是 name.lower()，
        所以这里用 name.lower() 查找，而非 path（path 是原始 JSON 字段路径，用于提取阶段）。

        让 tool_definition 生效：当 signals_json 未配置 produces 时，回退到
        admin-ui 配置的 tool_definition 默认值（与 _signal_to_qkv 保持一致）。
        """
        produces = (signal.get("orchestrate") or {}).get("produces")
        if not produces:
            produces = self._tool_def_default(_acquire_tool(signal), "produces") or []
        if not res.values:
            return
        res.values = await self._normalize_qkv_values(
            res.values,
            node_ip=node_ip,
            execution_mode=execution_mode,
            session_id=session_id or self._conversation_id or "",
        )
        first = res.values[0]
        # 同一案例允许 qkv_alert/qkv_task 作为替代证据。任务记录包含更完整的执行
        # 上下文，故标准变量采用 task > alert > dialog 的确定性优先级，与执行顺序无关。
        producer_priority = {
            "qkv_task": 30,
            "qkv_alert": 20,
            "qkv_dialog": 10,
        }.get(_acquire_tool(signal), 0)
        for spec in produces:
            name = spec.get("name") if isinstance(spec, dict) else None
            if not name:
                continue
            # 提取后的 dict key 是 name.lower()（见 parser._extract_by_produces line 90）
            val = first.get(name.lower()) if isinstance(first, dict) else None
            if val is not None:
                self._set_pool_var(name, val, producer_priority=producer_priority)

    async def _execute_acquirer(
        self,
        step: KBDStep,
        env_context: dict[str, str],
        session_id: str,
        user_id: str,
        *,
        signal: dict[str, Any] | None = None,
        exec_id: str | None = None,
    ) -> tuple[str | None, str | None, bool | None, Any | None]:
        """按 acquirer 路由执行：qkv→QKV 引擎 / qfk→QFK 引擎 / 其他→通用 tool_executor。

        返回 (raw_output, error, pre_matched, ai_value)：
          - raw_output: 供确定性 matcher 求值的文本（None 表示执行失败）
          - error: 错误信息
          - pre_matched: qfk keyword 类型已由引擎判定时直接给出布尔，否则 None
          - ai_value: 已通过引用行逐字回查的 AI 提取值；非 AI Matcher 时为 None
        """
        acquirer = step.tool_name

        # P2 增强：信号匹配日志 - 记录即将执行的信号
        logger.info(
            event="signal_executing",
            acquirer=acquirer,
            tool_args_template=step.tool_args_template,
            session_id=session_id,
            conversation_id=self._conversation_id,
        )

        if acquirer.startswith("qkv_"):
            # KBD 确定性诊断必须执行信号声明的 QKV acquisition，不能用会话预取的
            # task_logs/alerts 静默替代。预取上下文用于 S0 分类；KBD 证据必须具有
            # 独立 exec_id、实际命令和现场返回值，才能审计并避免陈旧/截断数据误命中。
            try:
                signal_context = dict(env_context)
                signal_context.update(self._variable_pool)
                fsignal = (
                    self._signal_to_qkv(signal, signal_context)
                    if signal is not None
                    else self._signal_to_qkv_from_step(step, signal_context)
                )
                if fsignal is None:
                    logger.warning(
                        event="signal_build_failed",
                        acquirer=acquirer,
                        reason="无法构建前端信号",
                        session_id=session_id,
                    )
                    return None, "无法构建前端信号", None, None
                from app.tools.qkv.engine import qkv_exec

                res = await qkv_exec(
                    signal=fsignal,
                    conversation_id=self._conversation_id or session_id,
                    node_ip=env_context.get("node_ip"),
                    exec_id=exec_id,
                )
                if not res.success:
                    logger.warning(
                        event="signal_exec_failed",
                        acquirer=acquirer,
                        error=res.error,
                        session_id=session_id,
                    )
                    return None, res.error, None, None
                if signal is not None:
                    await self._fill_pool_from_qkv(
                        signal,
                        res,
                        node_ip=env_context.get("node_ip"),
                        execution_mode=env_context.get("execution_mode"),
                        session_id=session_id,
                    )
                else:
                    res.values = await self._normalize_qkv_values(
                        res.values,
                        node_ip=env_context.get("node_ip"),
                        execution_mode=env_context.get("execution_mode"),
                        session_id=session_id,
                    )
                    self._fill_pool_from_qkv_on_step(step, res)

                # P2: 信号执行成功日志
                logger.info(
                    event="signal_exec_success",
                    acquirer=acquirer,
                    values_count=len(res.values) if res.values else 0,
                    session_id=session_id,
                )
                return res.to_observation(), None, bool(res.values), None
            except Exception as exc:
                logger.error(
                    event="signal_exec_exception",
                    acquirer=acquirer,
                    error=str(exc),
                    session_id=session_id,
                )
                return None, str(exc), None, None

        if acquirer.startswith("qfk_"):
            # 消费者：QFK 引擎取数并判定；keyword 类型直接采信引擎布尔。
            try:
                resolver_variables = dict(env_context)
                resolver_variables.update(self._variable_pool)
                produces = ((signal or {}).get("orchestrate") or {}).get("produces") or []
                bsignal = self._signal_to_qfk(step, resolver_variables, produces=produces)
                if bsignal is None:
                    return None, "无法构建后端信号", None, None
                from app.tools.qfk.engine import qfk_exec

                matcher = step.matcher or {}
                matcher_extract = matcher.get("extract") if isinstance(matcher, dict) else None
                required_output_sources = {
                    str((item.get("extract") or {}).get("source") or "stdout")
                    for item in produces
                    if isinstance(item, dict)
                }
                if isinstance(matcher_extract, dict):
                    required_output_sources.add(str(matcher_extract.get("source") or "stdout"))
                output_filters: list[dict[str, Any]] = []
                extract_specs = [
                    item.get("extract")
                    for item in produces
                    if isinstance(item, dict) and isinstance(item.get("extract"), dict)
                ]
                if isinstance(matcher_extract, dict):
                    extract_specs.append(matcher_extract)
                for extract_spec in extract_specs:
                    if not isinstance(extract_spec, dict):
                        continue
                    resolved_extract = self._resolve_template_value(extract_spec, resolver_variables)
                    # 边缘执行器只做安全的逐行筛选；列提取、基数校验和类型转换仍由
                    # Agent 的确定性 extractor 完成。无筛选条件时不前移，后端按大小
                    # 上限 Fail Closed，避免把“全量输出”伪装成过滤结果。
                    rows = resolved_extract.get("rows") or {}
                    structured_keywords = (
                        rows
                        if rows.get("mode") == "keywords" and not isinstance(resolved_extract.get("header"), dict)
                        else {}
                    )
                    include = structured_keywords.get("include") or []
                    exclude = structured_keywords.get("exclude") or []
                    if include or exclude:
                        output_filters.append(
                            {
                                "source": str(resolved_extract.get("source") or "stdout"),
                                "include": list(include),
                                "exclude": list(exclude),
                                "include_mode": str(structured_keywords.get("include_mode") or "all"),
                                "exclude_mode": str(structured_keywords.get("exclude_mode") or "any"),
                                "case_sensitive": structured_keywords.get("case_sensitive", True),
                            }
                        )

                target_node_ip = env_context.get("node_ip")
                if bsignal.host:
                    resolved_host = await self._resolve_host_ip(
                        bsignal.host,
                        node_ip=target_node_ip,
                        execution_mode=env_context.get("execution_mode"),
                        session_id=session_id,
                    )
                    if env_context.get("execution_mode") != "sim-ssh" and not self._is_ip_address(resolved_host):
                        return (
                            None,
                            f"QFK_TARGET_HOST_UNRESOLVED: 无法把目标 HOST={bsignal.host} 解析为节点 IP，"
                            "为避免在当前主机误查，已停止执行",
                            None,
                            None,
                        )
                    target_node_ip = str(resolved_host)

                res = await qfk_exec(
                    signal=bsignal,
                    conversation_id=self._conversation_id or session_id,
                    # QKV 生产者的 HOST 或专家指定主机名已解析为节点 IP；解析失败会在
                    # 上方 fail closed，绝不静默回退当前节点造成跨主机误查。
                    node_ip=target_node_ip,
                    case_id=self._case_id,  # 透传工单 ID，确保 terminal_bridge 能路由到正确的 SSH 会话
                    signal_id=str((signal or {}).get("id") or "") or None,
                    exec_id=exec_id,
                    required_output_sources=required_output_sources,
                    output_filters=output_filters,
                    execution_mode="produce" if produces else "match",
                    ai_client=self._ai_registry.get_client(self._assistant_type),
                )
                if res.error:
                    return res.raw_output or None, res.error, None, None
                if produces:
                    outputs = (
                        res.complete_outputs
                        if hasattr(res, "complete_outputs")
                        else {"stdout": res.raw_output}  # 兼容测试桩/旧扩展返回对象
                    )
                    ai_values, ai_error = await self._extract_ai_values_from_qfk(
                        produces,
                        outputs,
                        context_variables=env_context,
                    )
                    if ai_error:
                        return res.raw_output, ai_error, None, None
                    ok, extract_error = self._fill_pool_from_qfk(
                        produces,
                        outputs,
                        context_variables=env_context,
                        ai_values=ai_values,
                    )
                    if not ok:
                        return res.raw_output, extract_error, None, None
                    # 产出变量模式只关心命令是否成功且结果是否已写入变量池，不再进行 matcher 判定。
                    return res.raw_output, None, True, None
                # qfk_exec 是全部 Matcher 的唯一求值入口；配置 Extract 时它会从
                # complete_outputs 取值，禁止调用方再次从展示摘要重复求值。
                if isinstance(matcher, dict):
                    return res.raw_output, None, res.matched, res.ai_value
                return res.raw_output, None, None, None
            except Exception as exc:
                return None, str(exc), None, None

        # 遗留/通用工具（acli_* 等）：走既有 tool_executor
        try:
            args = self._resolve_args(step.tool_args_template, env_context, self._variable_pool)
            result = await self._tool_executor.execute(acquirer, args)
            return (str(result) if result is not None else "", None, None, None)
        except Exception as exc:
            return None, str(exc), None, None

    def _fill_pool_from_qfk(
        self,
        produces: list[Any],
        complete_outputs: dict[str, str] | str,
        context_variables: dict[str, Any] | None = None,
        ai_values: dict[str, Any] | None = None,
    ) -> tuple[bool, str | None]:
        """将 QFK 命令结果按产出变量约定写入变量池。

        每个变量都必须声明新版 extract。所有产出只读取完整物理流，取值失败时
        显式报错；所有变量先完成取值后才会原子写入变量池。
        """
        from shared.signals.extractor import QFKExtractionError, extract_value

        # 兼容直接调用该私有辅助的既有测试/扩展；真实 QFK 路径始终传完整物理流字典。
        if isinstance(complete_outputs, str):
            complete_outputs = {"stdout": complete_outputs}
        resolver_variables = dict(context_variables or {})
        resolver_variables.update(self._variable_pool)
        ai_values = ai_values or {}

        pending_values: list[tuple[str, Any]] = []
        valid_specs = [item for item in produces if isinstance(item, dict) and str(item.get("name") or "").strip()]
        if not valid_specs:
            return False, "QFK 产出变量未配置有效 name"

        for spec in valid_specs:
            name = str(spec["name"]).strip()
            if name in ai_values:
                pending_values.append((name, ai_values[name]))
                continue
            extract_spec = spec.get("extract")
            if not isinstance(extract_spec, dict):
                return False, f"QFK_EXTRACT_INVALID_SPEC: QFK 产出变量 {name} 必须配置新版 extract"
            resolved_extract = self._resolve_template_value(extract_spec, resolver_variables)
            source = str(resolved_extract.get("source") or "stdout")
            if source not in complete_outputs:
                return False, f"QFK_EXTRACT_INVALID_SPEC: 未取得完整 {source} 输出"
            try:
                value = extract_value(
                    complete_outputs[source],
                    resolved_extract,
                    str(spec.get("type") or "string"),
                )
            except QFKExtractionError as exc:
                return False, f"QFK 产出变量 {name} 提取失败：{exc}"
            pending_values.append((name, value))
        for name, value in pending_values:
            self._set_pool_var(name, value)
        return True, None

    async def _extract_ai_values_from_qfk(
        self,
        produces: list[Any],
        complete_outputs: dict[str, str] | str,
        context_variables: dict[str, Any] | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        """统一执行 QFK 产出变量的 AI 提取，并在原子写池前完成溯源校验。"""

        from shared.signals.extractor import QFKExtractionError

        from app.tools.qfk.ai_extractor import extract_ai_value, has_ai_extract

        if isinstance(complete_outputs, str):
            complete_outputs = {"stdout": complete_outputs}
        resolver_variables = dict(context_variables or {})
        resolver_variables.update(self._variable_pool)
        ai_client = self._ai_registry.get_client(self._assistant_type)
        values: dict[str, Any] = {}
        for spec in produces:
            if not isinstance(spec, dict) or not str(spec.get("name") or "").strip():
                continue
            extract_spec = spec.get("extract")
            if not has_ai_extract(extract_spec):
                continue
            name = str(spec["name"]).strip()
            resolved_extract = self._resolve_template_value(extract_spec, resolver_variables)
            source = str(resolved_extract.get("source") or "stdout")
            if source not in complete_outputs:
                return {}, f"QFK_EXTRACT_INVALID_SPEC: 未取得完整 {source} 输出"
            try:
                result = await extract_ai_value(
                    complete_outputs[source],
                    resolved_extract,
                    str(spec.get("type") or "string"),
                    ai_client,
                    conversation_id=self._conversation_id or "",
                    case_id=self._case_id or "",
                )
            except QFKExtractionError as exc:
                return {}, f"QFK 产出变量 {name} AI 提取失败：{exc}"
            values[name] = result.value
            logger.info(
                event="qfk_ai_extract_produce_ready",
                name=name,
                candidate_count=result.candidate_count,
                evidence_line_numbers=result.evidence_line_numbers,
                conversation_id=self._conversation_id or "",
            )
        return values, None

    @classmethod
    def _resolve_template_value(cls, value: Any, variable_pool: dict[str, Any]) -> Any:
        """递归渲染 extract 中 include/exclude 等位置的 ``{{VAR}}``。"""
        if isinstance(value, str):
            return cls._resolve_args({"value": value}, variable_pool=variable_pool)["value"]
        if isinstance(value, list):
            return [cls._resolve_template_value(item, variable_pool) for item in value]
        if isinstance(value, dict):
            return {key: cls._resolve_template_value(item, variable_pool) for key, item in value.items()}
        return value

    @staticmethod
    def _qkv_values_from_context(
        signal: dict[str, Any] | None,
        env_context: dict[str, Any],
    ) -> tuple[list[dict[str, Any]], str] | None:
        """从 conversation 已预取的结构化事实执行 QKV，不伪造不存在的数据。"""
        if not signal:
            return None
        acquire = signal.get("acquire") or {}
        tool = str(acquire.get("tool") or "")
        query_type = tool.split("_", 1)[1] if "_" in tool else ""
        source_key = f"{query_type}_logs"
        raw_items = env_context.get(source_key)
        if raw_items is None:
            return None
        if isinstance(raw_items, str):
            try:
                raw_items = json.loads(raw_items)
            except json.JSONDecodeError:
                return ([], source_key)
        if isinstance(raw_items, dict):
            raw_items = raw_items.get("data") or raw_items.get("items") or [raw_items]
        if not isinstance(raw_items, list):
            return ([], source_key)

        args = acquire.get("args") or {}
        keyword = str(args.get("keyword") or "").strip()
        keyword_base = keyword.replace("失败", "").strip()
        require_failed = bool(args.get("is_failed"))
        limit = max(1, min(int(args.get("limit") or 100), 200))
        produces = (signal.get("orchestrate") or {}).get("produces") or []

        def _is_failed(item: dict[str, Any]) -> bool:
            text = json.dumps(item, ensure_ascii=False).lower()
            return (
                item.get("status") in (3, "3", "failed", "失败")
                or str(item.get("process") or "").lower() in ("failed", "失败")
                or "失败" in text
            )

        matched: list[dict[str, Any]] = []
        for item in raw_items:
            if not isinstance(item, dict):
                continue
            text = json.dumps(item, ensure_ascii=False)
            keyword_match = not keyword or keyword in text or (keyword_base and keyword_base in text)
            if not keyword_match or (require_failed and not _is_failed(item)):
                continue
            values: dict[str, Any] = {}
            for spec in produces:
                if not isinstance(spec, dict) or not spec.get("name"):
                    continue
                current: Any = item
                for part in str(spec.get("path") or "").split("."):
                    current = current.get(part) if isinstance(current, dict) else None
                    if current is None:
                        break
                if current is not None:
                    values[str(spec["name"]).lower()] = current
            matched.append(values or item)
            if len(matched) >= limit:
                break
        return matched, source_key

    def _tool_def_default(self, tool_name: str, key: str) -> Any | None:
        """从 tool_definition 注册表读取某参数的默认值（produces / matcher 等）。

        这是「让 tool_definition 生效」的关键闭环：当 SOP/signals_json 未填写
        produces / matcher 时，回退到运营在 admin-ui 配置并写入 tool_definition
        的默认值。运营人员无需改 signals_json 即可调整默认产出 / 判定。
        """
        from app.adapters.agents.htp.tool_registry import TOOL_REGISTRY

        tool = TOOL_REGISTRY.get(tool_name)
        if not tool or not isinstance(tool.parameters, dict):
            return None
        prop = tool.parameters.get("properties", {}).get(key)
        if isinstance(prop, dict):
            return prop.get("default")
        return None

    def _signal_to_qkv(self, signal: dict[str, Any], env_context: dict[str, str]) -> Any:
        """从生产者信号 dict 构造 qkv/signal.FrontendSignal（解析占位符后再构造）。"""
        from app.tools.qkv.signal import FrontendQueryType, FrontendSignal

        acquire = signal.get("acquire") or {}
        acquirer = acquire.get("tool", "")
        parts = acquirer.split("_", 1)
        if len(parts) != 2 or parts[0] != "qkv":
            return None
        try:
            query = FrontendQueryType(parts[1])
        except ValueError:
            return None
        args = self._resolve_args(acquire.get("args") or {}, env_context, {})
        produces = (signal.get("orchestrate") or {}).get("produces")
        if not produces:
            # 让 tool_definition 生效：signals_json 未配置 produces 时，
            # 回退到 admin-ui 配置的 tool_definition 默认值。
            produces = self._tool_def_default(acquirer, "produces") or []
        try:
            # 统一走 FrontendSignal.from_dict，确保“启动虚拟机失败”被规范化为
            # keyword="启动虚拟机" + is_failed=true，与 QKV 命令契约一致。
            return FrontendSignal.from_dict(
                {
                    "query": query.value,
                    "keyword": str(args.get("keyword", "")),
                    "is_failed": bool(args.get("is_failed", False)),
                    "limit": int(args.get("limit", 100)),
                    "paths": args.get("paths", ["/sf/log/today", "/sf/log/today/vt"]),
                    "context_lines": int(args.get("context_lines", 2)),
                    "produces": produces,
                }
            )
        except Exception:
            return None

    def _signal_to_qkv_from_step(self, step: KBDStep, env_context: dict[str, str]) -> Any:
        """从 KBDStep 构造前端信号（兜底路径）。"""
        return self._signal_to_qkv({"acquire": {"tool": step.tool_name, "args": step.tool_args_template}}, env_context)

    def _fill_pool_from_qkv_on_step(self, step: KBDStep, res: Any) -> None:
        """兜底填充变量池（从 step 的 matcher/args 推断 produces 信息）。"""
        # step 不携带 produces，仅做尽力而为：若 raw values 为单字段则按 acquirer 后缀写入
        if not res.values:
            return
        first = res.values[0]
        if isinstance(first, dict) and len(first) == 1:
            ((name, val),) = first.items()
            if val is not None:
                self._set_pool_var(name, val)

    def _signal_to_qfk(
        self,
        step: KBDStep,
        variables: dict[str, Any] | None = None,
        *,
        produces: list[Any] | None = None,
    ) -> Any:
        """从消费者 KBDStep 构造 qfk/signal.BackendSignal（namespace 字符串路由）。"""
        from app.tools.qfk.signal import BackendSignal

        acquirer = step.tool_name
        parts = acquirer.split("_", 1)
        if len(parts) != 2 or parts[0] != "qfk":
            return None
        namespace = parts[1]  # log/service/system/vm/...

        args = step.tool_args_template or {}
        if variables is not None:
            args = self._resolve_template_value(args, variables)

        # 获取 matcher
        matcher = step.matcher
        if not matcher:
            matcher = self._tool_def_default(acquirer, "matcher") or {}
        if variables is not None:
            matcher = self._resolve_template_value(matcher, variables)

        # 提取关键字
        keywords: list[str] = []
        if matcher.get("type") == "keyword":
            p = matcher.get("pattern", "")
            keywords = [p] if isinstance(p, str) else list(p or [])

        filter_keywords: list[str] = []
        extract_specs = [matcher.get("extract")] if isinstance(matcher.get("extract"), dict) else []
        extract_specs.extend(
            item.get("extract")
            for item in (produces or [])
            if isinstance(item, dict) and isinstance(item.get("extract"), dict)
        )
        for extract_spec in extract_specs:
            rows = (extract_spec or {}).get("rows") or {}
            if rows.get("mode") != "keywords":
                continue
            for item in rows.get("include") or []:
                literal = str(item).strip()
                if literal and literal not in filter_keywords:
                    filter_keywords.append(literal)

        # 容器默认值按 namespace 语义：qfk_service 为服务组(asv)。qfk_system
        # 未声明时由 aCLI 在 HOST-OS 默认执行，绝不默认把 acli 放进 asv-con。
        _container_default = "asv" if namespace == "service" else None

        # 构建 v2 扁平信号数据（字段名与 acquirer_args 契约一致）
        signal_data = {
            "namespace": namespace,
            "keyword": keywords,
            "match_mode": {"any": "or", "all": "and"}.get(
                str(matcher.get("mode", "or")).lower(), str(matcher.get("mode", "or")).lower()
            ),
            "expected": bool(matcher.get("expected", True)),
            # v2 扁平字段
            "instruction": args.get("instruction"),
            "host": args.get("host"),
            "timeout": args.get("timeout", DEFAULT_SIGNAL_TIMEOUT_SECONDS),
            # 特有字段
            "command": args.get("command"),
            "command_args": args.get("command_args") or [],
            "container": args.get("container", _container_default),
            "cluster": bool(args.get("cluster", False)),
            "formatter": args.get("formatter"),
            "resource_keyword": args.get("resource_keyword"),
            "file": args.get("file"),
            "path": args.get("path"),
            "time_window": args.get("time_window"),
            "source_family": args.get("source_family", "auto"),
            "parser": args.get("parser"),
            "request_id": args.get("request_id"),
            "context_lines": args.get("context_lines", 0),
            "include_archives": args.get("include_archives", False),
            "archive_precheck": args.get("archive_precheck"),
            "matcher": matcher or None,
            "filter_keywords": filter_keywords,
        }

        # qfk_service 契约用 resource_keyword(服务名)/command(动作)，而运行时 BackendSignal
        # 用 service/action 字段。PR#611 删除 _coerce_legacy_fields 后该映射遗漏，导致
        # signal.service 恒为 None、ServiceHandler 抛 CommandBuildError。此处补回映射。
        if namespace == "service":
            signal_data["service"] = args.get("service") or args.get("resource_keyword")
            signal_data["action"] = args.get("action") or args.get("command") or "status"

        try:
            return BackendSignal.from_dict(signal_data)
        except Exception:
            return None

    # KBD 详情超链接模板（前端路由，部署时可据实际路径调整）
    KBD_DETAIL_URL_TEMPLATE = (
        "https://support.sangfor.com.cn/cases/list?product_id=33&type=1&category_id={support_id}&isOpen=true"
    )

    async def _generate_report(
        self,
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
        evaluated_kbds: list[KBD] | None = None,
        user_id: str = "",
        exclusion_reasons: dict[str, list[str]] | None = None,
    ) -> str:
        """生成诊断报告：仅返回命中 KBD 的 root_cause / solution 原始文本 + 现场证据 + 标题超链接。

        设计原则（诊断只读）：agent 只做"确认是哪篇 KBD 并返回根因/方案原文"，
        不重述、不改写，避免 LLM 二次加工引入偏差。证据即现场关键信号确认结果。
        """
        return self._build_diagnostic_report(
            matched_kbds,
            steps_executed,
            evaluated_kbds=evaluated_kbds,
            exclusion_reasons=exclusion_reasons,
        )

    @staticmethod
    def _build_diagnostic_report(
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
        evaluated_kbds: list[KBD] | None = None,
        kbd_detail_url: str = KBD_DETAIL_URL_TEMPLATE,
        exclusion_reasons: dict[str, list[str]] | None = None,
    ) -> str:
        """构建诊断报告：根因/方案原文 + 证据 + KBD 标题可点击超链接。"""
        if not matched_kbds:
            candidate_links = []
            for kbd in evaluated_kbds or []:
                support_id = kbd.support_id or kbd.id
                url = kbd_detail_url.format(id=kbd.id, support_id=support_id)
                reasons = (exclusion_reasons or {}).get(str(kbd.id)) or []
                suffix = f"（未确认：{'；'.join(reasons)}）" if reasons else "（未确认）"
                candidate_links.append(f"- [参考案例 {support_id} - {kbd.name}]({url}){suffix}")
            evidence = [
                KBDDiagnostic._format_step_evidence(step, index) for index, step in enumerate(steps_executed, start=1)
            ]
            return (
                "### 诊断结论：证据不足\n\n"
                "没有任何 KBD 的全部必需关键信号均已满足，因此系统不会输出 KBD 根因或解决方案。\n\n"
                "**已评估参考案例（未确认）**：\n"
                + ("\n".join(candidate_links) if candidate_links else "- 无")
                + "\n\n"
                "**关键信号执行结果**：\n" + ("\n".join(evidence) if evidence else "- 无可执行证据")
            )
        blocks: list[str] = []
        for kbd in matched_kbds[:5]:
            support_id = kbd.support_id or kbd.id
            title_link = f"[{kbd.name}]({kbd_detail_url.format(id=kbd.id, support_id=support_id)})"
            evidence: list[str] = []
            for s in steps_executed:
                if s.kbd_id != kbd.id:
                    continue
                evidence.append(KBDDiagnostic._format_step_evidence(s, len(evidence) + 1))
            blocks.append(
                f"### 诊断结论：参考案例 {support_id} - {title_link}\n\n"
                f"**根因（原始文本）**：\n{kbd.root_cause or '（无）'}\n\n"
                f"**解决方案（原始文本）**：\n{kbd.solution or '（无）'}\n\n"
                f"**现场证据（关键信号确认）**：\n" + ("\n".join(evidence) if evidence else "- 无")
            )
        return "\n\n".join(blocks)

    @staticmethod
    def _format_step_evidence(step: StepResult, index: int = 1) -> str:
        """把内部执行记录转换为面向用户的关键信号结果。

        exec_id、evaluation_id、完整参数和原始 stdout/stderr 属于审计数据，继续保留在
        tool_result 与 diagnostic_item 中；主报告只展示用户能据此行动的检查说明、状态、
        结论和结构化产出，避免把大输出及执行器噪音直接倾倒到对话页面。
        """
        status_labels = {
            SignalOutcome.SATISFIED: ("✅", "已满足"),
            SignalOutcome.CONTRADICTED: ("❌", "与预期矛盾"),
            SignalOutcome.ERROR: ("⚠️", "执行失败"),
            SignalOutcome.BLOCKED: ("⏸️", "未执行"),
            SignalOutcome.UNKNOWN: ("⚠️", "无法判断"),
            SignalOutcome.NOT_APPLICABLE: ("ℹ️", "不适用"),
            SignalOutcome.NOT_RUN: ("⏸️", "未执行"),
        }
        icon, status = status_labels[step.outcome]
        instruction = KBDDiagnostic._single_line_report_text(step.tool_args.get("instruction"), max_chars=160)
        title = instruction or f"关键信号检查 {index}"

        if step.outcome is SignalOutcome.SATISFIED:
            check_result = "已成功取得并提取所需信息。" if step.produced_variables else "命令输出符合该信号的判定条件。"
        elif step.outcome is SignalOutcome.CONTRADICTED:
            check_result = "命令已执行，但结果不符合该信号的预期条件。"
        elif step.outcome is SignalOutcome.ERROR:
            check_result = KBDDiagnostic._friendly_step_error(step.error)
        elif step.outcome is SignalOutcome.BLOCKED:
            check_result = "缺少前置步骤产出的变量，本项未执行。"
        elif step.outcome is SignalOutcome.UNKNOWN:
            check_result = "已取得执行结果，但现有规则无法形成明确判断。"
        elif step.outcome is SignalOutcome.NOT_APPLICABLE:
            check_result = "该信号不适用于当前产品、版本、组件或拓扑。"
        else:
            check_result = "本项未执行。"

        lines = [
            f"#### {index}. {icon} {title}",
            "",
            f"- **状态**：{status}",
            f"- **检查结果**：{check_result}",
        ]
        if step.produced_variables:
            lines.append("- **提取信息**：")
            for name, value in step.produced_variables.items():
                label = KBDDiagnostic._variable_display_label(name)
                rendered = KBDDiagnostic._format_inline_code(value, max_chars=500)
                lines.append(f"  - **{label}（{str(name).upper()}）**：{rendered}")
        if step.ai_value is not None:
            rendered = KBDDiagnostic._format_inline_code(step.ai_value, max_chars=500)
            lines.append(f"- **AI 提取值**：{rendered}")
        elif step.outcome is SignalOutcome.BLOCKED:
            missing = KBDDiagnostic._missing_variable_names(step.error)
            if missing:
                lines.append(
                    "- **缺失信息**："
                    + "、".join(KBDDiagnostic._format_inline_code(name.upper(), max_chars=80) for name in missing)
                )
        return "\n".join(lines)

    @staticmethod
    def _single_line_report_text(value: Any, *, max_chars: int) -> str:
        text = re.sub(r"\s+", " ", str(value or "")).strip()
        return smart_truncate(text, max_chars=max_chars) if text else ""

    @staticmethod
    def _format_inline_code(value: Any, *, max_chars: int) -> str:
        if isinstance(value, (dict, list)):
            text = json.dumps(value, ensure_ascii=False, sort_keys=True)
        elif value is None:
            text = "（空）"
        else:
            text = str(value)
        text = KBDDiagnostic._single_line_report_text(text, max_chars=max_chars)
        longest_run = max((len(run) for run in re.findall(r"`+", text)), default=0)
        fence = "`" * (longest_run + 1)
        return f"{fence} {text} {fence}" if text.startswith("`") or text.endswith("`") else f"{fence}{text}{fence}"

    @staticmethod
    def _variable_display_label(name: str) -> str:
        labels = {
            "vm": "虚拟机 ID",
            "host": "目标主机",
            "end": "发生时间",
            "pid": "进程 PID",
            "cmd": "进程命令",
            "pname": "进程信息",
        }
        normalized = str(name).strip().lower()
        return labels.get(normalized, "产出变量")

    @staticmethod
    def _friendly_step_error(error: str | None) -> str:
        text = KBDDiagnostic._single_line_report_text(error, max_chars=240)
        if "QFK_OUTPUT_EMPTY" in text:
            return "命令没有返回可用于提取变量的内容。"
        if "QFK_NO_MATCH" in text:
            return "命令输出中没有内容满足已配置的筛选条件。"
        if "QFK_MULTIPLE_MATCHES" in text:
            return "命令输出命中多条内容，无法按当前配置唯一确定结果。"
        if "QFK_COLUMN_OUT_OF_RANGE" in text:
            return "命令输出的列结构与产出变量配置不一致。"
        if "QFK_OUTPUT_TOO_LARGE" in text:
            return "命令输出超过当前安全处理上限，未使用不完整数据进行判断。"
        if "QFK_OUTPUT_TRUNCATED_SOURCE_UNAVAILABLE" in text:
            return "命令输出不完整且未能取得完整结果，未使用截断数据进行判断。"
        if text.startswith("依赖变量缺失:"):
            return "缺少前置步骤产出的变量，本项未执行。"
        return text or "执行过程中发生错误，未取得可用于判断的结果。"

    @staticmethod
    def _missing_variable_names(error: str | None) -> list[str]:
        text = str(error or "")
        if not text.startswith("依赖变量缺失:"):
            return []
        return [name.strip() for name in text.split(":", 1)[1].split(",") if name.strip()]

    @staticmethod
    def _fallback_report(
        matched_kbds: list[KBD],
        steps_executed: list[StepResult],
        evaluated_kbds: list[KBD] | None = None,
    ) -> str:
        """LLM 不可用时的降级文本报告（与 _generate_report 同源）。"""
        return KBDDiagnostic._build_diagnostic_report(
            matched_kbds,
            steps_executed,
            evaluated_kbds=evaluated_kbds,
        )
