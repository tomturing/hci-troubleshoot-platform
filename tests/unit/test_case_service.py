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
    try:
        from shared.database.postgres import Base

        Base.metadata.clear()
    except ImportError:
        pass
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

from datetime import datetime
from unittest.mock import AsyncMock, Mock

import pytest
from app.models.case import Case, CaseStatus
from app.services.case_service import CaseService
from shared.models.schemas import CaseCreate


@pytest.mark.asyncio
class TestCaseIDGeneration:
    """工单ID生成契约测试

    case_id 由 DB 函数 generate_case_id() 生成（Q + 当天日期 + 当天自增序号），
    service 仅委托 repository.generate_case_id()，不再本地随机生成，消除孤儿工单 ID。
    """

    async def _build_service(self, expected_id: str):
        """构造 service 与 mock repository：generate_case_id 返回期望值，其余依赖 mock 掉"""
        mock_repo = Mock()
        mock_repo.generate_case_id = AsyncMock(return_value=expected_id)
        mock_user = Mock()
        mock_user.user_id = "u1"
        mock_repo.get_user_by_client_id = AsyncMock(return_value=mock_user)
        mock_case = Case(
            case_id=expected_id,
            user_id="u1",
            client_id="c1",
            title="t",
            description="d",
            status=CaseStatus.created,
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        mock_repo.generate_case_id = AsyncMock(return_value="Q20260215001")
        mock_repo.create = AsyncMock(return_value=mock_case)
        return CaseService(mock_repo), mock_repo

    async def test_case_id_format(self):
        """create_case 返回的 case_id 必须满足格式契约：Q + 8位日期 + 5位序号（共14字符）"""
        today = datetime.utcnow().strftime("%Y%m%d")
        expected_id = f"Q{today}00001"
        service, mock_repo = await self._build_service(expected_id)

        result = await service.create_case(CaseCreate(title="t", description="d", client_id="c1"))

        # service 必须委托 repository 生成 case_id（而非本地随机），这是修复孤儿 ID 的核心契约
        mock_repo.generate_case_id.assert_awaited_once()
        assert result.case_id == expected_id
        # 格式契约
        assert result.case_id.startswith("Q")
        assert len(result.case_id) == 14
        assert result.case_id[1:9].isdigit()
        assert result.case_id[9:].isdigit()

    async def test_case_id_date_is_today(self):
        """create_case 返回的 case_id 日期部分必须为当前日期（与 DB 自增契约一致）"""
        today = datetime.utcnow().strftime("%Y%m%d")
        expected_id = f"Q{today}00007"
        service, mock_repo = await self._build_service(expected_id)

        result = await service.create_case(CaseCreate(title="t", description="d", client_id="c1"))

        assert result.case_id[1:9] == today
        mock_repo.generate_case_id.assert_awaited_once()


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
            updated_at=datetime.utcnow(),
        )
        mock_repo.generate_case_id = AsyncMock(return_value="Q20260215001")
        mock_repo.create = AsyncMock(return_value=mock_case)

        # Create service
        service = CaseService(mock_repo)

        # Create case
        case_create = CaseCreate(client_id="test-client", title="Test Case", description="Test Description")

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
            updated_at=datetime.utcnow(),
        )
        mock_repo.generate_case_id = AsyncMock(return_value="Q20260215001")
        mock_repo.create = AsyncMock(return_value=mock_case)

        service = CaseService(mock_repo)
        case_create = CaseCreate(client_id="test-client", title="Test")

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
            updated_at=datetime.utcnow(),
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
            Case(
                case_id="Q20260215001",
                user_id="test-user-id",
                client_id="test-client",
                title="Case 1",
                status=CaseStatus.created,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ),
            Case(
                case_id="Q20260215002",
                user_id="test-user-id",
                client_id="test-client",
                title="Case 2",
                status=CaseStatus.created,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            ),
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
            updated_at=datetime.utcnow(),
        )
        mock_repo.update_status = AsyncMock(return_value=mock_case)

        service = CaseService(mock_repo)
        result = await service.confirm_case("Q20260215001")

        assert result.status == CaseStatus.confirmed
        mock_repo.update_status.assert_called_once_with("Q20260215001", CaseStatus.confirmed)

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
            updated_at=datetime.utcnow(),
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


@pytest.mark.asyncio
class TestCaseAdminOperations:
    """工单管理员操作测试"""

    async def test_list_all_cases_with_search(self):
        """测试获取所有工单列表包含高级检索参数"""
        mock_repo = Mock()
        mock_cases = [
            Case(
                case_id="Q20260215001",
                user_id="test-user-id",
                client_id="test-client",
                title="Case 1",
                status=CaseStatus.created,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
        ]
        mock_repo.get_all = AsyncMock(return_value=(mock_cases, 1))

        service = CaseService(mock_repo)
        res = await service.list_all_cases(
            skip=0,
            limit=10,
            status="created",
            client_id="test-",
            case_id="Q2026",
            title="Case",
        )

        assert res.total == 1
        assert len(res.items) == 1
        assert res.items[0].case_id == "Q20260215001"
        mock_repo.get_all.assert_called_once_with(
            skip=0,
            limit=10,
            status="created",
            client_id="test-",
            case_id="Q2026",
            title="Case",
            start_time=None,
            end_time=None,
        )

    async def test_update_case_success(self):
        """测试成功更新工单字段"""
        mock_repo = Mock()
        mock_case = Case(
            case_id="Q20260215001",
            user_id="test-user-id",
            client_id="test-client",
            title="Updated Title",
            description="Updated Desc",
            status=CaseStatus.confirmed,
            priority="high",
            category="vm",
            assistant_type="ops-agent",
            created_at=datetime.utcnow(),
            updated_at=datetime.utcnow(),
        )
        mock_repo.update = AsyncMock(return_value=mock_case)

        service = CaseService(mock_repo)
        from shared.models.schemas import CaseUpdate

        update_data = CaseUpdate(
            title="Updated Title",
            description="Updated Desc",
            status=CaseStatus.confirmed,
            priority="high",
            category="vm",
            assistant_type="ops-agent",
        )
        res = await service.update_case("Q20260215001", update_data)

        assert res is not None
        assert res.title == "Updated Title"
        assert res.priority == "high"
        assert res.category == "vm"
        mock_repo.update.assert_called_once_with(
            "Q20260215001",
            {
                "title": "Updated Title",
                "description": "Updated Desc",
                "status": CaseStatus.confirmed,
                "priority": "high",
                "category": "vm",
                "assistant_type": "ops-agent",
            },
        )

    async def test_update_case_not_found(self):
        """测试更新不存在的工单"""
        mock_repo = Mock()
        mock_repo.update = AsyncMock(return_value=None)

        service = CaseService(mock_repo)
        from shared.models.schemas import CaseUpdate

        res = await service.update_case("Q99999999999", CaseUpdate(title="Non-existent"))

        assert res is None
