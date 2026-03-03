"""
Scheduler Service - 集成测试
"""

import uuid
from unittest.mock import MagicMock, patch

import httpx
import pytest
from app.main import app
from httpx import ASGITransport


@pytest.fixture
def mock_k8s_client():
    """Mock the K8sClient methods"""
    with patch("app.main.K8sClient", autospec=True) as mock_client:
        mock_k8s = mock_client.return_value
        # Mocking generic successful responses
        mock_k8s.create_pod.return_value = True
        mock_k8s.delete_pod.return_value = True
        mock_k8s.get_pod_status.return_value = "Running"
        mock_k8s.get_pod_ip.return_value = "10.0.0.1"
        # Return an empty list or specific items for listing pods
        # Mock item structure roughly matching k8s client obj
        mock_pod = MagicMock()
        mock_pod.metadata.name = "openclaw-warm-1"
        mock_pod.status.phase = "Running"
        mock_k8s.list_pods.return_value = [mock_pod]

        yield mock_k8s


@pytest.fixture
def test_app():
    return app


class TestSchedulerIntegration:
    BASE_URL = "http://testserver"

    @pytest.mark.asyncio
    async def test_health_check(self, test_app, mock_k8s_client):
        """测试健康检查接口"""
        async with (
            test_app.router.lifespan_context(test_app),
            httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url=self.BASE_URL) as client,
        ):
            response = await client.get(f"{self.BASE_URL}/health")
            assert response.status_code == 200
            data = response.json()
            assert data["status"] == "healthy"
            assert "service" in data

    @pytest.mark.asyncio
    async def test_allocate_and_release_pod(self, test_app, mock_k8s_client):
        """测试Pod分配和释放的完整流程"""
        case_id = f"TEST-CASE-{uuid.uuid4().hex[:6]}"
        trace_id = f"trace-{uuid.uuid4().hex[:6]}"

        async with (
            test_app.router.lifespan_context(test_app),
            httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url=self.BASE_URL) as client,
        ):
            # 1. 验证分配 (Allocate)
            response = await client.post(
                f"{self.BASE_URL}/api/scheduler/pods/allocate",
                json={"case_id": case_id},
                headers={"X-Trace-ID": trace_id},
            )
            assert response.status_code == 200
            data = response.json()
            assert "pod_name" in data

            pod_name = data["pod_name"]

            # 2. 验证再次分配返回同一个 (Idempotency / Reuse)
            response2 = await client.post(
                f"{self.BASE_URL}/api/scheduler/pods/allocate",
                json={"case_id": case_id},
                headers={"X-Trace-ID": trace_id},
            )
            assert response2.status_code == 200
            assert response2.json()["pod_name"] == pod_name

            # 3. 验证释放 (Release)
            response3 = await client.post(
                f"{self.BASE_URL}/api/scheduler/pods/release",
                json={"case_id": case_id},
                headers={"X-Trace-ID": trace_id},
            )
            assert response3.status_code == 200

    @pytest.mark.asyncio
    async def test_release_nonexistent_pod(self, test_app, mock_k8s_client):
        """测试释放不存在或未分配的Pod"""
        case_id = f"FAKE-CASE-{uuid.uuid4().hex[:6]}"

        async with (
            test_app.router.lifespan_context(test_app),
            httpx.AsyncClient(transport=ASGITransport(app=test_app), base_url=self.BASE_URL) as client,
        ):
            # Release without allocation shouldn't fail fatally, typically might ignore or return specific status
            response = await client.post(f"{self.BASE_URL}/api/scheduler/pods/release", json={"case_id": case_id})
            assert response.status_code == 404
