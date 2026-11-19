"""acli 插件阶段二票据：HMAC 签名、短期、只读、绑定工单。

第一性原理
----------
bridge 运行在客户机上，是不受信进程。acli_sync 第二阶段要让它用 HTTP 拉取
平台 PluginManifest 与插件包，但**绝不能**给它管理令牌或客户身份——否则
桥一旦被控，平台数据面就开了后门（收口设计 §3.3 票据模型）。

因此票据只承载两个事实：``case_id / exp``，外加固定 scope：

- 只读，且仅覆盖 ``manifest`` 与 ``package`` 两个端点；
- 绑定 case：不能用于枚举其他工单的插件（fail-closed 401）；
- TTL 上限 10 分钟：盖住一次阶段二拉取即可，泄露也无法长期复用。

范式与 ``conversation-service/app/security/bridge_token.py``、
``log_collect_evidence_service.LogCollectEvidenceTicket`` 同族：密钥从
INTERNAL_API_TOKEN 派生（不新增必填配置），验签用 ``compare_digest``。
"""

from __future__ import annotations

import hashlib
import hmac
import time

from ..config import settings

# 派生盐：与日志收集证据票据同款隔离，避免同一密钥在不同用途间裸复用。
_TICKET_SCOPE = "acli-plugin-read"


class PluginTicketError(ValueError):
    """票据非法（签名不符/过期/格式错）；调用侧统一回 401。"""


def _secret() -> bytes:
    return hashlib.sha256(f"{_TICKET_SCOPE}:{settings.INTERNAL_API_TOKEN}".encode()).digest()


def issue_plugin_ticket(case_id: str, ttl_seconds: int | None = None) -> tuple[str, int]:
    """按 case 签发短期只读票据，返回 ``(token, exp_unix)``。

    TTL 强制收敛到 ``(0, ACLI_PLUGIN_TICKET_TTL_SECONDS]``：调用方（前端）
    传来的更大值一律截断——票据时长不是不可信客户端可以协商的。
    """
    cap = max(60, int(settings.ACLI_PLUGIN_TICKET_TTL_SECONDS))
    ttl = max(60, min(int(ttl_seconds or cap), cap))
    expires_at = int(time.time()) + ttl
    signature = hmac.new(
        _secret(), f"{_TICKET_SCOPE}:{case_id}:{expires_at}".encode(), hashlib.sha256
    ).hexdigest()
    return f"{expires_at}.{signature}", expires_at


def verify_plugin_ticket(case_id: str, token: str) -> None:
    """校验票据与 case 的绑定；任何异常抛 ``PluginTicketError``（fail-closed）。"""
    expires_text, _, signature = (token or "").partition(".")
    if not expires_text.isdigit() or not signature:
        raise PluginTicketError("ticket_malformed")
    if int(expires_text) < int(time.time()):
        raise PluginTicketError("ticket_expired")
    expected = hmac.new(
        _secret(), f"{_TICKET_SCOPE}:{case_id}:{expires_text}".encode(), hashlib.sha256
    ).hexdigest()
    if not hmac.compare_digest(signature, expected):
        raise PluginTicketError("ticket_signature_mismatch")


__all__ = [
    "PluginTicketError",
    "issue_plugin_ticket",
    "verify_plugin_ticket",
]
