"""
Environment Service 单测

覆盖 create_environment、get_environments_by_case、build_context_info 的正常和异常路径。
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest
from app.services.environment_service import EnvironmentService
from shared.models.schemas import EnvironmentContextResponse, EnvironmentCreate, EnvType


@pytest.fixture
def mock_repository():
    """创建 mock repository"""
    repo = MagicMock()
    repo.create = AsyncMock()
    repo.get_by_case_id = AsyncMock()
    repo.get_by_case_and_type = AsyncMock()
    repo.upsert_by_case_and_type = AsyncMock()
    repo.session = MagicMock()
    repo.session.flush = AsyncMock()
    repo.session.refresh = AsyncMock()
    return repo


@pytest.fixture
def mock_environment():
    """创建 mock Environment ORM 对象"""
    env = MagicMock()
    env.environment_id = uuid4()
    env.case_id = "Q2026042000001"
    env.env_type = "cluster"
    env.env_data = {"hci_version": "6.8.1", "cluster_name": "prod-hci"}
    env.collected_at = datetime.now(UTC)
    env.created_at = datetime.now(UTC)
    env.updated_at = datetime.now(UTC)
    env.trace_id = "test-trace-123"
    return env


@pytest.fixture
def service(mock_repository):
    """创建 EnvironmentService 实例"""
    return EnvironmentService(mock_repository)


class TestCreateEnvironment:
    """create_environment 测试组"""

    async def test_create_success_with_collected_at(self, service, mock_repository, mock_environment):
        """测试正常创建（带 collected_at）"""
        mock_repository.create.return_value = mock_environment

        env_create = EnvironmentCreate(
            case_id="Q2026042000001",
            env_type=EnvType.CLUSTER,
            env_data={"hci_version": "6.8.1"},
            collected_at=datetime.now(UTC),
        )

        result = await service.create_environment(env_create)

        assert result.case_id == "Q2026042000001"
        assert result.env_type == EnvType.CLUSTER
        mock_repository.create.assert_called_once()

    async def test_create_success_without_collected_at(self, service, mock_repository, mock_environment):
        """测试正常创建（无 collected_at，让 DB 默认值生效）"""
        mock_environment.collected_at = None  # DB 默认值生效后的状态
        mock_repository.create.return_value = mock_environment

        env_create = EnvironmentCreate(
            case_id="Q2026042000001",
            env_type=EnvType.CLUSTER,
            env_data={"hci_version": "6.8.1"},
            collected_at=None,
        )

        result = await service.create_environment(env_create)

        # 验证创建时未传入 collected_at
        call_args = mock_repository.create.call_args[0][0]
        assert call_args.collected_at is None
        assert result.case_id == "Q2026042000001"


class TestGetEnvironmentsByCase:
    """get_environments_by_case 测试组"""

    async def test_get_success(self, service, mock_repository, mock_environment):
        """测试正常获取"""
        mock_repository.get_by_case_id.return_value = [mock_environment]

        result = await service.get_environments_by_case("Q2026042000001")

        assert result.total == 1
        assert len(result.items) == 1
        assert result.items[0].case_id == "Q2026042000001"

    async def test_get_empty_list(self, service, mock_repository):
        """测试获取空列表"""
        mock_repository.get_by_case_id.return_value = []

        result = await service.get_environments_by_case("Q2026042000001")

        assert result.total == 0
        assert len(result.items) == 0


class TestBuildContextInfo:
    """build_context_info 测试组"""

    async def test_build_success_all_types(self, service, mock_repository):
        """测试完整构建（所有类型都有数据）"""
        cluster_env = MagicMock()
        # env_data 字段名与 acli platform info get 输出一致（key=value ini 格式）
        cluster_env.env_data = {"hci_version": "6.8.1", "name": "prod-hci", "host_count": "3"}

        alert_env = MagicMock()
        # alerts 字段名与 acli alert list 实际输出一致：urgent_type/start/description/target
        alert_env.env_data = {"alerts": [{"urgent_type": "紧急", "start": "09:02", "description": "磁盘异常", "target": "node-1"}]}

        task_env = MagicMock()
        # tasks 字段名与 acli task list 实际输出一致：process/start/type/description
        task_env.env_data = {"tasks": [{"process": "失败", "start": "09:01", "type": "Migration", "description": ""}]}

        mock_repository.get_by_case_and_type.side_effect = [cluster_env, alert_env, task_env]

        result = await service.build_context_info("Q2026042000001")

        assert result.env_info["hci_version"] == "6.8.1"
        assert len(result.alert_logs) == 1
        # urgent_type="紧急" 映射为 "CRITICAL"
        assert result.alert_logs[0]["level"] == "CRITICAL"
        assert len(result.task_logs) == 1
        # process 字段直接透传中文字符串
        assert result.task_logs[0]["status"] == "失败"

    async def test_build_partial_data(self, service, mock_repository):
        """测试部分缺失（只有 cluster，无 alert/task）"""
        cluster_env = MagicMock()
        cluster_env.env_data = {"hci_version": "6.8.1"}

        mock_repository.get_by_case_and_type.side_effect = [cluster_env, None, None]

        result = await service.build_context_info("Q2026042000001")

        assert result.env_info["hci_version"] == "6.8.1"
        assert len(result.alert_logs) == 0
        assert len(result.task_logs) == 0

    async def test_build_empty_data(self, service, mock_repository):
        """测试完全无数据"""
        mock_repository.get_by_case_and_type.side_effect = [None, None, None]

        result = await service.build_context_info("Q2026042000001")

        # 返回默认空值，不抛异常（超时容忍原则）
        assert result == EnvironmentContextResponse()
        assert result.env_info == {}
        assert result.alert_logs == []
        assert result.task_logs == []

    async def test_build_exception_tolerance(self, service, mock_repository):
        """测试异常容忍（Repository 抛异常时返回默认值）"""
        mock_repository.get_by_case_and_type.side_effect = Exception("DB timeout")

        result = await service.build_context_info("Q2026042000001")

        # 异常不抛出，返回默认空值
        assert result.env_info == {}
        assert result.alert_logs == []
        assert result.task_logs == []


class TestUpsertEnvironment:
    """upsert_environment 测试组"""

    async def test_upsert_create_new_record(self, service, mock_repository, mock_environment):
        """测试 upsert 创建新记录（不存在时）"""
        mock_repository.get_by_case_and_type.return_value = None  # 不存在
        mock_repository.upsert_by_case_and_type.return_value = (mock_environment, True)  # created=True

        result = await service.upsert_environment(
            case_id="Q2026042000001",
            env_type=EnvType.CLUSTER,
            env_data={"hci_version": "6.8.1"},
            collected_at=datetime.now(UTC),
        )

        assert result.case_id == "Q2026042000001"
        mock_repository.upsert_by_case_and_type.assert_called_once()

    async def test_upsert_update_existing_record(self, service, mock_repository, mock_environment):
        """测试 upsert 更新已有记录（存在时）"""
        mock_repository.upsert_by_case_and_type.return_value = (mock_environment, False)  # created=False

        result = await service.upsert_environment(
            case_id="Q2026042000001",
            env_type=EnvType.CLUSTER,
            env_data={"hci_version": "6.8.2"},  # 更新版本
            collected_at=datetime.now(UTC),
        )

        assert result.case_id == "Q2026042000001"
        mock_repository.upsert_by_case_and_type.assert_called_once()

    async def test_upsert_without_collected_at(self, service, mock_repository, mock_environment):
        """测试 upsert 不传 collected_at（repo 层默认当前时间）"""
        mock_repository.upsert_by_case_and_type.return_value = (mock_environment, False)

        result = await service.upsert_environment(
            case_id="Q2026042000001",
            env_type=EnvType.CLUSTER,
            env_data={"hci_version": "6.8.1"},
            collected_at=None,  # 不传，repo 层应使用当前时间
        )

        mock_repository.upsert_by_case_and_type.assert_called_once()
        call_args = mock_repository.upsert_by_case_and_type.call_args
        assert call_args.kwargs["collected_at"] is None
