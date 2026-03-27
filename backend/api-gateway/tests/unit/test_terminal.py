"""
终端服务单元测试
Task 37: SSH 代理与终端交互后端能力
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.models.terminal import (
    AuthType,
    TerminalSessionCreate,
    TerminalWSMessage,
    WSMessageType,
)
from app.services.terminal import TerminalService, hash_host, mask_host


class TestHostMasking:
    """主机地址脱敏测试"""

    def test_mask_ipv4(self):
        """测试 IPv4 地址脱敏"""
        assert mask_host("192.168.1.100") == "192.168.*.*"
        assert mask_host("10.0.0.1") == "10.0.*.*"
        assert mask_host("172.16.255.255") == "172.16.*.*"

    def test_mask_domain(self):
        """测试域名脱敏"""
        result = mask_host("example.com")
        # 域名超过 8 字符，取前 4 + *4 + 后 4
        assert len(result) == 12
        assert result[:4] == "exam"
        assert result[-4:] == ".com"

    def test_mask_short_host(self):
        """测试短主机名"""
        assert mask_host("abc") == "***"

    def test_hash_host(self):
        """测试主机哈希"""
        hash1 = hash_host("192.168.1.100")
        assert len(hash1) == 16
        # 相同输入产生相同哈希
        assert hash1 == hash_host("192.168.1.100")


class TestTerminalSessionModels:
    """终端会话模型测试"""

    def test_session_create_password_auth(self):
        """测试密码认证创建"""
        session = TerminalSessionCreate(
            host="192.168.1.100",
            port=22,
            username="root",
            auth_type=AuthType.PASSWORD,
            password="testpass",
        )
        assert session.host == "192.168.1.100"
        assert session.port == 22
        assert session.auth_type == AuthType.PASSWORD

    def test_session_create_key_auth(self):
        """测试密钥认证创建"""
        session = TerminalSessionCreate(
            host="192.168.1.100",
            username="root",
            auth_type=AuthType.KEY,
            private_key="-----BEGIN RSA PRIVATE KEY-----\ntest\n-----END RSA PRIVATE KEY-----",
        )
        assert session.auth_type == AuthType.KEY

    def test_session_create_password_missing(self):
        """测试密码认证缺少密码时抛出异常"""
        with pytest.raises(ValueError, match="密码认证方式需要提供 password 字段"):
            TerminalSessionCreate(
                host="192.168.1.100",
                username="root",
                auth_type=AuthType.PASSWORD,
            )

    def test_session_create_key_missing(self):
        """测试密钥认证缺少私钥时抛出异常"""
        with pytest.raises(ValueError, match="密钥认证方式需要提供 private_key 字段"):
            TerminalSessionCreate(
                host="192.168.1.100",
                username="root",
                auth_type=AuthType.KEY,
            )


class TestWebSocketMessages:
    """WebSocket 消息测试"""

    def test_stdin_message(self):
        """测试 stdin 消息"""
        msg = TerminalWSMessage(type=WSMessageType.STDIN, data="ls -la\n")
        assert msg.type == WSMessageType.STDIN
        assert msg.data == "ls -la\n"

    def test_stdout_message(self):
        """测试 stdout 消息"""
        msg = TerminalWSMessage(type=WSMessageType.STDOUT, data="file1\nfile2\n")
        assert msg.type == WSMessageType.STDOUT

    def test_status_message(self):
        """测试 status 消息"""
        msg = TerminalWSMessage(
            type=WSMessageType.STATUS,
            state="connected",
            message="SSH 连接成功",
        )
        assert msg.type == WSMessageType.STATUS
        assert msg.state == "connected"

    def test_message_serialization(self):
        """测试消息序列化"""
        msg = TerminalWSMessage(type=WSMessageType.STDOUT, data="test output")
        json_str = msg.model_dump_json()
        assert '"type":"stdout"' in json_str
        assert '"data":"test output"' in json_str

    def test_message_deserialization(self):
        """测试消息反序列化"""
        json_str = '{"type":"stdin","data":"ls -la\\n"}'
        msg = TerminalWSMessage.model_validate_json(json_str)
        assert msg.type == WSMessageType.STDIN
        assert msg.data == "ls -la\n"


class TestTerminalService:
    """终端服务测试"""

    @pytest.fixture
    def mock_redis(self):
        """模拟 Redis 管理器"""
        redis = AsyncMock()
        redis.get = AsyncMock(return_value=None)
        redis.set = AsyncMock()
        redis.delete = AsyncMock()
        return redis

    @pytest.fixture
    def terminal_service(self, mock_redis):
        """创建终端服务实例"""
        from shared.database.redis import RedisManager

        redis_manager = MagicMock(spec=RedisManager)
        redis_manager.get = mock_redis.get
        redis_manager.set = mock_redis.set
        redis_manager.delete = mock_redis.delete

        return TerminalService(redis_manager)

    @pytest.mark.asyncio
    async def test_get_session_not_found(self, terminal_service):
        """测试获取不存在的会话"""
        session = await terminal_service.get_session("non-existent-id")
        assert session is None

    @pytest.mark.asyncio
    async def test_close_session_not_found(self, terminal_service):
        """测试关闭不存在的会话"""
        result = await terminal_service.close_session("non-existent-id")
        assert result is False

    @pytest.mark.asyncio
    async def test_create_session_auth_failed(self, terminal_service):
        """测试 SSH 认证失败"""
        import asyncssh

        with patch("app.services.terminal.asyncssh.connect") as mock_connect:
            mock_connect.side_effect = asyncssh.PermissionDenied("Permission denied")

            with pytest.raises(PermissionError, match="SSH 认证失败"):
                await terminal_service.create_session(
                    host="192.168.1.100",
                    port=22,
                    username="root",
                    auth_type=AuthType.PASSWORD,
                    password="wrongpass",
                )

    @pytest.mark.asyncio
    async def test_create_session_connection_refused(self, terminal_service):
        """测试 SSH 连接被拒绝"""
        import asyncssh

        with patch("app.services.terminal.asyncssh.connect") as mock_connect:
            mock_connect.side_effect = asyncssh.ConnectionLost("Connection lost")

            with pytest.raises(ConnectionError, match="SSH 连接被拒绝"):
                await terminal_service.create_session(
                    host="192.168.1.100",
                    port=22,
                    username="root",
                    auth_type=AuthType.PASSWORD,
                    password="testpass",
                )
