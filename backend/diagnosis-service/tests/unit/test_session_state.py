"""诊断会话状态机测试。"""

import pytest
from app.domain.session_state import (
    DiagnosisSessionStatus,
    InvalidSessionTransitionError,
    SessionStateMachine,
)


def test_happy_path_transitions_are_allowed():
    """主流程状态可以按顺序推进。"""

    path = [
        DiagnosisSessionStatus.CREATED,
        DiagnosisSessionStatus.PLAN_READY,
        DiagnosisSessionStatus.COLLECTING,
        DiagnosisSessionStatus.UPLOADING,
        DiagnosisSessionStatus.ASSESSING,
        DiagnosisSessionStatus.DIAGNOSING,
        DiagnosisSessionStatus.REVIEW_PENDING,
        DiagnosisSessionStatus.PUBLISHED,
        DiagnosisSessionStatus.CLOSED,
        DiagnosisSessionStatus.DELETION_PENDING,
        DiagnosisSessionStatus.DELETED,
    ]

    for source, target in zip(path, path[1:], strict=False):
        SessionStateMachine.validate(source, target)


def test_supplement_returns_to_collection_once():
    """补充采集需要重新进入采集阶段。"""

    SessionStateMachine.validate(
        DiagnosisSessionStatus.ASSESSING,
        DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
    )
    SessionStateMachine.validate(
        DiagnosisSessionStatus.SUPPLEMENT_REQUIRED,
        DiagnosisSessionStatus.COLLECTING,
    )


def test_invalid_skip_is_rejected():
    """禁止从创建状态直接跳到诊断状态。"""

    with pytest.raises(InvalidSessionTransitionError):
        SessionStateMachine.validate(
            DiagnosisSessionStatus.CREATED,
            DiagnosisSessionStatus.DIAGNOSING,
        )


def test_same_state_is_idempotent():
    """重复写入相同状态视为幂等。"""

    SessionStateMachine.validate(
        DiagnosisSessionStatus.ASSESSING,
        DiagnosisSessionStatus.ASSESSING,
    )


def test_failed_session_only_resumes_previous_status():
    """失败重试只能恢复到失败前状态。"""

    SessionStateMachine.validate(
        DiagnosisSessionStatus.FAILED,
        DiagnosisSessionStatus.UPLOADING,
        resume_status=DiagnosisSessionStatus.UPLOADING,
    )

    with pytest.raises(InvalidSessionTransitionError):
        SessionStateMachine.validate(
            DiagnosisSessionStatus.FAILED,
            DiagnosisSessionStatus.DIAGNOSING,
            resume_status=DiagnosisSessionStatus.UPLOADING,
        )


def test_deletion_failure_can_resume_deletion():
    """删除失败后允许恢复删除流程。"""

    SessionStateMachine.validate(
        DiagnosisSessionStatus.FAILED,
        DiagnosisSessionStatus.DELETION_PENDING,
        resume_status=DiagnosisSessionStatus.DELETION_PENDING,
    )
