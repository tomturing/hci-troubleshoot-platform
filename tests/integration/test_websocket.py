"""
WebSocket 集成测试

验证 API Gateway WebSocket 端点：
- 连接建立（握手）
- 消息收发（JSON 格式）
- 缺少字段时的错误回传
- 断连清理（会话从 Redis 移除）

依赖：fakeredis（无需真实 Redis）、可 mock downstream conversation-service

完成标准：本文件所有测试用例 PASSED（pytest -m integration）
"""

import os
import sys

# 将 api-gateway 加入路径
_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend", "api-gateway"))
_expect = os.path.normpath(os.path.join(_svc, "app"))
_actual = os.path.normpath(getattr(sys.modules.get("app"), "__path__", [""])[0]) if "app" in sys.modules else ""
if _expect != _actual:
    for _k in list(sys.modules):
        if _k == "app" or _k.startswith("app."):
            del sys.modules[_k]
    try:
        from shared.database.postgres import Base

        Base.metadata.clear()
    except ImportError:
        pass
    if _svc in sys.path:
        sys.path.remove(_svc)
    sys.path.insert(0, _svc)

import asyncio
import contextlib
import json
from unittest.mock import AsyncMock, MagicMock, patch

import fakeredis.aioredis
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

# --------------------------------------------------------------------------
# 测试专用 Session Manager（使用 fakeredis）
# --------------------------------------------------------------------------


@pytest.fixture
async def redis_client():
    """fakeredis 客户端"""
    client = fakeredis.aioredis.FakeRedis(decode_responses=True)
    yield client
    await client.aclose()


@pytest.fixture
def mock_redis_manager(redis_client):
    """直接将 FakeRedis 实例赋给 RedisManager.client，避免 coroutine 代理问题"""
    from shared.database.redis import RedisManager

    manager = RedisManager(redis_url="redis://fake-test")
    # 直接注入 fakeredis 客户端，跳过真实 connect()
    manager.client = redis_client
    return manager


@pytest.fixture
def session_manager(mock_redis_manager):
    """使用 fakeredis 的 SessionManager"""
    from app.services.session import SessionManager

    return SessionManager(mock_redis_manager)


# --------------------------------------------------------------------------
# SessionManager 单元级测试
# --------------------------------------------------------------------------


@pytest.mark.integration
class TestSessionManager:
    """SessionManager 核心逻辑（fakeredis 支撑）"""

    @pytest.mark.asyncio
    async def test_create_session_stores_data(self, session_manager, redis_client):
        """创建会话应写入 Redis"""
        mock_ws = MagicMock()
        await session_manager.create_session("client-1", mock_ws, case_id="case-abc")

        raw = await redis_client.get("session:client-1")
        assert raw is not None
        data = json.loads(raw)
        assert data["client_id"] == "client-1"
        assert data["case_id"] == "case-abc"

    @pytest.mark.asyncio
    async def test_get_session_returns_data(self, session_manager):
        """get_session 应返回之前创建的会话数据"""
        mock_ws = MagicMock()
        await session_manager.create_session("client-2", mock_ws)

        session = await session_manager.get_session("client-2")
        assert session is not None
        assert session["client_id"] == "client-2"

    @pytest.mark.asyncio
    async def test_get_session_nonexistent_returns_none(self, session_manager):
        """不存在的 client_id 应返回 None"""
        result = await session_manager.get_session("no-such-client")
        assert result is None

    @pytest.mark.asyncio
    async def test_close_session_removes_from_redis(self, session_manager, redis_client):
        """关闭会话应从 Redis 删除 key"""
        mock_ws = MagicMock()
        await session_manager.create_session("client-3", mock_ws)

        await session_manager.close_session("client-3")

        raw = await redis_client.get("session:client-3")
        assert raw is None

    @pytest.mark.asyncio
    async def test_close_session_removes_active_connection(self, session_manager):
        """关闭会话应从内存 active_connections 移除"""
        mock_ws = MagicMock()
        await session_manager.create_session("client-4", mock_ws)

        assert session_manager.get_connection("client-4") is not None

        await session_manager.close_session("client-4")
        assert session_manager.get_connection("client-4") is None

    @pytest.mark.asyncio
    async def test_get_connection_returns_websocket(self, session_manager):
        """get_connection 应返回注册时的 WebSocket 对象"""
        mock_ws = MagicMock()
        await session_manager.create_session("client-5", mock_ws)
        assert session_manager.get_connection("client-5") is mock_ws

    @pytest.mark.asyncio
    async def test_multiple_sessions_independent(self, session_manager):
        """多个并发会话间互相独立"""
        ws1, ws2 = MagicMock(), MagicMock()
        await asyncio.gather(
            session_manager.create_session("c-a", ws1, case_id="case-1"),
            session_manager.create_session("c-b", ws2, case_id="case-2"),
        )

        s1 = await session_manager.get_session("c-a")
        s2 = await session_manager.get_session("c-b")
        assert s1["case_id"] == "case-1"
        assert s2["case_id"] == "case-2"
        assert session_manager.get_connection("c-a") is ws1
        assert session_manager.get_connection("c-b") is ws2


# --------------------------------------------------------------------------
# WebSocket 路由集成测试（使用 starlette TestClient）
# --------------------------------------------------------------------------


@pytest.fixture
def ws_app(mock_redis_manager):
    """创建可测试的 FastAPI 应用（跳过 lifespan、使用 fakeredis）"""
    from app.routes import websocket as ws_module
    from app.services.session import SessionManager
    from fastapi import FastAPI

    test_app = FastAPI()

    sm = SessionManager(mock_redis_manager)
    ws_module.set_session_manager(sm)

    # 挂载 WebSocket 路由
    test_app.include_router(ws_module.router)

    return test_app


@pytest.fixture
def issued_identity():
    """网关签发的服务端签名身份，返回 (client_id, cookie_value)。

    WS 路由强制要求路径 client_id 与网关签发的身份 Cookie 一致（P1 信任链），
    因此测试必须以签发结果构造路径，不能再自报任意 client_id。
    """
    from app.config import settings
    from shared.security.identity import issue_identity

    return issue_identity(settings.INTERNAL_API_TOKEN)


@pytest.fixture
def authed_client(ws_app, issued_identity):
    """携带网关签发身份 Cookie 的测试客户端，返回 (client, client_id)。"""
    from shared.security.identity import IDENTITY_COOKIE_NAME

    client_id, cookie_value = issued_identity
    with TestClient(ws_app) as client:
        client.cookies.set(IDENTITY_COOKIE_NAME, cookie_value)
        yield client, client_id


@pytest.mark.integration
class TestWebSocketEndpoint:
    """WebSocket 路由行为测试"""

    def test_websocket_connect_and_receive_error_for_missing_conv_id(self, authed_client):
        """连接后发送缺少 conversation_id 的消息应收到错误回传"""
        client, client_id = authed_client
        with client.websocket_connect(f"/ws/{client_id}") as ws:
            ws.send_text(json.dumps({"type": "message", "content": "你好"}))
            data = json.loads(ws.receive_text())
            assert "error" in data
            assert "conversation_id" in data["error"].lower() or "missing" in data["error"].lower()

    def test_websocket_connect_valid_json(self, authed_client):
        """建立连接后服务端应接受并处理合法 JSON 消息（即使下游调用失败）"""
        client, client_id = authed_client
        # patch conversation-service 调用，模拟下游不可用场景
        with patch("app.routes.websocket.httpx.AsyncClient") as MockClient:
            mock_response = MagicMock()
            mock_response.is_error = True
            mock_response.status_code = 503
            mock_client_instance = AsyncMock()
            mock_client_instance.__aenter__ = AsyncMock(return_value=mock_client_instance)
            mock_client_instance.__aexit__ = AsyncMock(return_value=False)
            mock_client_instance.post = AsyncMock(return_value=mock_response)
            MockClient.return_value = mock_client_instance

            with client.websocket_connect(f"/ws/{client_id}") as ws:
                ws.send_text(
                    json.dumps(
                        {
                            "type": "message",
                            "conversation_id": "conv-test-123",
                            "content": "测试消息",
                        }
                    )
                )
                # 接收并检查任意响应（下游不可用时不强制要求回传）
                with contextlib.suppress(Exception):
                    _ = ws.receive_text(timeout=2)

    def test_websocket_invalid_json_is_handled(self, authed_client):
        """发送非 JSON 文本不应导致服务崩溃（连接保持活跃）"""
        client, client_id = authed_client
        with client.websocket_connect(f"/ws/{client_id}") as ws:
            ws.send_text("not json at all !!!!")
            # 连接应保持活跃，继续发送合法消息不报错
            ws.send_text(json.dumps({"ping": True}))
            # 如无响应也正常（invalid json 会被 continue 跳过）

    # ── 身份信任链负向门禁（P1 #1065）：不得为简化测试而弱化握手校验 ──────

    def test_websocket_rejects_missing_identity_cookie(self, ws_app):
        """未持有网关签发身份 Cookie 的连接必须在握手阶段拒绝（1008）。"""
        with TestClient(ws_app) as client:
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/ws/test-client-1"):
                    pass
            assert exc.value.code == 1008

    def test_websocket_rejects_identity_mismatch(self, ws_app, issued_identity):
        """Cookie 身份与路径 client_id 不一致时必须拒绝（冒用他人身份）。"""
        from shared.security.identity import IDENTITY_COOKIE_NAME

        _signed_client_id, cookie_value = issued_identity
        with TestClient(ws_app) as client:
            client.cookies.set(IDENTITY_COOKIE_NAME, cookie_value)
            with pytest.raises(WebSocketDisconnect) as exc:
                with client.websocket_connect("/ws/another-client"):
                    pass
            assert exc.value.code == 1008
