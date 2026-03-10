"""
E2E Pod 调度全流程测试

测试完整的工单生命周期：
  创建工单 → 分配 Pod → 创建对话 → 发送消息 → 关闭工单 → 释放 Pod

本测试需要：
  1. 运行中的 HCI 平台（docker-compose 或 K3s）
  2. API_GATEWAY_URL 环境变量指向 api-gateway（默认 http://localhost:8000）
  3. pytest -m e2e

完成标准：
  - test_full_lifecycle 通过：工单从 open → closed，Pod 分配后释放
  - test_pod_released_after_case_close 验证 Scheduler 不再持有该 case_id 的 Pod

注意：此测试依赖外部服务，在 CI 中默认 skip，仅在有实际集群时执行。
"""

import asyncio
import os

import httpx
import pytest

# 目标地址，CI 中不设置此变量则整套测试 skip
API_GATEWAY_URL = os.environ.get("API_GATEWAY_URL", "")
INTERNAL_TOKEN = os.environ.get("INTERNAL_API_TOKEN", "hci-dev-internal-token")
SCHEDULER_URL = os.environ.get("SCHEDULER_URL", "http://localhost:8003")

pytestmark = pytest.mark.skipif(
    not API_GATEWAY_URL,
    reason="API_GATEWAY_URL 未设置，跳过 E2E 测试（仅在实际集群中执行）",
)


@pytest.fixture(scope="module")
def anyio_backend():
    return "asyncio"


@pytest.fixture(scope="module")
async def http():
    """模块级 HTTP 客户端"""
    async with httpx.AsyncClient(
        base_url=API_GATEWAY_URL,
        timeout=30.0,
        headers={"Authorization": f"Bearer {INTERNAL_TOKEN}"},
    ) as client:
        yield client


@pytest.fixture(scope="module")
async def scheduler_http():
    """Scheduler 直连客户端"""
    async with httpx.AsyncClient(
        base_url=SCHEDULER_URL,
        timeout=15.0,
        headers={
            "Authorization": f"Bearer {INTERNAL_TOKEN}",
            "X-Internal-Token": INTERNAL_TOKEN,
        },
    ) as client:
        yield client


# --------------------------------------------------------------------------
# 辅助函数
# --------------------------------------------------------------------------


async def create_case(http: httpx.AsyncClient, title: str = "E2E 测试工单") -> dict:
    """创建一个测试工单，返回响应 JSON"""
    resp = await http.post(
        "/api/cases/",
        json={
            "client_id": "e2e-test-user",
            "title": title,
            "description": "E2E 自动化测试工单，请忽略",
            "source": "e2e_test",
        },
    )
    assert resp.status_code == 201, f"创建工单失败：{resp.text}"
    return resp.json()


async def create_conversation(http: httpx.AsyncClient, case_id: str) -> dict:
    """为工单创建对话"""
    resp = await http.post(f"/api/conversations/?case_id={case_id}")
    assert resp.status_code == 201, f"创建对话失败：{resp.text}"
    return resp.json()


async def close_case(http: httpx.AsyncClient, case_id: str) -> dict:
    """关闭工单"""
    resp = await http.patch(
        f"/api/cases/{case_id}",
        json={"status": "closed"},
    )
    assert resp.status_code == 200, f"关闭工单失败：{resp.text}"
    return resp.json()


# --------------------------------------------------------------------------
# E2E 测试用例
# --------------------------------------------------------------------------


@pytest.mark.e2e
@pytest.mark.asyncio
class TestPodSchedulingE2E:
    """Pod 调度 E2E 测试套件"""

    async def test_api_gateway_health(self, http):
        """前置检查：API Gateway 健康"""
        resp = await http.get("/api/health")
        assert resp.status_code == 200
        data = resp.json()
        assert data.get("status") in ("ok", "healthy")

    async def test_create_case_success(self, http):
        """工单创建返回 201 + case_id"""
        case = await create_case(http, title="E2E 工单创建测试")
        assert "case_id" in case
        assert case.get("status") == "open"

    async def test_list_cases_includes_new_case(self, http):
        """工单列表应包含刚创建的工单"""
        case = await create_case(http, title="E2E 列表查询测试")
        case_id = case["case_id"]

        resp = await http.get("/api/cases/")
        assert resp.status_code == 200
        case_ids = [c["case_id"] for c in resp.json()]
        assert case_id in case_ids, f"case_id {case_id} 未出现在工单列表中"

    async def test_full_lifecycle(self, http, scheduler_http):
        """
        完整生命周期测试：
          创建工单 → 检查 Pod 分配 → 创建对话 → 关闭工单 → 验证 Pod 释放
        """
        # 1. 创建工单
        case = await create_case(http, title="E2E 全流程测试")
        case_id = case["case_id"]
        assert case["status"] == "open"

        # 2. 等待 Scheduler 分配 Pod（最多 15s）
        allocated = False
        for _ in range(15):
            resp = await scheduler_http.get(f"/api/scheduler/allocations/{case_id}")
            if resp.status_code == 200:
                alloc = resp.json()
                if alloc.get("pod_name"):
                    allocated = True
                    break
            await asyncio.sleep(1)

        assert allocated, f"等待 15s 后 case_id={case_id} 仍未分配到 Pod"

        # 3. 创建对话
        conv = await create_conversation(http, case_id)
        conv_id = conv.get("conversation_id") or conv.get("id")
        assert conv_id

        # 4. 关闭工单
        closed = await close_case(http, case_id)
        assert closed.get("status") == "closed"

        # 5. 等待 Pod 释放（最多 10s）
        released = False
        for _ in range(10):
            resp = await scheduler_http.get(f"/api/scheduler/allocations/{case_id}")
            if resp.status_code == 404:
                released = True
                break
            await asyncio.sleep(1)

        assert released, f"关闭工单后 Pod 未被释放（case_id={case_id}）"

    async def test_pod_released_after_case_close(self, http, scheduler_http):
        """单独验证关闭工单后 Scheduler 移除分配记录"""
        case = await create_case(http, title="E2E Pod 释放验证")
        case_id = case["case_id"]

        # 等待分配（宽松：只要工单存在，Scheduler 就会尝试分配）
        await asyncio.sleep(3)

        # 关闭工单
        await close_case(http, case_id)

        # Scheduler 的分配记录最终应消失
        await asyncio.sleep(2)
        resp = await scheduler_http.get(f"/api/scheduler/allocations/{case_id}")
        assert resp.status_code in (404, 200), f"意外的状态码：{resp.status_code}"
        if resp.status_code == 200:
            data = resp.json()
            # 即使接口返回 200，pod_name 应为空或状态 released
            assert not data.get("pod_name") or data.get("status") in ("released", "idle"), (
                f"工单已关闭但 Pod 未释放：{data}"
            )

    async def test_duplicate_case_creation_is_handled(self, http):
        """创建多个工单各自独立，case_id 唯一"""
        case_a = await create_case(http, title="E2E 重复A")
        case_b = await create_case(http, title="E2E 重复B")
        assert case_a["case_id"] != case_b["case_id"]
