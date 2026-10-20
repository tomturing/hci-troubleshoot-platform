"""确定性候选诊断使用的封闭数据模型。"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from shared.cdd.kbd_model import KBD


class SignalOutcome(StrEnum):
    """现场原子信号的认识论状态。

    PASS/FAIL 仅作为源码兼容别名；序列化统一产出 SATISFIED/CONTRADICTED。
    NOT_RUN/BLOCKED 是调度内部态，不得作为最终 SignalResult 对外发布。
    """

    NOT_RUN = "NOT_RUN"
    SATISFIED = "SATISFIED"
    PASS = "SATISFIED"
    CONTRADICTED = "CONTRADICTED"
    FAIL = "CONTRADICTED"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"
    NOT_APPLICABLE = "NOT_APPLICABLE"

    @classmethod
    def _missing_(cls, value: object):
        # 允许 replay 历史执行记录，但新记录必须写 canonical value。
        return {"PASS": cls.SATISFIED, "FAIL": cls.CONTRADICTED}.get(value)


class EvidenceRole(StrEnum):
    MUST = "must"
    SHOULD = "should"
    EXCLUDE = "exclude"
    CONTEXT = "context"


class CandidateState(StrEnum):
    CANDIDATE = "CANDIDATE"
    SUPPORTED = "SUPPORTED"
    REJECTED = "REJECTED"
    INCONCLUSIVE = "INCONCLUSIVE"
    NOT_EXECUTABLE = "NOT_EXECUTABLE"


class ConclusionLevel(StrEnum):
    DEFINITIVE = "DEFINITIVE"
    PARTIAL = "PARTIAL"
    INCONCLUSIVE = "INCONCLUSIVE"
    NO_MATCH = "NO_MATCH"


@dataclass(frozen=True)
class SignalRef:
    ref_id: str
    kbd_id: str
    support_id: str
    revision: str
    signal_id: str
    signal: dict[str, Any]
    required_for_support: bool
    evidence_role: EvidenceRole
    failure_effect: str
    requires: tuple[str, ...]
    produces: tuple[str, ...]
    phase: str
    matcher_fingerprint: str


@dataclass
class Acquisition:
    template_key: str
    tool_name: str
    args_template: dict[str, Any]
    signal_refs: list[SignalRef] = field(default_factory=list)
    requires: set[str] = field(default_factory=set)
    produces: set[str] = field(default_factory=set)
    cost: float = 1.0
    latency: float = 1.0
    risk: float = 1.0


@dataclass
class SignalPlan:
    plan_id: str
    category_id: str
    snapshot_id: str
    candidates: dict[str, KBD]
    signals: dict[str, SignalRef]
    acquisitions: dict[str, Acquisition]
    verification_policies: dict[str, CaseVerificationPolicy] = field(default_factory=dict)
    compile_errors: dict[str, list[str]] = field(default_factory=dict)


@dataclass(frozen=True)
class CaseVerificationPolicy:
    """单案例最小证据策略；KBD 缺省时由 legacy required 信号安全推导。"""

    must: tuple[str, ...] = ()
    should: tuple[str, ...] = ()
    exclude: tuple[str, ...] = ()
    context: tuple[str, ...] = ()
    minimum_should: int = 0
    scope: dict[str, Any] = field(default_factory=dict)
    # None 表示 legacy KBD 尚无 Contract，继续兼容其运行时环境变量；一旦存在
    # Contract，就必须显式声明所有不是由 Signal 产出的外部变量。
    external_variables: tuple[str, ...] | None = None


class CaseVerdict(StrEnum):
    CONFIRMED = "confirmed"
    REJECTED = "rejected"
    INCONCLUSIVE = "inconclusive"


@dataclass(frozen=True)
class SignalEvaluation:
    evaluation_id: str
    signal_ref_id: str
    exec_id: str
    outcome: SignalOutcome


@dataclass
class CandidateAssessment:
    kbd_id: str
    state: CandidateState = CandidateState.CANDIDATE
    scope_state: str = "APPLICABLE"
    signal_outcomes: dict[str, SignalOutcome] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)

    @property
    def verdict(self) -> CaseVerdict:
        if self.state is CandidateState.SUPPORTED:
            return CaseVerdict.CONFIRMED
        if self.state is CandidateState.REJECTED:
            return CaseVerdict.REJECTED
        return CaseVerdict.INCONCLUSIVE


@dataclass(frozen=True)
class ConclusionDecision:
    level: ConclusionLevel
    supported_ids: tuple[str, ...]
    rejected_ids: tuple[str, ...]
    inconclusive_ids: tuple[str, ...]
    not_executable_ids: tuple[str, ...]
    reason: str
