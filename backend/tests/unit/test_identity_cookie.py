"""
身份 Cookie 模块单元测试（P0 修复）

纯函数测试，无 DB 依赖。验证：
- 签发/验签往返一致
- 错误密钥 / 篡改 / 缺失均被拒绝
"""

import uuid

from shared.security.identity import IDENTITY_COOKIE_NAME, issue_identity, verify_identity

SECRET = "test-internal-token"


def test_issue_and_verify_roundtrip():
    client_id, cookie = issue_identity(SECRET)
    assert client_id and len(client_id) == 32  # uuid4 hex
    uuid.UUID(client_id)  # 必须是合法 uuid
    assert cookie.startswith(str(int(cookie.split(".")[0])))
    assert verify_identity(cookie, SECRET) == client_id


def test_verify_rejects_wrong_secret():
    _, cookie = issue_identity(SECRET)
    assert verify_identity(cookie, "wrong-secret") is None


def test_verify_rejects_tampered_cookie():
    client_id, cookie = issue_identity(SECRET)
    ts, cid, sig = cookie.split(".")
    tampered = f"{ts}.{cid}.deadbeef"
    assert verify_identity(tampered, SECRET) is None
    # 篡改 client_id 段
    assert verify_identity(f"{ts}.{cid}other.{sig}", SECRET) is None


def test_verify_rejects_none_and_malformed():
    assert verify_identity(None, SECRET) is None
    assert verify_identity("not-a-cookie", SECRET) is None
    assert verify_identity("", SECRET) is None


def test_cookie_name_constant():
    assert IDENTITY_COOKIE_NAME == "hci_client_id"
