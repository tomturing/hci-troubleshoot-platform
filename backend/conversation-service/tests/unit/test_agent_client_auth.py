"""AgentClient 鉴权与链路请求头单元测试

验证 AgentClient 在请求 agent-service 的 react-confirm、interactive-response 端点时，
必须显式注入 Authorization: Bearer <internal_token>，防止 401 Unauthorized 阻断确认回路。
"""

import httpx
import pytest
from app.services.agent_client import AgentClient


@pytest.mark.asyncio
async def test_agent_client_react_confirm_sends_authorization_header():
    """验证 react_confirm 请求会携带 Authorization Bearer token"""
    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        return httpx.Response(200, json={"ok": True})

    transport = httpx.MockTransport(handler)
    client = AgentClient("http://agent-service:8005", internal_token="test-secret-token")

    original_client = httpx.AsyncClient

    def mock_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    import app.services.agent_client as ac_mod
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ac_mod.httpx, "AsyncClient", mock_async_client)

    try:
        success = await client.react_confirm(
            session_id="test-session-123",
            confirmed=True,
            authorized_by="test_user",
            exec_id="exec-456",
        )
        assert success is True
        assert captured_headers.get("authorization") == "Bearer test-secret-token"
        assert captured_headers.get("content-type") == "application/json"
    finally:
        monkeypatch.undo()


@pytest.mark.asyncio
async def test_agent_client_submit_interactive_response_sends_authorization():
    """验证 submit_interactive_response 同样携带 Authorization 头"""
    captured_headers = {}

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal captured_headers
        captured_headers = dict(request.headers)
        return httpx.Response(200, json={"success": True})

    transport = httpx.MockTransport(handler)
    client = AgentClient("http://agent-service:8005", internal_token="test-secret-token")

    original_client = httpx.AsyncClient

    def mock_async_client(*args, **kwargs):
        kwargs["transport"] = transport
        return original_client(*args, **kwargs)

    import app.services.agent_client as ac_mod
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(ac_mod.httpx, "AsyncClient", mock_async_client)

    try:
        success = await client.submit_interactive_response(
            acp_session_id="session-789",
            request_id="req-001",
            outcome={"outcome": "selected", "optionId": "approved"},
        )
        assert success is True
        assert captured_headers.get("authorization") == "Bearer test-secret-token"
    finally:
        monkeypatch.undo()
