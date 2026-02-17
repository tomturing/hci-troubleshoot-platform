"""
Case Service集成测试
需要PostgreSQL运行
"""

import pytest
import httpx
import asyncio


class TestCaseServiceIntegration:
    """Case Service完整流程集成测试"""
    
    BASE_URL = "http://localhost:8001"
    
    @pytest.mark.asyncio
    async def test_complete_case_workflow(self):
        """测试完整的工单流程"""
        async with httpx.AsyncClient() as client:
            # 1. 创建工单
            response = await client.post(
                f"{self.BASE_URL}/api/cases",
                json={
                    "client_id": "integration-test-client",
                    "title": "集成测试工单",
                    "description": "这是一个集成测试"
                },
                headers={"X-Trace-ID": "int-test-001"}
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
                f"{self.BASE_URL}/api/cases/{case_id}/confirm",
                headers={"X-Trace-ID": "int-test-002"}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "confirmed"
            
            # 4. 查询客户端的所有工单
            response = await client.get(
                f"{self.BASE_URL}/api/cases",
                params={"client_id": "integration-test-client"}
            )
            assert response.status_code == 200
            cases = response.json()
            assert len(cases) >= 1
            assert any(c["case_id"] == case_id for c in cases)
            
            # 5. 关闭工单
            response = await client.put(
                f"{self.BASE_URL}/api/cases/{case_id}/close",
                headers={"X-Trace-ID": "int-test-003"}
            )
            assert response.status_code == 200
            assert response.json()["status"] == "closed"
            assert response.json()["closed_at"] is not None
    
    @pytest.mark.asyncio
    async def test_health_check(self):
        """测试健康检查"""
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.BASE_URL}/health")
            assert response.status_code == 200
            assert response.json()["status"] == "healthy"
    
    @pytest.mark.asyncio
    async def test_case_not_found(self):
        """测试查询不存在的工单"""
        async with httpx.AsyncClient() as client:
            response = await client.get(f"{self.BASE_URL}/api/cases/Q99999999999")
            assert response.status_code == 404
    
    @pytest.mark.asyncio
    async def test_create_case_validation(self):
        """测试创建工单的数据验证"""
        async with httpx.AsyncClient() as client:
            # 缺少必填字段
            response = await client.post(
                f"{self.BASE_URL}/api/cases",
                json={"client_id": "test"}  # 缺少title
            )
            assert response.status_code == 422
