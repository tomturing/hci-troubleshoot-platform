"""诊断会话状态机。"""

from enum import StrEnum


class DiagnosisSessionStatus(StrEnum):
    """诊断会话状态。"""

    CREATED = "created"
    PLAN_READY = "plan_ready"
    COLLECTING = "collecting"
    UPLOADING = "uploading"
    ASSESSING = "assessing"
    DIAGNOSING = "diagnosing"
    SUPPLEMENT_REQUIRED = "supplement_required"
    REVIEW_PENDING = "review_pending"
    PUBLISHED = "published"
    CLOSED = "closed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DELETION_PENDING = "deletion_pending"
    DELETED = "deleted"


class InvalidSessionTransitionError(ValueError):
    """诊断会话状态转换不合法。"""


class SessionStateMachine:
    """集中维护诊断会话的合法状态转换。"""

    _RESUMABLE_STATUSES = frozenset(
        {
            DiagnosisSessionStatus.CREATED,
            DiagnosisSessionStatus.PLAN_READY,
            DiagnosisSessionStatus.COLLECTING,
            DiagnosisSessionStatus.UPLOADING,
            DiagnosisSessionStatus.ASSESSING,
            DiagnosisSessionStatus.DIAGNOSING,
            DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
            DiagnosisSessionStatus.REVIEW_PENDING,
            DiagnosisSessionStatus.DELETION_PENDING,
        }
    )

    _TRANSITIONS: dict[DiagnosisSessionStatus, frozenset[DiagnosisSessionStatus]] = {
        DiagnosisSessionStatus.CREATED: frozenset(
            {
                DiagnosisSessionStatus.PLAN_READY,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.PLAN_READY: frozenset(
            {
                DiagnosisSessionStatus.COLLECTING,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.COLLECTING: frozenset(
            {
                DiagnosisSessionStatus.UPLOADING,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.UPLOADING: frozenset(
            {
                DiagnosisSessionStatus.ASSESSING,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.ASSESSING: frozenset(
            {
                DiagnosisSessionStatus.DIAGNOSING,
                DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.DIAGNOSING: frozenset(
            {
                DiagnosisSessionStatus.REVIEW_PENDING,
                DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.SUPPLEMENT_REQUIRED: frozenset(
            {
                DiagnosisSessionStatus.COLLECTING,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.REVIEW_PENDING: frozenset(
            {
                DiagnosisSessionStatus.PUBLISHED,
                DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
                DiagnosisSessionStatus.FAILED,
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.PUBLISHED: frozenset(
            {
                DiagnosisSessionStatus.CLOSED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.CLOSED: frozenset({DiagnosisSessionStatus.DELETION_PENDING}),
        DiagnosisSessionStatus.FAILED: frozenset(
            {
                DiagnosisSessionStatus.CANCELLED,
                DiagnosisSessionStatus.DELETION_PENDING,
            }
        ),
        DiagnosisSessionStatus.CANCELLED: frozenset({DiagnosisSessionStatus.DELETION_PENDING}),
        DiagnosisSessionStatus.DELETION_PENDING: frozenset(
            {
                DiagnosisSessionStatus.DELETED,
                DiagnosisSessionStatus.FAILED,
            }
        ),
        DiagnosisSessionStatus.DELETED: frozenset(),
    }

    @classmethod
    def validate(
        cls,
        source: DiagnosisSessionStatus,
        target: DiagnosisSessionStatus,
        *,
        resume_status: DiagnosisSessionStatus | None = None,
    ) -> None:
        """校验一次状态转换。

        `failed` 的恢复目标必须等于进入失败状态前保存的 `resume_status`，
        避免调用方通过重试绕过状态机。
        """

        if source == target:
            return

        if source == DiagnosisSessionStatus.FAILED and resume_status is not None:
            if resume_status not in cls._RESUMABLE_STATUSES:
                raise InvalidSessionTransitionError(f"失败恢复目标不是活动状态: {resume_status}")
            if target == resume_status:
                return

        if target not in cls._TRANSITIONS[source]:
            raise InvalidSessionTransitionError(f"不允许从 {source} 转换到 {target}")

    @classmethod
    def can_transition(
        cls,
        source: DiagnosisSessionStatus,
        target: DiagnosisSessionStatus,
        *,
        resume_status: DiagnosisSessionStatus | None = None,
    ) -> bool:
        """返回状态转换是否合法。"""

        try:
            cls.validate(source, target, resume_status=resume_status)
        except InvalidSessionTransitionError:
            return False
        return True
