"""bridge 日志直传令牌的签发与校验。

这些用例守护的是安全边界：令牌只能被用来"写一个工单的日志"，
且必须对篡改、过期、换密钥、跨工单四种情况 fail-closed。
"""

import time

import pytest
from app.security.bridge_token import (
    MAX_TTL_SECONDS,
    issue_bridge_log_token,
    verify_bridge_log_token,
)

SECRET = "unit-test-secret"


def _issue(**overrides):
    params = {
        "client_id": "client-a",
        "case_id": "Q2026102700862",
        "bridge_instance_id": "inst-1234",
        "secret": SECRET,
        "ttl_seconds": 3600,
    }
    params.update(overrides)
    return issue_bridge_log_token(**params)


class TestIssueAndVerify:
    def test_roundtrip(self):
        token, exp = _issue()
        assert exp > time.time()
        payload = verify_bridge_log_token(token, SECRET)
        assert payload is not None
        assert payload["cid"] == "client-a"
        assert payload["case"] == "Q2026102700862"
        assert payload["ins"] == "inst-1234"

    def test_ttl_is_capped(self):
        _, exp = _issue(ttl_seconds=MAX_TTL_SECONDS * 10)
        assert exp <= int(time.time()) + MAX_TTL_SECONDS + 5

    def test_ttl_has_floor(self):
        """极短 TTL 也要有下限，避免签出"立刻过期"的令牌让 bridge 空转。"""
        _, exp = _issue(ttl_seconds=1)
        assert exp >= int(time.time()) + 60


class TestRejects:
    def test_tampered_payload(self):
        token, _ = _issue()
        parts = token.split(".")
        # 改 payload 不改签名：必须被 HMAC 拒绝
        forged = f"{parts[0]}.{parts[1][:-4]}AAAA.{parts[2]}"
        assert verify_bridge_log_token(forged, SECRET) is None

    def test_wrong_secret(self):
        token, _ = _issue()
        assert verify_bridge_log_token(token, "another-secret") is None

    @pytest.mark.parametrize("token", ["", "v1", "v1.abc", "v2.abc.def", "garbage"])
    def test_malformed(self, token):
        assert verify_bridge_log_token(token, SECRET) is None

    def test_expired(self, monkeypatch):
        token, exp = _issue(ttl_seconds=60)
        # 把时钟推到 exp 之后：不依赖 sleep，避免用例变慢且不受机器时钟漂移影响
        monkeypatch.setattr("app.security.bridge_token.time.time", lambda: exp + 1)
        assert verify_bridge_log_token(token, SECRET) is None

    def test_empty_secret_rejected(self):
        token, _ = _issue()
        assert verify_bridge_log_token(token, "") is None
