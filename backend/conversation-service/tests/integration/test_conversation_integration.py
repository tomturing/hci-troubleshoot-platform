import pytest
import httpx
from httpx import ASGITransport
import uuid
import json
from unittest.mock import patch


from app.main import app
from sqlalchemy import text
from shared.database.postgres import DatabaseManager
from app.config import settings

@pytest.fixture(scope="module", autouse=True)
async def setup_test_cases():
    """在模块开始前插入必要的测试 Case 和 User 数据"""
    test_db = DatabaseManager(settings.DATABASE_URL)
    test_uuids = [
        "1e106d60-0fe5-4c00-9421-1c4da35d128c",
        "0ceb21a2-2da6-449f-bbf7-f43d515b2d7c",
        "3fd03725-d003-4354-be46-6f4370beca8d",
        "971bfb12-f3d0-4680-91e6-1415e26be8ca",
        "6ba79191-6cff-4f80-a0d0-327f1e1ae98f",
        "6b9a8f4c-3f2d-4c0e-8f2c-5c4d3b8f1a9e",
        "e3b0c442-989b-464c-8693-b0a8c4f9a5e1",
        "5f187313-2d2c-493a-814a-59424d8622f9"
    ]
    
    test_cases = [
        {"case_id": "Q202602220001", "trace_id": "inttest-conv-001"},
        {"case_id": "Q202602220002", "trace_id": "inttest-conv-002"},
        {"case_id": "Q202602220003", "trace_id": "inttest-conv-003"},
        {"case_id": "Q202602220004", "trace_id": "inttest-conv-004"},
        {"case_id": "Q202602220005", "trace_id": "inttest-conv-005"}
    ]
    
    for i, test_case in enumerate(test_cases):
        test_uuid = test_uuids[i]
        
        async for session in test_db.get_session():
            # 插入必需的测试用户，使用 test_uuid 作为 client_id 保证唯一性
            await session.execute(
                text("""
                    INSERT INTO "user" (user_id, client_id, username, trace_id)
                    VALUES (:uid, :client_id, 'test-user-int', 'inttest-setup')
                    ON CONFLICT (user_id) DO NOTHING
                """),
                {"uid": test_uuid, "client_id": f"test-client-{test_uuid}"}
            )
            
            # 再插入测试工单
            await session.execute(
                text("""
                    INSERT INTO "case" (case_id, title, status, client_id, user_id, trace_id)
                    VALUES (:cid, 'Integration Test Case', 'created', :client_id, :uid, :tid)
                    ON CONFLICT (case_id) DO NOTHING
                """),
                {
                    "cid": test_case["case_id"], 
                    "client_id": f"test-client-{test_uuid}",
                    "uid": test_uuid, 
                    "tid": test_case["trace_id"]
                }
            )
            await session.commit()
    
    yield
    await test_db.close()

@pytest.mark.asyncio
class TestConversationServiceIntegration:
    """对话服务集成测试"""
    
    BASE_URL = "http://test"

    @pytest.mark.asyncio
    async def test_health_check(self):
        """测试健康检查"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.get(f"{self.BASE_URL}/health")
                assert response.status_code == 200
                assert response.json()["status"] == "healthy"

    @pytest.mark.asyncio
    async def test_create_conversation(self):
        """测试创建对话"""
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
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
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
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
        async with app.router.lifespan_context(app):
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.get(
                    f"{self.BASE_URL}/api/conversations/{fake_id}"
                )
                assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_send_message_and_stream(self):
        """测试发送消息和SSE流返回"""
        # Mock OpenClaw stream
        async def mock_stream(*args, **kwargs):
            yield "测试"
            yield "流式"
            yield "响应"
            
        async with app.router.lifespan_context(app):
            # 1. 创建对话
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url=self.BASE_URL
            ) as client:
                create_resp = await client.post(
                    f"{self.BASE_URL}/api/conversations/",
                    params={"case_id": "Q202602220003"},
                    headers={"X-Trace-ID": "inttest-conv-003"}
                )
                conv_id = create_resp.json()["conversation_id"]

            # 2. 发送消息
            with patch(
                'app.services.openclaw_client.OpenClawClient.chat_completion_stream',
                side_effect=mock_stream
            ):
                async with httpx.AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url=self.BASE_URL
                ) as client:
                    response = await client.post(
                        f"{self.BASE_URL}/api/conversations/{conv_id}/message",
                        json={
                            "role": "user",
                            "content": "你好，请帮我排查问题",
                            "case_id": "Q202602220003"
                        },
                        headers={"X-Trace-ID": "inttest-conv-003"}
                    )
                    
                    if response.status_code != 200:
                        print(f"Error {response.status_code}: {response.text}")
                    assert response.status_code == 200
                    body = response.text
                    assert "data:" in body
                    assert "[DONE]" in body
            
            import asyncio
            await asyncio.sleep(1.0)

    @pytest.mark.asyncio
    async def test_get_conversation_messages(self):
        """测试查询对话消息历史"""
        async def mock_stream(*args, **kwargs):
            yield "测试回复"

        async with app.router.lifespan_context(app):
            # 1. 创建对话
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url=self.BASE_URL
            ) as client:
                create_resp = await client.post(
                    f"{self.BASE_URL}/api/conversations/",
                    params={"case_id": "Q202602220004"},
                    headers={"X-Trace-ID": "inttest-conv-004"}
                )
                conv_id = create_resp.json()["conversation_id"]

            # 2. 发送消息
            with patch(
                'app.services.openclaw_client.OpenClawClient.chat_completion_stream',
                side_effect=mock_stream
            ):
                async with httpx.AsyncClient(
                    transport=ASGITransport(app=app),
                    base_url=self.BASE_URL
                ) as client:
                    # 使用 stream 方法正确消费 SSE 流
                    async with client.stream(
                        "POST",
                        f"{self.BASE_URL}/api/conversations/{conv_id}/message",
                        json={
                            "role": "user",
                            "content": "问题描述",
                            "case_id": "Q202602220004"
                        },
                        headers={"X-Trace-ID": "inttest-conv-004"}
                    ) as response:
                        assert response.status_code == 200
                        # 消费流内容以确保后端迭代完成
                        async for chunk in response.aiter_text():
                            pass
                            
            import asyncio
            # 给背景任务(特别是异步 session)足够的完成时间
            await asyncio.sleep(2.0)

            # 3. 查询消息历史
            async with httpx.AsyncClient(
                transport=ASGITransport(app=app),
                base_url=self.BASE_URL
            ) as client:
                response = await client.get(
                    f"{self.BASE_URL}/api/conversations/{conv_id}/messages"
                )
                assert response.status_code == 200
                messages = response.json()
                assert len(messages) >= 2
                roles = [m["role"] for m in messages]
                assert "user" in roles
                assert "assistant" in roles
