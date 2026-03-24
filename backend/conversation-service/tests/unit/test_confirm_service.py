"""
ConfirmService 单元测试

覆盖：
  - request_confirm 正常确认流程（Redis BRPOP 返回确认结果）
  - request_confirm 超时返回 False
  - submit_confirm 写入 Redis 正确格式
  - 解析确认结果异常时返回 False（降级）
"""

import os
import sys

_svc = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", ".."))
if _svc not in sys.path:
    sys.path.insert(0, _svc)

import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.services.confirm_service import ConfirmService, CONFIRM_TIMEOUT, REDIS_KEY_PREFIX


@pytest.fixture
def mock_redis():
    return AsyncMock()


@pytest.fixture
def service(mock_redis):
    return ConfirmService(redis=mock_redis)


class TestRequestConfirm:

    @pytest.mark.asyncio
    async def test_request_confirm_approved_returns_true(self, service, mock_redis):
        """BRPOP 返回 confirmed=True 时，request_confirm 返回 True"""
        payload = json.dumps({"confirmed": True, "authorized_by": "user@example.com"})
        mock_redis.brpop.return_value = ("key", payload.encode())

        result = await service.request_confirm(
            session_id="sid-001",
            tool_name="get_active_alerts",
            tool_args={},
            risk_level=2,
        )

        assert result is True
        # Redis key 应先被删除（清空残留）再等待
        mock_redis.delete.assert_called_once_with(f"{REDIS_KEY_PREFIX}sid-001")

    @pytest.mark.asyncio
    async def test_request_confirm_cancelled_returns_false(self, service, mock_redis):
        """BRPOP 返回 confirmed=False 时，request_confirm 返回 False"""
        payload = json.dumps({"confirmed": False, "authorized_by": "user@example.com"})
        mock_redis.brpop.return_value = ("key", payload.encode())

        result = await service.request_confirm(
            session_id="sid-002",
            tool_name="dangerous_op",
            tool_args={"host": "node-01"},
            risk_level=2,
        )

        assert result is False

    @pytest.mark.asyncio
    async def test_request_confirm_timeout_returns_false(self, service, mock_redis):
        """BRPOP 超时（返回 None）时，request_confirm 返回 False"""
        mock_redis.brpop.return_value = None

        result = await service.request_confirm(
            session_id="sid-003",
            tool_name="restart_service",
            tool_args={},
            risk_level=2,
        )

        assert result is False
        # 确认超时时调用的 BRPOP timeout 参数应等于 CONFIRM_TIMEOUT
        mock_redis.brpop.assert_called_once_with(
            f"{REDIS_KEY_PREFIX}sid-003",
            timeout=CONFIRM_TIMEOUT,
        )

    @pytest.mark.asyncio
    async def test_request_confirm_parse_error_returns_false(self, service, mock_redis):
        """BRPOP 返回无法解析的值时，不抛异常，返回 False"""
        mock_redis.brpop.return_value = ("key", b"not-json-at-all{{{{")

        result = await service.request_confirm(
            session_id="sid-004",
            tool_name="op",
            tool_args={},
            risk_level=2,
        )

        assert result is False


class TestSubmitConfirm:

    @pytest.mark.asyncio
    async def test_submit_confirm_pushes_correct_json(self, service, mock_redis):
        """submit_confirm 应向 Redis LPUSH 包含 confirmed 和 authorized_by 的 JSON"""
        await service.submit_confirm(
            session_id="sid-005",
            confirmed=True,
            authorized_by="admin@example.com",
        )

        key = f"{REDIS_KEY_PREFIX}sid-005"
        mock_redis.lpush.assert_called_once()
        call_args = mock_redis.lpush.call_args
        pushed_key = call_args[0][0]
        pushed_value = call_args[0][1]

        assert pushed_key == key
        data = json.loads(pushed_value)
        assert data["confirmed"] is True
        assert data["authorized_by"] == "admin@example.com"

    @pytest.mark.asyncio
    async def test_submit_confirm_sets_expiry(self, service, mock_redis):
        """submit_confirm 应设置 key 过期时间（防止遗留数据）"""
        await service.submit_confirm(
            session_id="sid-006",
            confirmed=False,
            authorized_by="user",
        )

        mock_redis.expire.assert_called_once()
        expire_key = mock_redis.expire.call_args[0][0]
        expire_secs = mock_redis.expire.call_args[0][1]
        assert expire_key == f"{REDIS_KEY_PREFIX}sid-006"
        assert expire_secs == 300
