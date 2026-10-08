"""
测试 bridge_logs 回采接口 - L2 加固后的归属校验 + 落库逻辑

覆盖：
  - _is_internal_caller：内部直连令牌判定
  - _assert_case_owned / _verify_batch_ownership：工单归属校验（403 防护）
  - _authenticate_batch：internal / 签名 / 匿名三条分支
  - ingest_bridge_logs：落库（skip 无 case_id / insert 有效条目）、归属失败 403 传播
  - upload_bridge_logs：JSONL 解析、去重、体积上限、开关关闭、归属失败 403

说明：历史占位符 token 与 `_check_session_or_internal` 桩鉴权已在 SRC L2 修复中移除，
customer 前端经网关携带签名 X-Client-ID 做工单归属校验；内部服务直连用
Bearer <INTERNAL_API_TOKEN> 旁路。
"""

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from app.routes.bridge_logs import (
    MAX_UPLOAD_FILE_BYTES,
    BridgeLogBatch,
    BridgeLogEntry,
    BridgeLogUploadFile,
    BridgeLogUploadRequest,
    _assert_case_owned,
    _authenticate_batch,
    _is_internal_caller,
    _parse_event_time,
    _verify_batch_ownership,
    ingest_bridge_logs,
    upload_bridge_logs,
)
from fastapi import HTTPException


def _fake_request() -> MagicMock:
    """构造占位 Request（authenticate_request 已被各测试 patch，不读取其内容）。"""
    return MagicMock()


class TestIsInternalCaller:
    """内部直连令牌判定测试。"""

    def test_accepts_internal_token(self):
        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            assert _is_internal_caller("Bearer hci-dev-internal-token") is True

    def test_rejects_placeholder_token(self):
        """历史占位符 token 不再是合法凭证。"""
        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            assert _is_internal_caller("Bearer client-session-placeholder-token") is False

    @pytest.mark.parametrize("auth", [None, "", "Basic abc123", "Bearer ", "Bearer wrong-token"])
    def test_rejects_non_internal(self, auth):
        with patch("app.routes.bridge_logs.settings") as mock_settings:
            mock_settings.INTERNAL_API_TOKEN = "hci-dev-internal-token"
            assert _is_internal_caller(auth) is False


class TestAssertCaseOwned:
    """单工单归属校验测试。"""

    @staticmethod
    def _session(fetchone_value):
        session = AsyncMock()
        res = MagicMock()
        res.fetchone.return_value = fetchone_value
        session.execute.return_value = res
        return session

    @pytest.mark.asyncio
    async def test_passes_when_owner_matches(self):
        await _assert_case_owned(self._session(("cust-A",)), "Q001", "cust-A")

    @pytest.mark.asyncio
    async def test_forbidden_when_owner_mismatch(self):
        with pytest.raises(HTTPException) as exc_info:
            await _assert_case_owned(self._session(("cust-B",)), "Q001", "cust-A")
        assert exc_info.value.status_code == 403

    @pytest.mark.asyncio
    async def test_forbidden_when_case_missing(self):
        with pytest.raises(HTTPException) as exc_info:
            await _assert_case_owned(self._session(None), "Q001", "cust-A")
        assert exc_info.value.status_code == 403


class TestVerifyBatchOwnership:
    """批量回采写库前归属校验测试。"""

    @pytest.mark.asyncio
    async def test_checks_all_distinct_cases(self):
        body = BridgeLogBatch(
            logs=[
                BridgeLogEntry(case_id="Q001", event="exec.done", message="a"),
                BridgeLogEntry(case_id="Q002", event="exec.done", message="b"),
                BridgeLogEntry(case_id="Q001", event="exec.done", message="dup"),
            ],
            fallback_case_id="Q000",
        )
        mock_db_manager = MagicMock()

        async def _gen():
            yield AsyncMock()

        mock_db_manager.get_session.return_value = _gen()
        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs._assert_case_owned", new=AsyncMock()) as mock_assert,
        ):
            await _verify_batch_ownership(body, "cust-A")
        checked = {call.args[1] for call in mock_assert.await_args_list}
        assert checked == {"Q000", "Q001", "Q002"}

    @pytest.mark.asyncio
    async def test_no_cases_skips(self):
        body = BridgeLogBatch(logs=[])
        with patch("app.routes.bridge_logs._assert_case_owned", new=AsyncMock()) as mock_assert:
            await _verify_batch_ownership(body, "cust-A")
        mock_assert.assert_not_awaited()


class TestAuthenticateBatch:
    """回采鉴权编排测试。"""

    @pytest.mark.asyncio
    async def test_internal_bypasses_signature(self):
        body = BridgeLogBatch(logs=[])
        with (
            patch("app.routes.bridge_logs.settings") as mock_settings,
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock()) as mock_auth,
        ):
            mock_settings.INTERNAL_API_TOKEN = "internal-token"
            user_id = await _authenticate_batch(body, _fake_request(), "Bearer internal-token")
        assert user_id == "internal"
        mock_auth.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_signed_client_verifies_ownership(self):
        body = BridgeLogBatch(logs=[BridgeLogEntry(case_id="Q001", event="e", message="m")])
        with (
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value="cust-A")),
            patch("app.routes.bridge_logs._verify_batch_ownership", new=AsyncMock()) as mock_verify,
        ):
            user_id = await _authenticate_batch(body, _fake_request(), None)
        assert user_id == "cust-A"
        mock_verify.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_anonymous_transitional_skips_ownership(self):
        body = BridgeLogBatch(logs=[BridgeLogEntry(case_id="Q001", event="e", message="m")])
        with (
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
            patch("app.routes.bridge_logs._verify_batch_ownership", new=AsyncMock()) as mock_verify,
        ):
            user_id = await _authenticate_batch(body, _fake_request(), None)
        assert user_id == "anonymous"
        mock_verify.assert_not_awaited()


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
    """ingest_bridge_logs 落库逻辑测试（过渡模式匿名，跳过归属）。"""

    @staticmethod
    def _db_manager(mock_session):
        mock_db_manager = MagicMock()

        async def _gen():
            yield mock_session

        mock_db_manager.get_session.return_value = _gen()
        return mock_db_manager

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
        with (
            patch("app.routes.bridge_logs._db_manager", self._db_manager(mock_session)),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
        ):
            result = await ingest_bridge_logs(body, _fake_request(), authorization=None)

        assert result == {"ok": True, "accepted": 1, "duplicates": 0, "skipped": 1}
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
        with (
            patch("app.routes.bridge_logs._db_manager", self._db_manager(mock_session)),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
        ):
            result = await ingest_bridge_logs(body, _fake_request(), authorization=None)

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
        with (
            patch("app.routes.bridge_logs._db_manager", self._db_manager(mock_session)),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
        ):
            result = await ingest_bridge_logs(body, _fake_request(), authorization=None)

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
        with (
            patch("app.routes.bridge_logs._db_manager", self._db_manager(mock_session)),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
        ):
            result = await ingest_bridge_logs(body, _fake_request(), authorization=None)

        assert result == {"ok": True, "accepted": 0, "duplicates": 1, "skipped": 0}

    @pytest.mark.asyncio
    async def test_ingest_returns_503_when_db_not_ready(self):
        """_db_manager 为 None 时返回 503"""
        body = BridgeLogBatch(logs=[])

        with (
            patch("app.routes.bridge_logs._db_manager", None),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
            pytest.raises(HTTPException) as exc_info,
        ):
            await ingest_bridge_logs(body, _fake_request(), authorization=None)

        assert exc_info.value.status_code == 503
        assert "数据库未就绪" in exc_info.value.detail

    @pytest.mark.asyncio
    async def test_ingest_uses_fallback_case_id_for_entries_without_case(self):
        """条目缺 case_id 时按 fallback_case_id 归档，不再整体丢弃。"""
        body = BridgeLogBatch(
            logs=[BridgeLogEntry(level="INFO", event="bridge.startup", message="started")],
            fallback_case_id="Q2026092010235",
        )

        mock_session = AsyncMock()
        with (
            patch("app.routes.bridge_logs._db_manager", self._db_manager(mock_session)),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
        ):
            result = await ingest_bridge_logs(body, _fake_request(), authorization=None)

        assert result == {"ok": True, "accepted": 1, "duplicates": 0, "skipped": 0}
        args, _ = mock_session.execute.call_args
        assert args[1]["case_id"] == "Q2026092010235"

    @pytest.mark.asyncio
    async def test_ingest_forbidden_before_insert_when_ownership_fails(self):
        """签名身份归属校验失败时 403，且在写库前抛出（不落库）。"""
        body = BridgeLogBatch(logs=[BridgeLogEntry(case_id="Q001", event="exec.done", message="x")])

        mock_session = AsyncMock()
        with (
            patch("app.routes.bridge_logs._db_manager", self._db_manager(mock_session)),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value="attacker")),
            patch(
                "app.routes.bridge_logs._verify_batch_ownership",
                new=AsyncMock(side_effect=HTTPException(status_code=403, detail="无权操作此工单的日志")),
            ),
            pytest.raises(HTTPException) as exc_info,
        ):
            await ingest_bridge_logs(body, _fake_request(), authorization=None)

        assert exc_info.value.status_code == 403
        mock_session.execute.assert_not_called()
        mock_session.commit.assert_not_called()

    @pytest.mark.asyncio
    async def test_ingest_internal_token_bypasses_ownership(self):
        """内部直连令牌旁路归属校验，正常落库。"""
        body = BridgeLogBatch(logs=[BridgeLogEntry(case_id="Q001", event="exec.done", message="x")])

        mock_session = AsyncMock()
        with (
            patch("app.routes.bridge_logs._db_manager", self._db_manager(mock_session)),
            patch("app.routes.bridge_logs._is_internal_caller", return_value=True),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock()) as mock_auth,
        ):
            result = await ingest_bridge_logs(body, _fake_request(), authorization="Bearer internal-token")

        assert result["ok"] is True
        assert result["accepted"] == 1
        mock_auth.assert_not_awaited()


class TestUploadBridgeLogs:
    """本地日志手动上传补采接口测试"""

    @staticmethod
    def _build_db(rowcount_sequence: list[int]) -> tuple[MagicMock, AsyncMock]:
        """构造按调用顺序返回 rowcount 的会话 mock（0 表示被去重）。"""
        mock_session = AsyncMock()
        results = []
        for rowcount in rowcount_sequence:
            result = MagicMock()
            result.rowcount = rowcount
            results.append(result)
        mock_session.execute = AsyncMock(side_effect=results)
        mock_db_manager = MagicMock()

        async def _gen():
            yield mock_session

        mock_db_manager.get_session.return_value = _gen()
        return mock_db_manager, mock_session

    @pytest.mark.asyncio
    async def test_upload_parses_jsonl_and_binds_case(self):
        """JSONL 逐行解析：条目自带 case_id 优先，缺省继承上传绑定工单。"""
        content = "\n".join(
            [
                '{"event":"exec.done","message":"done","case_id":"Q001","level":"INFO"}',
                '{"event":"bridge.connected","message":"connected"}',
                "not-json-line",
            ]
        )
        body = BridgeLogUploadRequest(
            case_id="Q2026092010235",
            files=[
                BridgeLogUploadFile(name="bridge-20260920-abcd1234.log", content=content),
            ],
        )
        mock_db_manager, mock_session = self._build_db([1, 1])

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
            patch("app.routes.bridge_logs.settings") as mock_settings,
        ):
            mock_settings.BRIDGE_LOG_UPLOAD_ENABLED = True
            result = await upload_bridge_logs(body, _fake_request(), authorization=None)

        assert result["ok"] is True
        assert result["accepted"] == 2
        assert result["invalid"] == 1
        assert result["files"][0]["name"] == "bridge-20260920-abcd1234.log"

        first_params = mock_session.execute.call_args_list[0].args[1]
        second_params = mock_session.execute.call_args_list[1].args[1]
        assert first_params["case_id"] == "Q001"
        assert second_params["case_id"] == "Q2026092010235"
        # 上传来源留痕，便于区分自动回采与手动补采
        assert '"source": "manual_upload"' in second_params["extra"]

    @pytest.mark.asyncio
    async def test_upload_deduplicates_repeated_upload(self):
        """重复上传同一文件：ON CONFLICT 命中后计入 duplicates，不重复落库。"""
        content = (
            '{"event":"exec.done","message":"done","case_id":"Q001","event_id":"11111111-1111-1111-1111-111111111111"}'
        )
        body = BridgeLogUploadRequest(
            case_id="Q001",
            files=[
                BridgeLogUploadFile(name="bridge.log", content=content),
            ],
        )
        mock_db_manager, _ = self._build_db([0])

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
            patch("app.routes.bridge_logs.settings") as mock_settings,
        ):
            mock_settings.BRIDGE_LOG_UPLOAD_ENABLED = True
            result = await upload_bridge_logs(body, _fake_request(), authorization=None)

        assert result["duplicates"] == 1
        assert result["accepted"] == 0

    @pytest.mark.asyncio
    async def test_upload_reports_invalid_lines(self):
        """非法 JSON 行计入 invalid，不得导致整批上传失败。"""
        body = BridgeLogUploadRequest(
            case_id="Q001",
            files=[
                BridgeLogUploadFile(name="bridge.log", content="{broken\n"),
            ],
        )
        mock_db_manager, _ = self._build_db([1])

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
            patch("app.routes.bridge_logs.settings") as mock_settings,
        ):
            mock_settings.BRIDGE_LOG_UPLOAD_ENABLED = True
            result = await upload_bridge_logs(body, _fake_request(), authorization=None)

        assert result["ok"] is True
        assert result["invalid"] == 1

    @pytest.mark.asyncio
    async def test_upload_returns_404_when_disabled(self):
        """开关关闭时入口不可用（前端据此隐藏上传入口）"""
        body = BridgeLogUploadRequest(
            case_id="Q001",
            files=[
                BridgeLogUploadFile(name="bridge.log", content='{"event":"exec.done"}'),
            ],
        )

        with (
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
            patch("app.routes.bridge_logs.settings") as mock_settings,
            pytest.raises(HTTPException) as exc_info,
        ):
            mock_settings.BRIDGE_LOG_UPLOAD_ENABLED = False
            await upload_bridge_logs(body, _fake_request(), authorization=None)

        assert exc_info.value.status_code == 404

    @pytest.mark.asyncio
    async def test_upload_rejects_oversized_file(self):
        """超过体积上限的文件被拒绝，避免请求被大文件拖垮"""
        body = BridgeLogUploadRequest(
            case_id="Q001",
            files=[
                BridgeLogUploadFile(name="bridge.log", content="x" * (MAX_UPLOAD_FILE_BYTES + 1)),
            ],
        )

        # 注意：_db_manager 必须提供真实异步会话，否则 `async for` 不会进入循环体
        mock_db_manager, _ = self._build_db([1])

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value=None)),
            patch("app.routes.bridge_logs.settings") as mock_settings,
            pytest.raises(HTTPException) as exc_info,
        ):
            mock_settings.BRIDGE_LOG_UPLOAD_ENABLED = True
            await upload_bridge_logs(body, _fake_request(), authorization=None)

        assert exc_info.value.status_code == 413

    @pytest.mark.asyncio
    async def test_upload_forbidden_and_rolls_back_when_ownership_fails(self):
        """签名身份对绑定工单无归属时 403，写库前抛出、不提交（整单回滚不落库）。"""
        content = '{"event":"exec.done","message":"done","case_id":"Q001"}'
        body = BridgeLogUploadRequest(
            case_id="Q001",
            files=[
                BridgeLogUploadFile(name="bridge.log", content=content),
            ],
        )
        mock_db_manager, mock_session = self._build_db([1])

        with (
            patch("app.routes.bridge_logs._db_manager", mock_db_manager),
            patch("app.routes.bridge_logs.authenticate_request", new=AsyncMock(return_value="attacker")),
            patch(
                "app.routes.bridge_logs._assert_case_owned",
                new=AsyncMock(side_effect=HTTPException(status_code=403, detail="无权操作此工单的日志")),
            ),
            patch("app.routes.bridge_logs.settings") as mock_settings,
            pytest.raises(HTTPException) as exc_info,
        ):
            mock_settings.BRIDGE_LOG_UPLOAD_ENABLED = True
            await upload_bridge_logs(body, _fake_request(), authorization=None)

        assert exc_info.value.status_code == 403
        mock_session.execute.assert_not_called()
        mock_session.commit.assert_not_called()
