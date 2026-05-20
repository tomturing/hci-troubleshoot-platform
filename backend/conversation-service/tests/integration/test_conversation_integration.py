"""
Conversation Service 集成测试

测试 conversation-service API 端点，需要数据库支持。
在没有数据库的环境中，所有测试会自动跳过。
"""

import uuid

import httpx
import pytest
from app.config import settings
from app.main import app
from httpx import ASGITransport


@pytest.fixture(scope="module", autouse=True)
async def check_database():
    """检查数据库是否可用"""
    if not settings.DATABASE_URL:
        pytest.skip("DATABASE_URL not configured")
    try:
        from shared.database.postgres import DatabaseManager
        db = DatabaseManager(settings.DATABASE_URL)
        async for session in db.get_session():
            await session.execute("SELECT 1")
            await session.commit()
            break
        await db.close()
    except Exception as e:
        pytest.skip(f"Database not available: {e}")


@pytest.mark.integration
@pytest.mark.asyncio
class TestConversationServiceIntegration:
    """对话服务集成测试"""

    BASE_URL = "http://test"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """测试健康检查端点"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url=self.BASE_URL
        ) as client:
            response = await client.get(f"{self.BASE_URL}/health")
            # 健康检查应返回 200（即使状态为 degraded）
            assert response.status_code == 200
            data = response.json()
            assert "status" in data
            assert "service" in data

    @pytest.mark.asyncio
    async def test_health_live(self):
        """测试存活探针"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url=self.BASE_URL
        ) as client:
            response = await client.get(f"{self.BASE_URL}/health/live")
            assert response.status_code == 200
            assert response.json()["status"] == "alive"

    @pytest.mark.asyncio
    async def test_create_conversation(self):
        """测试创建对话"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url=self.BASE_URL
        ) as client:
            response = await client.post(
                f"{self.BASE_URL}/api/conversations/",
                params={"case_id": "Q202602220001"},
                headers={"X-Trace-ID": "inttest-conv-001"}
            )
            assert response.status_code == 201
            data = response.json()
            assert "conversation_id" in data
            assert data["case_id"] == "Q202602220001"

    @pytest.mark.asyncio
    async def test_create_and_get_conversation(self):
        """测试创建并获取对话"""
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url=self.BASE_URL
        ) as client:
            create_resp = await client.post(
                f"{self.BASE_URL}/api/conversations/",
                params={"case_id": "Q202602220002"},
                headers={"X-Trace-ID": "inttest-conv-002"}
            )
            assert create_resp.status_code == 201
            conv_data = create_resp.json()
            conv_id = conv_data["conversation_id"]

            get_resp = await client.get(
                f"{self.BASE_URL}/api/conversations/{conv_id}"
            )
            assert get_resp.status_code == 200
            assert get_resp.json()["conversation_id"] == conv_id

    @pytest.mark.asyncio
    async def test_get_nonexistent_conversation(self):
        """测试获取不存在的对话"""
        fake_id = str(uuid.uuid4())
        async with app.router.lifespan_context(app), httpx.AsyncClient(
            transport=ASGITransport(app=app),
            base_url=self.BASE_URL
        ) as client:
            response = await client.get(
                f"{self.BASE_URL}/api/conversations/{fake_id}"
            )
            assert response.status_code == 404