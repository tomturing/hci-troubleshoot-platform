"""P1 直传票据：签发/验签的行为边界。

这些用例锁住三件事：
1. 票据只对**它绑定的那个**产物名有效（换包必须被拒）；
2. 过期与时钟漂移的处理是确定的（不依赖机器时钟状态）；
3. 任何"格式像票据但不是"的输入都不会退化成"放行"。
"""

from __future__ import annotations

import time

import pytest
from app.services.p1_ticket import (
    TICKET_PREFIX,
    P1TicketError,
    P1TicketService,
    archive_filename_for,
)

SECRET = "0" * 64
CASE_ID = "Q2026110300278"
FILENAME = "Q2026110300278.zip"


def make_service(**overrides) -> P1TicketService:
    kwargs = {"ttl_seconds": 3600, "clock_skew_seconds": 300}
    kwargs.update(overrides)
    return P1TicketService(SECRET, **kwargs)


def test_issue_then_verify_roundtrip() -> None:
    service = make_service()
    now = 1_800_000_000
    token = service.issue(CASE_ID, FILENAME, now=now)

    assert token.startswith(TICKET_PREFIX)
    claims = service.verify(token, filename=FILENAME, now=now + 10)
    assert claims.case_id == CASE_ID
    assert claims.filename == FILENAME
    assert claims.expires_at == now + 3600
    assert claims.issued_at == now
    assert len(claims.ticket_id) == 32


def test_token_is_unique_per_issue() -> None:
    """同一工单连续签发必须是不同票据（jti 随机），否则无法区分重放。"""

    service = make_service()
    first = service.issue(CASE_ID, FILENAME, now=1_800_000_000)
    second = service.issue(CASE_ID, FILENAME, now=1_800_000_000)
    assert first != second


def test_verify_rejects_other_filename() -> None:
    """票据绑定产物名：拿 A 工单的票据传 B 的包必须失败。"""

    service = make_service()
    token = service.issue(CASE_ID, FILENAME, now=1_800_000_000)
    with pytest.raises(P1TicketError) as excinfo:
        service.verify(token, filename="Q2026110300999.zip", now=1_800_000_100)
    assert excinfo.value.code == "filename_mismatch"


def test_verify_without_filename_skips_binding_check() -> None:
    """不传 filename 时不校验产物名（供 5888 的归档查询等只需验签的场景）。"""

    service = make_service()
    token = service.issue(CASE_ID, FILENAME, now=1_800_000_000)
    claims = service.verify(token, now=1_800_000_100)
    assert claims.filename == FILENAME


def test_verify_rejects_tampered_signature() -> None:
    service = make_service()
    token = service.issue(CASE_ID, FILENAME, now=1_800_000_000)
    tampered = token[:-4] + ("aaaa" if not token.endswith("aaaa") else "bbbb")
    with pytest.raises(P1TicketError) as excinfo:
        service.verify(tampered, now=1_800_000_100)
    assert excinfo.value.code == "bad_signature"


def test_verify_rejects_tampered_payload() -> None:
    """改载荷而不改签名必须失败——签名覆盖前缀+载荷整体。"""

    service = make_service()
    token = service.issue(CASE_ID, FILENAME, now=1_800_000_000)
    encoded = token[len(TICKET_PREFIX):].rpartition(".")[0]
    swapped = f"{TICKET_PREFIX}{'A' * len(encoded)}.{token.rpartition('.')[2]}"
    with pytest.raises(P1TicketError) as excinfo:
        service.verify(swapped, now=1_800_000_100)
    assert excinfo.value.code in {"bad_signature", "malformed"}


def test_verify_rejects_expired_token_beyond_skew() -> None:
    service = make_service(ttl_seconds=60, clock_skew_seconds=300)
    token = service.issue(CASE_ID, FILENAME, now=1_800_000_000)
    # 过期后仍在宽限窗口内：允许（两台机器时钟差 5 分钟内是常态）
    service.verify(token, now=1_800_000_060 + 299)
    # 超过宽限即拒绝
    with pytest.raises(P1TicketError) as excinfo:
        service.verify(token, now=1_800_000_060 + 301)
    assert excinfo.value.code == "expired"


def test_verify_rejects_global_bearer_token() -> None:
    """全局 Bearer 不是票据：不能因为"看起来像 token"就放行。"""

    service = make_service()
    with pytest.raises(P1TicketError) as excinfo:
        service.verify("a" * 64, now=1_800_000_000)
    assert excinfo.value.code == "not_ticket"


@pytest.mark.parametrize("token", ["", "   ", f"{TICKET_PREFIX}", f"{TICKET_PREFIX}onlypayload"])
def test_verify_rejects_malformed(token: str) -> None:
    service = make_service()
    with pytest.raises(P1TicketError) as excinfo:
        service.verify(token, now=1_800_000_000)
    assert excinfo.value.code in {"empty", "malformed"}


def test_verify_rejects_token_signed_by_other_secret() -> None:
    signer = P1TicketService("f" * 64, ttl_seconds=3600)
    verifier = make_service()
    token = signer.issue(CASE_ID, FILENAME, now=1_800_000_000)
    with pytest.raises(P1TicketError) as excinfo:
        verifier.verify(token, now=1_800_000_100)
    assert excinfo.value.code == "bad_signature"


def test_disabled_service_refuses_both_directions() -> None:
    """未配置密钥时必须整体停用：签发报错，验签也报错，绝不静默放行。"""

    service = P1TicketService("", ttl_seconds=3600)
    assert service.enabled is False
    with pytest.raises(P1TicketError) as issue_err:
        service.issue(CASE_ID, FILENAME)
    assert issue_err.value.code == "disabled"
    with pytest.raises(P1TicketError) as verify_err:
        service.verify(f"{TICKET_PREFIX}x.y", now=int(time.time()))
    assert verify_err.value.code == "disabled"


def test_issue_requires_both_inputs() -> None:
    service = make_service()
    with pytest.raises(P1TicketError) as excinfo:
        service.issue("", FILENAME)
    assert excinfo.value.code == "invalid_input"


def test_archive_filename_matches_5888_contract() -> None:
    """产物名是两侧唯一的对账键，形状必须与 5888 的文件名校验一致。"""

    assert archive_filename_for("Q2026110300278") == "Q2026110300278.zip"
