"""
Case Service业务逻辑单元测试
"""

import os
import sys

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "case-service"))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from app.models.case import Case, CaseStatus
from app.services.case_service import CaseService
from shared.models.schemas import CaseCreate


class TestCaseIDGeneration:
    """工单ID生成测试"""

    def test_generate_case_id_format(self):
        """测试工单ID格式: Q + YYYYMMDD + 5位序号"""
        mock_repo = Mock()
        service = CaseService(mock_repo)

        case_id = service._generate_case_id()

        # 检查格式
        assert case_id.startswith("Q")
        assert len(case_id) == 14  # Q + 8位日期 + 5位序号
        assert case_id[1:9].isdigit()  # 日期部分
        assert case_id[9:].isdigit()   # 序号部分

    def test_generate_case_id_date(self):
        """测试工单ID包含当前日期"""
        mock_repo = Mock()
        service = CaseService(mock_repo)

        case_id = service._generate_case_id()
        date_part = case_id[1:9]

        today = datetime.utcnow().strftime("%Y%m%d")
        assert date_part == today


@pytest.mark.asyncio
class TestCaseCreation:
    """工单创建测试"""

    async def test_create_case_success(self):
        """测试成功创建工单"""
        # Mock repository
        mock_repo = Mock()

        # Mock user
        mock_user = Mock()
        mock_user.user_id = "test-user-id"
        mock_repo.get_user_by_client_id = AsyncMock(return_value=mock_user)

        mock_case = Case(
            case_id="Q20260215001",
            user_id="test-user-id",
            client_id="test-client",
            title="Test Case",
            description="Test Description",
            status=CaseStatus.created,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        mock_repo.create = AsyncMock(return_value=mock_case)

        # Create service
        service = CaseService(mock_repo)

        # Create case
        case_create = CaseCreate(
            client_id="test-client",
            title="Test Case",
            description="Test Description"
        )

        result = await service.create_case(case_create)

        # Assertions
        assert result.case_id == "Q20260215001"
        assert result.client_id == "test-client"
        assert result.status == CaseStatus.created
        assert result.title == "Test Case"
        mock_repo.get_user_by_client_id.assert_called_once()
        mock_repo.create.assert_called_once()

    async def test_create_case_with_trace_id(self):
        """测试创建工单时正确设置TraceID"""
        mock_repo = Mock()

        # Mock user
        mock_user = Mock()
        mock_user.user_id = "test-user-id"
        mock_repo.get_user_by_client_id = AsyncMock(return_value=mock_user)

        mock_case = Case(
            case_id="Q20260215001",
            user_id="test-user-id",
            client_id="test-client",
            title="Test",
            trace_id="test-trace-001",
            status=CaseStatus.created,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        mock_repo.create = AsyncMock(return_value=mock_case)

        service = CaseService(mock_repo)
        case_create = CaseCreate(
            client_id="test-client",
            title="Test"
        )

        result = await service.create_case(case_create)

        assert result.trace_id == "test-trace-001"


@pytest.mark.asyncio
class TestCaseRetrieval:
    """工单查询测试"""

    async def test_get_case_exists(self):
        """测试查询存在的工单"""
        mock_repo = Mock()
        mock_case = Case(
            case_id="Q20260215001",
            user_id="test-user-id",
            client_id="test-client",
            title="Test Case",
            status=CaseStatus.created,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        mock_repo.get_by_id = AsyncMock(return_value=mock_case)

        service = CaseService(mock_repo)
        result = await service.get_case("Q20260215001")

        assert result is not None
        assert result.case_id == "Q20260215001"
        mock_repo.get_by_id.assert_called_once_with("Q20260215001")

    async def test_get_case_not_exists(self):
        """测试查询不存在的工单"""
        mock_repo = Mock()
        mock_repo.get_by_id = AsyncMock(return_value=None)

        service = CaseService(mock_repo)
        result = await service.get_case("Q99999999999")

        assert result is None

    async def test_list_cases_by_client(self):
        """测试查询客户端的所有工单"""
        mock_repo = Mock()
        mock_cases = [
            Case(case_id="Q20260215001", user_id="test-user-id", client_id="test-client", title="Case 1", status=CaseStatus.created, created_at=datetime.utcnow(), updated_at=datetime.utcnow()),
            Case(case_id="Q20260215002", user_id="test-user-id", client_id="test-client", title="Case 2", status=CaseStatus.created, created_at=datetime.utcnow(), updated_at=datetime.utcnow()),
        ]
        mock_repo.get_by_client_id = AsyncMock(return_value=mock_cases)

        service = CaseService(mock_repo)
        results = await service.list_cases("test-client")

        assert len(results) == 2
        assert all(r.client_id == "test-client" for r in results)


@pytest.mark.asyncio
class TestCaseStatusTransitions:
    """工单状态转换测试"""

    async def test_confirm_case_success(self):
        """测试确认工单"""
        mock_repo = Mock()
        mock_case = Case(
            case_id="Q20260215001",
            user_id="test-user-id",
            client_id="test-client",
            title="Test",
            status=CaseStatus.confirmed,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        service = CaseService(mock_repo)
        result = await service.confirm_case("Q20260215001")

        assert result.status == CaseStatus.confirmed
        mock_repo.update_status.assert_called_once_with(
            "Q20260215001",
            CaseStatus.confirmed
        )

    async def test_close_case_success(self):
        """测试关闭工单"""
        mock_repo = Mock()
        mock_case = Case(
            case_id="Q20260215001",
            user_id="test-user-id",
            client_id="test-client",
            title="Test",
            status=CaseStatus.closed,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow()
        )
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        service = CaseService(mock_repo)
        result = await service.close_case("Q20260215001")

        assert result.status == CaseStatus.closed
        mock_repo.update_status.assert_called_once()

    async def test_confirm_case_not_found(self):
        """测试确认不存在的工单"""
        mock_repo = Mock()
        mock_repo.update_status = AsyncMock(return_value=None)

        service = CaseService(mock_repo)
        result = await service.confirm_case("Q99999999999")

        assert result is None
