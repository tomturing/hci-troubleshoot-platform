"""Closed data model for deterministic candidate diagnosis."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.adapters.agents.htp.kbd_model import KBD


class SignalOutcome(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASS = "PASS"
    FAIL = "FAIL"
    UNKNOWN = "UNKNOWN"
    ERROR = "ERROR"
    BLOCKED = "BLOCKED"


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
    compile_errors: dict[str, list[str]] = field(default_factory=dict)


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
    signal_outcomes: dict[str, SignalOutcome] = field(default_factory=dict)
    reasons: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class ConclusionDecision:
    level: ConclusionLevel
    supported_ids: tuple[str, ...]
    rejected_ids: tuple[str, ...]
    inconclusive_ids: tuple[str, ...]
    not_executable_ids: tuple[str, ...]
    reason: str
