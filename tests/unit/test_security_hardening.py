"""
安全加固单元测试（对应安全审计 2026-08-19 修复项）

覆盖：
1. 服务间身份签名（HMAC 往返、防篡改、防重放）
2. MessageCreate/WebSocketMessage metadata 注入防护（sim-ssh 兼容）
3. WebSocket 协议 conversation_id 必填
"""

import os
import sys
import time

import pytest

_backend = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "backend"))
if _backend not in sys.path:
    sys.path.insert(0, _backend)

from pydantic import ValidationError  # noqa: E402
from shared.models.schemas import MessageCreate, WebSocketMessage  # noqa: E402
from shared.security.identity import (  # noqa: E402
    IDENTITY_MAX_AGE_SECONDS,
    issue_identity,
    refresh_identity,
    verify_identity,
)
from shared.security.signature import sign_client_identity, verify_client_identity  # noqa: E402

SECRET = "unit-test-secret"


class TestIdentitySignature:
    """服务间身份签名（IDOR 信任链基础）"""

    def test_valid_signature_roundtrip(self):
        headers = sign_client_identity("client-A", SECRET)
        assert verify_client_identity(headers, SECRET) == "client-A"

    def test_wrong_secret_rejected(self):
        headers = sign_client_identity("client-A", SECRET)
        assert verify_client_identity(headers, "wrong-secret") is None

    def test_client_id_tamper_rejected(self):
        headers = sign_client_identity("client-A", SECRET)
        tampered = {**headers, "X-Client-ID": "client-B"}
        assert verify_client_identity(tampered, SECRET) is None

    def test_missing_signature_rejected(self):
        assert verify_client_identity({"X-Client-ID": "client-A"}, SECRET) is None
        assert verify_client_identity({}, SECRET) is None

    def test_expired_signature_rejected(self):
        """签名超出时间窗（防重放）"""
        import hashlib
        import hmac

        old_ts = str(int(time.time()) - 600)
        digest = hmac.new(SECRET.encode(), f"{old_ts}:client-A".encode(), hashlib.sha256).hexdigest()
        expired = {"X-Client-ID": "client-A", "X-Client-Signature": f"{old_ts}.{digest}"}
        assert verify_client_identity(expired, SECRET) is None

    def test_malformed_signature_rejected(self):
        bad = {"X-Client-ID": "a", "X-Client-Signature": "not-a-signature"}
        assert verify_client_identity(bad, SECRET) is None


class TestIdentityCookieLifetime:
    """身份 Cookie 生命周期（修复工单 Q2026101330905「上游服务暂时不可用」误报）。

    与服务间出口签名不同：身份 Cookie 是长寿命会话身份，绝不能在 300s 后静默
    轮换（否则用户离线数分钟后回来发消息会被归属校验 403 误杀）。
    """

    @staticmethod
    def _cookie_with_ts(client_id: str, ts: int, secret: str) -> str:
        import hashlib
        import hmac

        digest = hmac.new(secret.encode(), f"{ts}:{client_id}".encode(), hashlib.sha256).hexdigest()
        return f"{ts}.{client_id}.{digest}"

    def test_issue_and_verify_roundtrip(self):
        client_id, cookie = issue_identity(SECRET)
        assert verify_identity(cookie, SECRET) == client_id

    def test_stale_beyond_300s_still_verifies(self):
        """关键回归：旧 300s 硬窗口会导致离线数分钟后身份轮换，现必须仍验签通过。"""
        client_id = "b" * 32
        old_ts = int(time.time()) - 900  # 15 分钟前，远超旧 300s 窗口
        cookie = self._cookie_with_ts(client_id, old_ts, SECRET)
        assert verify_identity(cookie, SECRET) == client_id

    def test_beyond_max_age_rejected(self):
        client_id = "c" * 32
        ancient_ts = int(time.time()) - (IDENTITY_MAX_AGE_SECONDS + 3600)
        cookie = self._cookie_with_ts(client_id, ancient_ts, SECRET)
        assert verify_identity(cookie, SECRET) is None

    def test_future_timestamp_beyond_skew_rejected(self):
        client_id = "d" * 32
        future_ts = int(time.time()) + 10_000
        cookie = self._cookie_with_ts(client_id, future_ts, SECRET)
        assert verify_identity(cookie, SECRET) is None

    def test_forged_signature_rejected(self):
        client_id, cookie = issue_identity(SECRET)
        assert verify_identity(cookie, "wrong-secret") is None
        ts, cid, _sig = cookie.split(".")
        assert verify_identity(f"{ts}.{cid}.deadbeef", SECRET) is None

    def test_refresh_preserves_client_id(self):
        """滑动续期：保持 client_id 不变、仅刷新时间戳，新 Cookie 可验签。"""
        client_id, cookie = issue_identity(SECRET)
        refreshed = refresh_identity(client_id, SECRET)
        assert verify_identity(refreshed, SECRET) == client_id
        assert int(refreshed.split(".")[0]) >= int(cookie.split(".")[0])


class TestMessageMetadataValidation:
    """metadata 注入防护（CWE-20），不得破坏 sim-ssh 合法链路"""

    def test_sim_ssh_metadata_allowed(self):
        """sim-ssh 是合法业务模式，test_run_id 是其必填字段，必须放行"""
        msg = MessageCreate(
            case_id="CASE-123",
            role="user",
            content="帮我排查",
            metadata={"execution_mode": "sim-ssh", "test_run_id": "run-001"},
        )
        assert msg.metadata["execution_mode"] == "sim-ssh"

    def test_s0_selection_fields_allowed(self):
        msg = MessageCreate(
            case_id="CASE-123",
            role="user",
            content="选第一个",
            metadata={"selectedCategoryCode": "NET-01", "isNoneOfAbove": False},
        )
        assert msg.metadata["selectedCategoryCode"] == "NET-01"

    @pytest.mark.parametrize(
        "bad_key",
        ["bash_command", "ssh_password", "ssh_host", "ssh_credentials", "api_key", "token", "password"],
    )
    def test_dangerous_keys_rejected(self, bad_key):
        """顶层与嵌套危险字段均拒绝"""
        with pytest.raises(ValidationError):
            MessageCreate(
                case_id="C1",
                role="user",
                content="x",
                metadata={bad_key: "evil", "nested": {bad_key: "evil"}},
            )

    def test_execution_mode_whitelist(self):
        with pytest.raises(ValidationError):
            MessageCreate(case_id="C1", role="user", content="x", metadata={"execution_mode": "rm-rf"})

    def test_deep_nesting_rejected(self):
        deep = cur = {}
        for _ in range(6):
            cur["n"] = {}
            cur = cur["n"]
        with pytest.raises(ValidationError):
            MessageCreate(case_id="C1", role="user", content="x", metadata=deep)

    def test_control_characters_stripped(self):
        msg = MessageCreate(case_id="C1", role="user", content="hello\x00\x07world")
        assert "\x00" not in msg.content
        assert "\x07" not in msg.content


class TestWebSocketMessageSchema:
    """WS 协议修正：conversation_id 必填（修复实体混淆）"""

    def test_valid_ws_message(self):
        ws = WebSocketMessage(
            type="message",
            conversation_id="550e8400-e29b-41d4-a716-446655440000",
            content="hi",
            metadata={"selectedOptionId": "opt-1"},
        )
        assert ws.conversation_id.startswith("550e")

    def test_missing_conversation_id_rejected(self):
        with pytest.raises(ValidationError):
            WebSocketMessage(type="message", content="hi")

    def test_ws_metadata_dangerous_key_rejected(self):
        with pytest.raises(ValidationError):
            WebSocketMessage(
                type="message",
                conversation_id="550e8400-e29b-41d4-a716-446655440000",
                content="hi",
                metadata={"bash_command": "rm -rf /"},
            )

    def test_case_id_compat_field_allowed(self):
        """case_id 作为兼容字段保留"""
        ws = WebSocketMessage(
            type="message",
            conversation_id="550e8400-e29b-41d4-a716-446655440000",
            content="hi",
            case_id="CASE-1",
        )
        assert ws.case_id == "CASE-1"
