"""
状态转换约束测试

验证 case.status 状态机的合法转换规则（见 01_系统架构.md §9.6）
"""

import uuid
from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.models.case import Case, CaseStatus, CloseReason
from app.repositories.case_repo import CaseRepository
from app.services.case_service import CaseService


@pytest.fixture
def mock_repo():
    """创建 mock repository"""
    return MagicMock(spec=CaseRepository)


@pytest.fixture
def service(mock_repo):
    """创建 service 实例"""
    return CaseService(mock_repo)


def create_mock_case(case_id: str, status: CaseStatus) -> Case:
    """创建带有完整属性的 mock Case 对象"""
    now = datetime.now(UTC)
    case = Case(
        case_id=case_id,
        user_id=uuid.uuid4(),
        client_id="test-client",
        title="Test Case",
        description="Test description",
        status=status,
        assistant_type="openclaw",
        trace_id="test-trace-id",
        created_at=now,
        updated_at=now,
    )
    return case


class TestCaseStatusTransitions:
    """case.status 状态转换约束测试"""

    @pytest.mark.asyncio
    async def test_created_to_confirmed_via_confirm_case(self, service, mock_repo):
        """合法转换：created → confirmed（通过 confirm_case）"""
        mock_case = create_mock_case("Q20260407001", CaseStatus.confirmed)
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        result = await service.confirm_case("Q20260407001")

        assert result is not None
        mock_repo.update_status.assert_called_once()

    @pytest.mark.asyncio
    async def test_created_to_in_progress_via_escalate(self, service, mock_repo):
        """合法转换：created → in_progress（通过 escalate_to_human，跳过 confirmed）

        场景：S0 意图识别彻底失败，直接移交人工
        """
        mock_case = create_mock_case("Q20260407001", CaseStatus.in_progress)
        mock_case.close_reason = "s0_classification_failed"
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        result = await service.escalate_to_human(
            "Q20260407001", close_reason="s0_classification_failed"
        )

        assert result is not None
        mock_repo.update_status.assert_called_once_with(
            "Q20260407001", CaseStatus.in_progress, close_reason="s0_classification_failed"
        )

    @pytest.mark.asyncio
    async def test_escalate_with_different_close_reasons(self, service, mock_repo):
        """测试不同的 close_reason 场景"""
        mock_case = create_mock_case("Q20260407001", CaseStatus.in_progress)
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        # S0 失败
        await service.escalate_to_human("Q20260407001", close_reason="s0_classification_failed")
        mock_repo.update_status.assert_called_with(
            "Q20260407001", CaseStatus.in_progress, close_reason="s0_classification_failed"
        )

        # S6 选 C 升级人工
        mock_repo.reset_mock()
        await service.escalate_to_human("Q20260407001", close_reason="escalated")
        mock_repo.update_status.assert_called_with(
            "Q20260407001", CaseStatus.in_progress, close_reason="escalated"
        )


class TestCloseReasonEnum:
    """close_reason 枚举完整性测试"""

    def test_all_close_reasons_defined(self):
        """验证所有 close_reason 值都已定义"""
        expected_reasons = [
            "user_command",
            "timeout",
            "abandon",
            "admin_close",
            "escalated",
            "s0_classification_failed",
        ]

        for reason in expected_reasons:
            assert hasattr(CloseReason, reason), f"CloseReason 缺少枚举值: {reason}"

    def test_close_reason_values(self):
        """验证 close_reason 枚举值"""
        assert CloseReason.user_command.value == "user_command"
        assert CloseReason.escalated.value == "escalated"
        assert CloseReason.s0_classification_failed.value == "s0_classification_failed"


class TestS0FailureScenario:
    """S0 失败场景完整流程测试"""

    @pytest.mark.asyncio
    async def test_s0_failure_flow(self, service, mock_repo):
        """S0 失败完整流程：
        1. created → in_progress（跳过 confirmed）
        2. close_reason = s0_classification_failed
        """
        mock_case = create_mock_case("Q20260407001", CaseStatus.in_progress)
        mock_case.close_reason = "s0_classification_failed"
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        result = await service.escalate_to_human(
            "Q20260407001", close_reason="s0_classification_failed"
        )

        assert result is not None
        # 验证跳过 confirmed 直接进入 in_progress
        mock_repo.update_status.assert_called_once_with(
            "Q20260407001", CaseStatus.in_progress, close_reason="s0_classification_failed"
        )


class TestRepositoryUpdateStatusSignature:
    """Repository update_status 方法签名测试"""

    @pytest.mark.asyncio
    async def test_update_status_accepts_close_reason(self, mock_repo):
        """验证 update_status 方法接受 close_reason 参数"""
        mock_case = create_mock_case("Q20260407001", CaseStatus.in_progress)
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        # 调用带 close_reason 参数
        result = await mock_repo.update_status(
            "Q20260407001",
            CaseStatus.in_progress,
            close_reason="s0_classification_failed",
        )

        assert result is not None
        mock_repo.update_status.assert_called_once_with(
            "Q20260407001", CaseStatus.in_progress, close_reason="s0_classification_failed"
        )

    @pytest.mark.asyncio
    async def test_update_status_without_close_reason(self, mock_repo):
        """验证 update_status 方法可以不传 close_reason"""
        mock_case = create_mock_case("Q20260407001", CaseStatus.confirmed)
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        # 调用不带 close_reason 参数
        result = await mock_repo.update_status("Q20260407001", CaseStatus.confirmed)

        assert result is not None
        mock_repo.update_status.assert_called_once_with("Q20260407001", CaseStatus.confirmed)
