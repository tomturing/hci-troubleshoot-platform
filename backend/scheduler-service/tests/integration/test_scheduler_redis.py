"""
Scheduler Service — Redis 集成测试 (T-2)

使用 fakeredis 替代真实 Redis，验证 SchedulerService 中所有
Redis Hash 操作的正确性，以及 allocate_pod / release_pod 的完整流程。
kubernetes 库由 tests/conftest.py 在收集前预先 mock，此处无需重复。
"""

import json
from unittest.mock import MagicMock

import fakeredis.aioredis as aioredis
import pytest

# 激活 scheduler-service app 命名空间（由父级 conftest.py 完成）
from app.services.scheduler_service import REDIS_ALLOCATIONS_KEY, SchedulerService
from shared.database.redis import RedisManager

# ─────────────────────────────────────────────────────────────────
#  Fixtures
# ─────────────────────────────────────────────────────────────────


@pytest.fixture
async def fake_redis() -> RedisManager:
    """返回注入了 FakeRedis client 的 RedisManager（无需真实 Redis）"""
    mgr = RedisManager("redis://fake")
    # 直接替换 client，绕过 connect()（FakeRedis 不要求 await from_url）
    mgr.client = aioredis.FakeRedis(decode_responses=True)
    yield mgr
    await mgr.client.aclose()


@pytest.fixture
def mock_k8s():
    """轻量 K8s 客户端 Mock — 不涉及 K8s API"""
    k8s = MagicMock()
    k8s.get_pod_status.return_value = "Running"
    k8s.get_pod_ip.return_value = "10.0.0.1"
    k8s.delete_pod.return_value = True
    return k8s


@pytest.fixture
async def service(fake_redis, mock_k8s) -> SchedulerService:
    """创建 SchedulerService，Pool 中注入了一个可用 Pod"""
    svc = SchedulerService(k8s_client=mock_k8s, redis_manager=fake_redis)

    # 向默认池(openclaw)注入一个可用 Pod（跳过 K8s 初始化）
    pool = svc.pool_manager.get_pool("htp-agent")
    if pool:
        pool.idle_pods.append("test-pod-001")  # deque.append，非 asyncio.Queue.put

    return svc


# 获取测试中使用的助手类型（与默认配置一致）
TEST_ASSISTANT_TYPE = "htp-agent"


# ─────────────────────────────────────────────────────────────────
#  Redis 底层操作测试
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
class TestRedisOperations:
    """直接测试 SchedulerService 中的 Redis Hash 辅助方法"""

    async def test_set_and_get_allocation(self, service):
        """写入分配后能正确读回"""
        await service._set_allocation("CASE-001", "pod-abc", TEST_ASSISTANT_TYPE)

        result = await service._get_allocation("CASE-001")
        assert result is not None
        pod_name, assistant_type = result
        assert pod_name == "pod-abc"
        assert assistant_type == TEST_ASSISTANT_TYPE

    async def test_get_nonexistent_allocation(self, service):
        """不存在的 case_id 应返回 None"""
        result = await service._get_allocation("NONEXISTENT-CASE")
        assert result is None

    async def test_del_allocation(self, service):
        """删除分配后应返回 None"""
        await service._set_allocation("CASE-002", "pod-xyz", "htp-agent")
        await service._del_allocation("CASE-002")

        result = await service._get_allocation("CASE-002")
        assert result is None

    async def test_get_all_allocations(self, service):
        """批量写入后 get_all_allocations 应返回全部"""
        await service._set_allocation("CASE-A", "pod-1", TEST_ASSISTANT_TYPE)
        await service._set_allocation("CASE-B", "pod-2", TEST_ASSISTANT_TYPE)

        all_allocs = await service._get_all_allocations()
        assert "CASE-A" in all_allocs
        assert "CASE-B" in all_allocs
        assert all_allocs["CASE-A"] == ("pod-1", TEST_ASSISTANT_TYPE)
        assert all_allocs["CASE-B"] == ("pod-2", TEST_ASSISTANT_TYPE)

    async def test_corrupted_data_is_cleaned(self, service, fake_redis):
        """损坏的 JSON 数据应被自动清理，get 返回 None"""
        # 直接写入非法 JSON
        await fake_redis.client.hset(REDIS_ALLOCATIONS_KEY, "CASE-BAD", "{{invalid-json}}")

        result = await service._get_allocation("CASE-BAD")
        assert result is None

        # 确认损坏记录已被删除
        raw = await fake_redis.client.hget(REDIS_ALLOCATIONS_KEY, "CASE-BAD")
        assert raw is None

    async def test_health_check_returns_true(self, fake_redis):
        """FakeRedis 应能响应 PING"""
        ok = await fake_redis.health_check()
        assert ok is True


# ─────────────────────────────────────────────────────────────────
#  allocate_pod / release_pod 流程测试
# ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.integration
class TestAllocationLifecycle:
    """测试 Pod 分配 → 查询 → 释放 的完整生命周期"""

    async def test_allocate_stores_in_redis(self, service, fake_redis):
        """allocate_pod 成功后 Redis 中应存有分配记录"""
        pod_name = await service.allocate_pod("CASE-10", assistant_type=TEST_ASSISTANT_TYPE)
        assert pod_name is not None

        # 验证 Redis 中写入了正确记录
        raw = await fake_redis.client.hget(REDIS_ALLOCATIONS_KEY, "CASE-10")
        assert raw is not None
        data = json.loads(raw)
        assert data["pod_name"] == pod_name
        assert data["assistant_type"] == TEST_ASSISTANT_TYPE

    async def test_reuse_existing_allocation(self, service):
        """同一 case_id 第二次分配应复用已有 Pod"""
        pod1 = await service.allocate_pod("CASE-20", assistant_type=TEST_ASSISTANT_TYPE)
        assert pod1 is not None

        pod2 = await service.allocate_pod("CASE-20", assistant_type=TEST_ASSISTANT_TYPE)
        assert pod2 == pod1  # 应返回同一 Pod

    async def test_get_allocation_info(self, service):
        """allocate 后 get_allocation_info 应返回正确详情"""
        pod_name = await service.allocate_pod("CASE-30", assistant_type=TEST_ASSISTANT_TYPE)
        info = await service.get_allocation_info("CASE-30")

        assert info is not None
        assert info["pod_name"] == pod_name
        assert info["assistant_type"] == TEST_ASSISTANT_TYPE

    async def test_release_removes_from_redis(self, service, fake_redis):
        """release_pod 成功后 Redis 中应删除该记录"""
        await service.allocate_pod("CASE-40", assistant_type=TEST_ASSISTANT_TYPE)

        released = await service.release_pod("CASE-40")
        assert released is True

        raw = await fake_redis.client.hget(REDIS_ALLOCATIONS_KEY, "CASE-40")
        assert raw is None

    async def test_release_nonexistent_returns_false(self, service):
        """对未分配的 case_id 调用 release_pod 应返回 False"""
        result = await service.release_pod("NONEXISTENT-CASE-99")
        assert result is False

    async def test_get_status_reflects_allocations(self, service):
        """get_status 应正确反映当前分配数量"""
        # 初始应为 0
        status_before = await service.get_status()
        initial_count = status_before.get("allocated_cases", 0)

        await service.allocate_pod("CASE-50", assistant_type=TEST_ASSISTANT_TYPE)

        status_after = await service.get_status()
        assert status_after.get("allocated_cases", 0) == initial_count + 1
