"""
Scheduler Service 单元测试

匹配 v2.0 API 签名:
- __init__(k8s_client, redis_manager)
- allocate_pod(case_id, assistant_type) -> Optional[str]
- release_pod(case_id) -> bool
- get_allocation_info(case_id) -> Optional[Dict]
- Redis Hash 持久化分配状态
"""

import os
import sys

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

import os
import sys

# 多服务共享 app/ 命名空间，仅在 app 指向错误服务时清除重载
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.services.scheduler_service import REDIS_ALLOCATIONS_KEY, SchedulerService


@pytest.fixture
def mock_k8s():
    k8s = MagicMock()
    k8s.get_pod_status.return_value = "Running"
    k8s.get_pod_ip.return_value = "10.0.0.1"
    k8s.create_pod.return_value = True
    k8s.delete_pod.return_value = True
    k8s.list_pods.return_value = []
    return k8s


@pytest.fixture
def mock_redis():
    redis = MagicMock()
    redis.hget = AsyncMock(return_value=None)
    redis.hset = AsyncMock()
    redis.hdel = AsyncMock()
    redis.hgetall = AsyncMock(return_value={})
    redis.connect = AsyncMock()
    redis.close = AsyncMock()
    return redis


@pytest.fixture
def service(mock_k8s, mock_redis):
    """创建 SchedulerService 实例（使用 mock K8s + mock Redis）"""
    svc = SchedulerService(mock_k8s, mock_redis)
    return svc


@pytest.mark.asyncio
class TestSchedulerAllocatePod:
    """Pod 分配测试"""

    async def test_allocate_pod_new(self, service, mock_k8s, mock_redis):
        """测试首次分配新 Pod"""
        # Redis 中无已有分配
        mock_redis.hget.return_value = None
        # Mock 池中有可用 Pod
        pool = MagicMock()
        pool.acquire_pod = AsyncMock(return_value="openclaw-pool-abc12345")
        service.pool_manager.get_pool = MagicMock(return_value=pool)

        result = await service.allocate_pod("case-001", "openclaw")

        assert result == "openclaw-pool-abc12345"
        # 验证写入 Redis
        mock_redis.hset.assert_called_once()
        call_args = mock_redis.hset.call_args
        assert call_args[0][0] == REDIS_ALLOCATIONS_KEY
        assert call_args[0][1] == "case-001"
        stored = json.loads(call_args[0][2])
        assert stored["pod_name"] == "openclaw-pool-abc12345"
        assert stored["assistant_type"] == "openclaw"

    async def test_allocate_pod_reuse_existing(self, service, mock_k8s, mock_redis):
        """测试复用已有的 Pod（同类型且存活）"""
        mock_redis.hget.return_value = json.dumps({"pod_name": "openclaw-pool-existing", "assistant_type": "openclaw"})
        mock_k8s.get_pod_status.return_value = "Running"

        result = await service.allocate_pod("case-001", "openclaw")

        assert result == "openclaw-pool-existing"
        # 不应重新写入 Redis
        mock_redis.hset.assert_not_called()

    async def test_allocate_pod_type_mismatch(self, service, mock_k8s, mock_redis):
        """测试助手类型不匹配时清理旧分配"""
        mock_redis.hget.return_value = json.dumps({"pod_name": "openclaw-pool-old", "assistant_type": "openclaw"})
        pool = MagicMock()
        pool.acquire_pod = AsyncMock(return_value="nabobot-pool-new123")
        service.pool_manager.get_pool = MagicMock(return_value=pool)

        result = await service.allocate_pod("case-001", "nabobot")

        assert result == "nabobot-pool-new123"
        # 验证清理了旧分配
        mock_redis.hdel.assert_called_once_with(REDIS_ALLOCATIONS_KEY, "case-001")

    async def test_allocate_pod_no_pool(self, service, mock_redis):
        """测试不存在的助手类型"""
        mock_redis.hget.return_value = None
        service.pool_manager.get_pool = MagicMock(return_value=None)

        result = await service.allocate_pod("case-001", "unknown_type")

        assert result is None


@pytest.mark.asyncio
class TestSchedulerReleasePod:
    """Pod 释放测试"""

    async def test_release_pod_success(self, service, mock_redis):
        """测试成功释放 Pod"""
        mock_redis.hget.return_value = json.dumps({"pod_name": "openclaw-pool-abc", "assistant_type": "openclaw"})
        pool = MagicMock()
        pool.release_pod = AsyncMock()
        service.pool_manager.get_pool = MagicMock(return_value=pool)

        result = await service.release_pod("case-001")

        assert result is True
        mock_redis.hdel.assert_called_once_with(REDIS_ALLOCATIONS_KEY, "case-001")
        pool.release_pod.assert_called_once_with("openclaw-pool-abc")

    async def test_release_pod_not_found(self, service, mock_redis):
        """测试释放不存在的分配"""
        mock_redis.hget.return_value = None

        result = await service.release_pod("case-nonexistent")

        assert result is False


@pytest.mark.asyncio
class TestSchedulerStatus:
    """服务状态测试"""

    async def test_get_status(self, service, mock_redis):
        """测试获取服务状态"""
        mock_redis.hgetall.return_value = {
            "case-001": json.dumps({"pod_name": "pod1", "assistant_type": "openclaw"}),
            "case-002": json.dumps({"pod_name": "pod2", "assistant_type": "nabobot"}),
        }

        status = await service.get_status()

        assert status["allocated_cases"] == 2
        assert "pools" in status

    async def test_get_allocation_info(self, service, mock_redis):
        """测试查询分配详情"""
        mock_redis.hget.return_value = json.dumps({"pod_name": "openclaw-pool-xyz", "assistant_type": "openclaw"})

        info = await service.get_allocation_info("case-001")

        assert info is not None
        assert info["pod_name"] == "openclaw-pool-xyz"
        assert info["assistant_type"] == "openclaw"

    async def test_get_allocation_info_none(self, service, mock_redis):
        """测试查询不存在的分配"""
        mock_redis.hget.return_value = None

        info = await service.get_allocation_info("case-nonexistent")

        assert info is None
