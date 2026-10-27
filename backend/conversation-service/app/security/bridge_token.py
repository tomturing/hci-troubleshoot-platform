"""bridge 日志直传令牌：HMAC 签名、短期、绑定工单与实例。

第一性原理
----------
bridge 运行在客户机上，**没有浏览器 Cookie**。要让它在浏览器关闭/断网/进程长时间
无人值守时仍把桥侧日志送到平台，只能给它一个「只写日志」的凭证。

因此令牌只承载四个事实：`client_id / case_id / bridge_instance_id / exp`，
不携带任何读权限，也不允许跨工单写入。

对抗性审查
----------
- 令牌泄露 → 可伪造该工单的桥日志（污染审计）。缓解：有效期上限 12h、绑定实例、
  只允许写 `/api/bridge-logs/direct`；写入时 `user_id` 标记为 `bridge:<instance>`，
  事后可按实例批量甄别与删除。
- 令牌重放 → 权限不放大（仍是"只能写这一个工单的日志"）。
- 密钥复用 `INTERNAL_API_TOKEN` 的取舍：不新增配置项；代价是内网令牌轮换会让存量
  令牌同时失效——bridge 会在下一次 `ssh_connect` 重新取令牌，可接受。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
from typing import Any

# 令牌前缀：为将来更换算法留出版本位，解析时按前缀分派。
_TOKEN_VERSION = "v1"
# 上限 12 小时：覆盖一次完整的采集+上传，又不至于成为长期凭证。
MAX_TTL_SECONDS = 12 * 60 * 60


def _secret(secret: str | None) -> bytes:
    """返回签名密钥；空值时回落到 INTERNAL_API_TOKEN（由调用方传入）。"""
    return (secret or "").encode("utf-8")


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


def _sign(payload_b64: str, secret: str) -> str:
    return hmac.new(_secret(secret), payload_b64.encode("ascii"), hashlib.sha256).hexdigest()


def issue_bridge_log_token(
    *,
    client_id: str,
    case_id: str,
    bridge_instance_id: str,
    secret: str,
    ttl_seconds: int,
) -> tuple[str, int]:
    """签发令牌，返回 `(token, exp_unix)`。

    Args:
        client_id: 网关签名校验出的客户身份（不来自用户自报头）。
        case_id: 令牌唯一允许写入的工单。
        bridge_instance_id: 令牌绑定的 bridge 实例，禁止跨实例复用。
        secret: 签名密钥。
        ttl_seconds: 有效期，内部强制收敛到 `(0, MAX_TTL_SECONDS]`。
    """
    ttl = max(60, min(int(ttl_seconds), MAX_TTL_SECONDS))
    exp = int(time.time()) + ttl
    payload = {
        "cid": client_id,
        "case": case_id,
        "ins": bridge_instance_id,
        "exp": exp,
    }
    payload_b64 = _b64url_encode(json.dumps(payload, separators=(",", ":"), ensure_ascii=False).encode("utf-8"))
    return f"{_TOKEN_VERSION}.{payload_b64}.{_sign(payload_b64, secret)}", exp


def verify_bridge_log_token(token: str, secret: str) -> dict[str, Any] | None:
    """校验令牌并返回载荷；任何异常（格式/签名/过期）一律返回 None。

    刻意不抛异常：调用方只需区分"能用/不能用"，具体原因由调用方按 401 统一返回，
    避免把"签名错""过期""格式错"暴露成可利用的探测面。
    """
    if not token or not secret:
        return None
    parts = token.split(".")
    if len(parts) != 3 or parts[0] != _TOKEN_VERSION:
        return None
    payload_b64, signature = parts[1], parts[2]
    if not hmac.compare_digest(_sign(payload_b64, secret), signature):
        return None
    try:
        payload = json.loads(_b64url_decode(payload_b64))
    except (ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict):
        return None
    if int(payload.get("exp") or 0) <= int(time.time()):
        return None
    if not payload.get("cid") or not payload.get("case"):
        return None
    return payload
