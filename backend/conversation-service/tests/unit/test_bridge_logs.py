"""
测试 bridge_logs 回采接口 - 鉴权弱化对齐 customer 路由 + 落库逻辑

覆盖：
  - _check_session_or_internal 鉴权（internal token / 占位符 token / JWT / 拒绝）
  - ingest_bridge_logs 落库（skip 无 case_id / insert 有效条目）
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.bridge_logs import (
    BridgeLogBatch,
    BridgeLogEntry,
    _check_session_or_internal,
    _parse_event_time,
    ingest_bridge_logs,
)
from fastapi import HTTPException


class TestCheckSessionOrInternal:
    """鉴权函数 _check_session_or_internal 测试"""

    def test_accepts_internal_token(self):
        """INTERNAL_API_TOKEN 通过，返回 'internal'"""
        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            result = _check_session_or_internal("Bearer hci-dev-internal-token")
            assert result == "internal"

    def test_accepts_placeholder_token(self):
        """占位符 token 通过，返回 'customer'（对齐 exec-result 路由）"""
        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            result = _check_session_or_internal("Bearer client-session-placeholder-token")
            assert result == "customer"

    def test_accepts_valid_jwt(self):
        """3 段 JWT 解析 sub 字段成功，返回用户标识"""
        import base64
        import json

        payload = {"sub": "user-abc-123"}
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        token = f"header.{payload_b64}.signature"

        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            result = _check_session_or_internal(f"Bearer {token}")
            assert result == "user-abc-123"

    def test_rejects_missing_token(self):
        """无 Authorization 头 -> 401"""
        with pytest.raises(HTTPException) as exc_info:
            _check_session_or_internal(None)
        assert exc_info.value.status_code == 401
        assert "缺少 Bearer Token" in exc_info.value.detail

    def test_rejects_non_bearer(self):
        """非 Bearer 前缀 -> 401"""
        with pytest.raises(HTTPException) as exc_info:
            _check_session_or_internal("Basic abc123")
        assert exc_info.value.status_code == 401

    def test_rejects_empty_token(self):
        """Bearer 后空串 -> 401"""
        with pytest.raises(HTTPException) as exc_info:
            _check_session_or_internal("Bearer ")
        assert exc_info.value.status_code == 401

    def test_rejects_invalid_token(self):
        """非 internal / 非占位符 / 非 3 段 JWT 的非法 token -> 401"""
        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            with pytest.raises(HTTPException) as exc_info:
                _check_session_or_internal("Bearer some-random-invalid-token")
            assert exc_info.value.status_code == 401
            assert "Token 无效" in exc_info.value.detail

    def test_rejects_jwt_without_identity(self):
        """3 段 JWT 但无 sub/user_id/user -> 401"""
        import base64
        import json

        payload = {"foo": "bar"}  # 无身份字段
        payload_b64 = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
        token = f"header.{payload_b64}.signature"

        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            with pytest.raises(HTTPException) as exc_info:
                _check_session_or_internal(f"Bearer {token}")
            assert exc_info.value.status_code == 401


class TestParseEventTime:
    """Bridge RFC3339/RFC3339Nano 时间解析测试。"""

    def test_parses_rfc3339_nano_and_normalizes_to_microseconds(self):
        """Go 纳秒时间可解析，并按 PostgreSQL 精度归一为微秒。"""
        result = _parse_event_time("2026-07-27T14:33:31.909840289Z")

        assert result == datetime(2026, 7, 27, 14, 33, 31, 909840, tzinfo=UTC)

    def test_none_remains_none(self):
        """缺失时间允许以 NULL 落库。"""
        assert _parse_event_time(None) is None

    @pytest.mark.parametrize("value", ["not-a-time", "2026-07-27T14:33:31"])
    def test_rejects_invalid_or_timezone_naive_time(self, value):
        """非法或无时区时间不得进入数据库。"""
        with pytest.raises(ValueError):
            _parse_event_time(value)


class TestIngestBridgeLogs:
    """ingest_bridge_logs 落库逻辑测试"""

    @pytest.mark.asyncio
    async def test_ingest_skips_entries_without_case_id(self):
        """无 case_id 的条目被 skip，不落库"""
        body = BridgeLogBatch(
            logs=[
                BridgeLogEntry(level="INFO", event="exec.start", message="ok"),  # 无 case_id
                BridgeLogEntry(case_id="Q001", level="INFO", event="exec.done", message="done"),
            ]
        )

        mock_session = AsyncMock()
        mock_db_manager = MagicMock()
        mock_db_manager.get_session = MagicMock()

        async def _gen():
            yield mock_session

        mock_db_manager.get_session.return_value = _gen()

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs._check_session_or_internal", return_value="customer"),
        ):
            result = await ingest_bridge_logs(body, authorization="Bearer client-session-placeholder-token")

        assert result == {"ok": True, "accepted": 1, "duplicates": 0, "skipped": 1}
        # 只有 1 条（有 case_id 的）执行了 INSERT
        assert mock_session.execute.call_count == 1
        mock_session.commit.assert_called_once()

    @pytest.mark.asyncio
    async def test_ingest_inserts_valid_entries(self):
        """有 case_id 的条目正确落库"""
        body = BridgeLogBatch(
            logs=[
                BridgeLogEntry(
                    case_id="Q2026072055042",
                    level="INFO",
                    event="ssh.connected",
                    message="SSH 连接成功",
                    trace_id="abc123",
                    custom_ui="hci.local",
                    node_ip="10.0.0.1",
                    extra={"key": "value"},
                ),
            ]
        )

        mock_session = AsyncMock()
        mock_db_manager = MagicMock()
        mock_db_manager.get_session = MagicMock()

        async def _gen():
            yield mock_session

        mock_db_manager.get_session.return_value = _gen()

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs._check_session_or_internal", return_value="customer"),
        ):
            result = await ingest_bridge_logs(body, authorization="Bearer client-session-placeholder-token")

        assert result == {"ok": True, "accepted": 1, "duplicates": 0, "skipped": 0}
        assert mock_session.execute.call_count == 1

        # 验证 INSERT 参数（session.execute(text(...), params) 为位置参数调用）
        args, _ = mock_session.execute.call_args
        bind_params = args[1]
        assert bind_params["case_id"] == "Q2026072055042"
        assert bind_params["level"] == "INFO"  # upper() 转换
        assert bind_params["event"] == "ssh.connected"

    @pytest.mark.asyncio
    async def test_ingest_skips_only_invalid_time_in_mixed_batch(self):
        """单条非法时间只跳过自身，不得使整个回采批次 500。"""
        body = BridgeLogBatch(
            logs=[
                BridgeLogEntry(
                    case_id="Q001",
                    event="ssh.connected",
                    message="nano",
                    ts="2026-07-27T14:33:31.909840289Z",
                ),
                BridgeLogEntry(case_id="Q001", event="exec.done", message="no time", ts=None),
                BridgeLogEntry(case_id="Q001", event="exec.invalid", message="bad", ts="not-a-time"),
            ]
        )

        mock_session = AsyncMock()
        mock_db_manager = MagicMock()

        async def _gen():
            yield mock_session

        mock_db_manager.get_session.return_value = _gen()

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs._check_session_or_internal", return_value="customer"),
        ):
            result = await ingest_bridge_logs(body, authorization="Bearer client-session-placeholder-token")

        assert result == {"ok": True, "accepted": 2, "duplicates": 0, "skipped": 1}
        assert mock_session.execute.call_count == 2
        first_params = mock_session.execute.call_args_list[0].args[1]
        second_params = mock_session.execute.call_args_list[1].args[1]
        assert first_params["event_time"] == datetime(2026, 7, 27, 14, 33, 31, 909840, tzinfo=UTC)
        assert second_params["event_time"] is None

    @pytest.mark.asyncio
    async def test_ingest_counts_duplicate_without_increasing_accepted(self):
        """数据库 ON CONFLICT 命中时计为 duplicate，不计 accepted。"""
        body = BridgeLogBatch(logs=[BridgeLogEntry(case_id="Q001", event="exec.done", message="duplicate")])

        duplicate_result = MagicMock(rowcount=0)
        mock_session = AsyncMock()
        mock_session.execute.return_value = duplicate_result
        mock_db_manager = MagicMock()

        async def _gen():
            yield mock_session

        mock_db_manager.get_session.return_value = _gen()

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs._check_session_or_internal", return_value="customer"),
        ):
            result = await ingest_bridge_logs(body, authorization="Bearer client-session-placeholder-token")

        assert result == {"ok": True, "accepted": 0, "duplicates": 1, "skipped": 0}

    @pytest.mark.asyncio
    async def test_ingest_returns_503_when_db_not_ready(self):
        """_db_manager 为 None 时返回 503"""
        body = BridgeLogBatch(logs=[])

        with (
            patch("app.routes.bridge_logs._db_manager", None),
            patch("app.routes.bridge_logs._check_session_or_internal", return_value="customer"),
            pytest.raises(HTTPException) as exc_info,
        ):
            await ingest_bridge_logs(body, authorization="Bearer client-session-placeholder-token")

        assert exc_info.value.status_code == 503
        assert "数据库未就绪" in exc_info.value.detail
