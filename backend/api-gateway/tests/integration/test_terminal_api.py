"""
终端 API 集成测试
Task 37: SSH 代理与终端交互后端能力
"""

import json
from unittest.mock import AsyncMock

import pytest
from app.main import app
from app.models.terminal import TerminalSessionInfo, TerminalSessionStatus
from fastapi.testclient import TestClient

pytestmark = pytest.mark.integration


@pytest.fixture
def mock_terminal_service():
    """模拟终端服务"""
    service = AsyncMock()

    # 模拟创建会话
    service.create_session = AsyncMock(
        return_value=(
            "test-session-id",
            TerminalSessionInfo(
                session_id="test-session-id",
                host="192.168.*.*",
                port=22,
                username="root",
                client_id="test-client-id",
                status=TerminalSessionStatus.CONNECTED,
                created_at="2024-01-01T00:00:00Z",
            ),
        )
    )

    # 模拟获取会话
    service.get_session = AsyncMock(
        return_value=TerminalSessionInfo(
            session_id="test-session-id",
            host="192.168.*.*",
            port=22,
            username="root",
            client_id="test-client-id",
            status=TerminalSessionStatus.CONNECTED,
            created_at="2024-01-01T00:00:00Z",
        )
    )

    # 模拟关闭会话
    service.close_session = AsyncMock(return_value=True)

    return service


@pytest.fixture
def mock_redis_manager():
    """模拟 Redis 管理器"""
    manager = AsyncMock()
    manager.connect = AsyncMock()
    manager.close = AsyncMock()
    manager.health_check = AsyncMock(return_value=True)
    return manager


@pytest.fixture
def auth_headers():
    """模拟客户端标识请求头"""
    return {"X-Client-ID": "test-client-id"}


@pytest.fixture
def test_client(monkeypatch, mock_terminal_service, mock_redis_manager, auth_headers):
    """构造测试客户端（屏蔽真实 Redis 依赖）"""

    async def fake_connect(self):
        self.client = AsyncMock()

    async def fake_close(self):
        return None

    monkeypatch.setattr("shared.database.redis.RedisManager.connect", fake_connect)
    monkeypatch.setattr("shared.database.redis.RedisManager.close", fake_close)
    monkeypatch.setattr("app.services.terminal.TerminalService.start", AsyncMock())
    monkeypatch.setattr("app.services.terminal.TerminalService.shutdown", AsyncMock())

    with TestClient(app, headers=auth_headers) as client:
        app.state.terminal_service = mock_terminal_service
        app.state.redis_manager = mock_redis_manager
        yield client


class TestTerminalHTTPAPI:
    """终端 HTTP API 测试"""

    def test_create_session_success(self, test_client):
        """测试创建会话成功"""
        response = test_client.post(
            "/api/terminal/sessions",
            json={
                "host": "192.168.1.100",
                "port": 22,
                "username": "root",
                "auth_type": "password",
                "password": "testpass",
            },
        )

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "test-session-id"
        assert data["status"] == "connected"
        assert data["host"] == "192.168.*.*"  # 已脱敏

    def test_create_session_auth_failed(self, mock_terminal_service, test_client):
        """测试创建会话认证失败"""
        mock_terminal_service.create_session.side_effect = PermissionError("SSH 认证失败")

        response = test_client.post(
            "/api/terminal/sessions",
            json={
                "host": "192.168.1.100",
                "username": "root",
                "auth_type": "password",
                "password": "wrongpass",
            },
        )

        assert response.status_code == 401

    def test_create_session_connection_refused(self, mock_terminal_service, test_client):
        """测试创建会话连接被拒绝"""
        mock_terminal_service.create_session.side_effect = ConnectionError("SSH 连接被拒绝: Connection refused")

        response = test_client.post(
            "/api/terminal/sessions",
            json={
                "host": "192.168.1.100",
                "username": "root",
                "auth_type": "password",
                "password": "testpass",
            },
        )

        assert response.status_code == 503

    def test_close_session_success(self, test_client):
        """测试关闭会话成功"""
        response = test_client.post("/api/terminal/sessions/test-session-id/close")

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "test-session-id"
        assert data["status"] == "closed"

    def test_close_session_not_found(self, mock_terminal_service, test_client):
        """测试关闭不存在的会话"""
        mock_terminal_service.get_session = AsyncMock(return_value=None)
        mock_terminal_service.close_session = AsyncMock(return_value=False)

        response = test_client.post("/api/terminal/sessions/non-existent/close")

        assert response.status_code == 404

    def test_get_session_success(self, test_client):
        """测试获取会话信息成功"""
        response = test_client.get("/api/terminal/sessions/test-session-id")

        assert response.status_code == 200
        data = response.json()
        assert data["session_id"] == "test-session-id"

    def test_get_session_not_found(self, mock_terminal_service, test_client):
        """测试获取不存在的会话"""
        mock_terminal_service.get_session = AsyncMock(return_value=None)

        response = test_client.get("/api/terminal/sessions/non-existent")

        assert response.status_code == 404


class TestTerminalWebSocketProtocol:
    """终端 WebSocket 协议测试"""

    def test_message_types(self):
        """测试消息类型定义"""
        from app.models.terminal import WSMessageType

        # 客户端 -> 服务端
        assert WSMessageType.STDIN.value == "stdin"
        assert WSMessageType.RESIZE.value == "resize"
        assert WSMessageType.PING.value == "ping"

        # 服务端 -> 客户端
        assert WSMessageType.STDOUT.value == "stdout"
        assert WSMessageType.STDERR.value == "stderr"
        assert WSMessageType.STATUS.value == "status"
        assert WSMessageType.PONG.value == "pong"
        assert WSMessageType.ERROR.value == "error"

    def test_stdin_message_format(self):
        """测试 stdin 消息格式"""
        from app.models.terminal import TerminalWSMessage, WSMessageType

        msg = TerminalWSMessage(type=WSMessageType.STDIN, data="ls -la\n")
        json_str = msg.model_dump_json()
        parsed = json.loads(json_str)

        assert parsed["type"] == "stdin"
        assert parsed["data"] == "ls -la\n"

    def test_stdout_message_format(self):
        """测试 stdout 消息格式"""
        from app.models.terminal import TerminalWSMessage, WSMessageType

        msg = TerminalWSMessage(type=WSMessageType.STDOUT, data="file1.txt\nfile2.txt\n")
        json_str = msg.model_dump_json()
        parsed = json.loads(json_str)

        assert parsed["type"] == "stdout"
        assert "file1.txt" in parsed["data"]

    def test_stderr_message_format(self):
        """测试 stderr 消息格式"""
        from app.models.terminal import TerminalWSMessage, WSMessageType

        msg = TerminalWSMessage(type=WSMessageType.STDERR, data="Error: file not found\n")
        json_str = msg.model_dump_json()
        parsed = json.loads(json_str)

        assert parsed["type"] == "stderr"
        assert "Error" in parsed["data"]

    def test_status_message_format(self):
        """测试 status 消息格式"""
        from app.models.terminal import TerminalSessionStatus, TerminalStatusMessage

        msg = TerminalStatusMessage(
            state=TerminalSessionStatus.CONNECTED,
            message="SSH 连接成功",
        )
        json_str = msg.model_dump_json()
        parsed = json.loads(json_str)

        assert parsed["type"] == "status"
        assert parsed["state"] == "connected"
        assert parsed["message"] == "SSH 连接成功"

    def test_error_message_format(self):
        """测试 error 消息格式"""
        from app.models.terminal import TerminalWSMessage, WSMessageType

        msg = TerminalWSMessage(
            type=WSMessageType.ERROR,
            data="会话不存在或已过期",
        )
        json_str = msg.model_dump_json()
        parsed = json.loads(json_str)

        assert parsed["type"] == "error"
        assert "会话不存在" in parsed["data"]
