"""
Case Service集成测试
需要PostgreSQL运行
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


import httpx
import pytest
from app.main import app
from httpx import ASGITransport


class TestCaseServiceIntegration:
    """Case Service完整流程集成测试"""

    BASE_URL = "http://test"

    @pytest.mark.asyncio
    async def test_complete_case_workflow(self):
        """测试完整的工单流程"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=self.BASE_URL) as client:
                # 1. 创建工单
                response = await client.post(
                    f"{self.BASE_URL}/api/cases/",
                    json={
                        "client_id": "integration-test-client",
                        "title": "集成测试工单",
                        "description": "这是一个集成测试",
                    },
                    headers={"X-Trace-ID": "int-test-001"},
                )

                assert response.status_code == 201
                case_data = response.json()
                case_id = case_data["case_id"]

                assert case_data["status"] == "created"
                assert case_data["client_id"] == "integration-test-client"
                assert case_data["trace_id"] == "int-test-001"

                # 2. 查询工单详情
                response = await client.get(f"{self.BASE_URL}/api/cases/{case_id}")
                assert response.status_code == 200
                assert response.json()["case_id"] == case_id

                # 3. 确认工单
                response = await client.put(
                    f"{self.BASE_URL}/api/cases/{case_id}/confirm", headers={"X-Trace-ID": "int-test-002"}
                )
                assert response.status_code == 200
                assert response.json()["status"] == "confirmed"

                # 4. 查询客户端的所有工单
                response = await client.get(
                    f"{self.BASE_URL}/api/cases/", params={"client_id": "integration-test-client"}
                )
                assert response.status_code == 200
                cases = response.json()
                assert len(cases) >= 1
                assert any(c["case_id"] == case_id for c in cases)

                # 5. 关闭工单
                response = await client.put(
                    f"{self.BASE_URL}/api/cases/{case_id}/close", headers={"X-Trace-ID": "int-test-003"}
                )
                assert response.status_code == 200
                assert response.json()["status"] == "closed"
                assert response.json()["closed_at"] is not None

    @pytest.mark.asyncio
    async def test_health_check(self):
        """测试健康检查"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=self.BASE_URL) as client:
                response = await client.get(f"{self.BASE_URL}/health")
                assert response.status_code == 200
                assert response.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_case_not_found(self):
        """测试查询不存在的工单"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=self.BASE_URL) as client:
                response = await client.get(f"{self.BASE_URL}/api/cases/Q99999999999")
                assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_create_case_validation(self):
        """测试创建工单的数据验证"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(transport=ASGITransport(app=app), base_url=self.BASE_URL) as client:
                # 缺少必填字段
                response = await client.post(
                    f"{self.BASE_URL}/api/cases/",
                    json={"client_id": "test"},  # 缺少title
                )
                assert response.status_code == 422
