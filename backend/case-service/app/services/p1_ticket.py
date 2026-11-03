"""P1 直传票据：让节点 acli 携带平台签发的短期凭据上传日志服务器。

背景（2026-11-03 现场 Q2026110300278）：
    5888 在 2026-10-31 启用 ``ACLI_UPLOAD_TOKEN`` 后，节点 acli 的直传链路
    （P1）在 ``/init_upload`` 就被 401 打回——acli 只会原样请求 ``-u`` 给出的
    地址，自身不带任何认证头；而平台代传（P2）用的是服务端 Bearer，所以
    "同一台 5888、同一时刻，带 token 的通道通、不带的全 401"。

    转折点：acli 自 1.0.0 起支持 ``-L|--url-header``（help 原文：
    "URL特定请求头，优先级大于--header，格式：index:Header-Name:value"），
    bridge 可以把它想要的头注入进去。因此 P1 不需要改 acli，缺的只是
    "平台签发、5888 验签"的凭据本身就够了。

设计取舍（第一性原理，不套惯例）：
    1. 凭据必须 per-task：泄露一个工单的票据不能拿去伪造别的工单；
    2. 凭据必须短时效：超过采集窗口即失效，不给长期驻留的复用空间；
    3. 凭据必须自描述：5888 只验签、不回查平台库，避免两侧状态耦合
       （回查会把"5888 能不能访问平台"变成新的单点故障）；
    4. 用 HMAC-SHA256 而不是非对称签名：两者在"密钥泄露即可伪造"这一点上
       没有差别，而 5888 与平台都不在客户环境；非对称换来的"验签方不持
       签发密钥"收益有限，代价却是给 case-service 镜像新增密码学依赖
       （实测当前镜像 ``import cryptography`` 失败），收益/风险不划算。
    5. 票据走 HTTP 头而不是 URL：URL 会进 ``ps``、进 bridge 的 command 日志、
       进 ``detail`` 字段，凭证一旦进这些地方就等于写进了排障材料。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

# 票据前缀。5888 据此把"P1 票据"与"全局 Bearer"分流：
#   有前缀 → 走票据验签；无前缀 → 走原有的全局 token 比较（P2 与手工上传页）。
# 前缀里带版本号，后续格式变更不需要靠猜。
TICKET_PREFIX = "p1.v1."

# 允许的时钟漂移（秒）。平台与 5888 是两台独立机器，NTP 未强制对齐时
# 严格比较 exp 会把"刚签发就过期"变成随机故障。
DEFAULT_CLOCK_SKEW_SECONDS = 300


class P1TicketError(Exception):
    """票据不可用。``code`` 供上层映射成稳定的错误码/日志字段。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class P1TicketClaims:
    """验签通过后的票据内容。"""

    case_id: str
    filename: str
    expires_at: int
    ticket_id: str
    issued_at: int


def _b64url_encode(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


def _b64url_decode(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class P1TicketService:
    """签发与校验 P1 直传票据。

    ``secret`` 为空表示未启用：此时 :attr:`enabled` 为 False，上层必须让 P1
    降级为 P2，而不是把无凭据的直传放出去（那等于让 5888 再吃一次 401）。
    """

    def __init__(
        self,
        secret: str,
        *,
        ttl_seconds: int = 12 * 3600,
        clock_skew_seconds: int = DEFAULT_CLOCK_SKEW_SECONDS,
    ) -> None:
        self._secret = (secret or "").strip().encode("utf-8")
        self.ttl_seconds = max(int(ttl_seconds), 60)
        self.clock_skew_seconds = max(int(clock_skew_seconds), 0)

    @property
    def enabled(self) -> bool:
        return bool(self._secret)

    # ── 签发 ────────────────────────────────────────────────────────────────

    def issue(
        self,
        case_id: str,
        filename: str,
        *,
        now: int | None = None,
        ttl_seconds: int | None = None,
    ) -> str:
        """签发一张绑定「工单 + 产物名」的票据。

        ``filename`` 必须显式给出而不是由 case_id 推导：产物名是平台与 5888
        之间唯一的对账键，两边各推一次就可能因为后缀/大小写不一致而对不上。
        """
        if not self.enabled:
            raise P1TicketError("disabled", "未配置 P1 票据密钥，不能签发直传票据")
        case_id = (case_id or "").strip()
        filename = (filename or "").strip()
        if not case_id or not filename:
            raise P1TicketError("invalid_input", "签发票据需要 case_id 与 filename")

        issued_at = int(now if now is not None else time.time())
        expires_at = issued_at + int(ttl_seconds or self.ttl_seconds)
        payload = {
            "v": 1,
            "cid": case_id,
            "fn": filename,
            "iat": issued_at,
            "exp": expires_at,
            "jti": uuid.uuid4().hex,
        }
        encoded = _b64url_encode(
            json.dumps(payload, separators=(",", ":"), sort_keys=True).encode("utf-8")
        )
        signing_input = f"{TICKET_PREFIX}{encoded}"
        signature = hmac.new(self._secret, signing_input.encode("ascii"), hashlib.sha256).hexdigest()
        return f"{signing_input}.{signature}"

    # ── 校验 ────────────────────────────────────────────────────────────────

    def verify(
        self,
        token: str,
        *,
        filename: str | None = None,
        now: int | None = None,
    ) -> P1TicketClaims:
        """验签并返回票据内容；任何不满足都抛 :class:`P1TicketError`。

        ``filename`` 给出时额外校验票据与请求的产物名一致——这是防止
        "拿 A 工单的票据传 B 的包"的唯一有效约束（大小在签发时还未知）。
        """
        if not self.enabled:
            raise P1TicketError("disabled", "未配置 P1 票据密钥")
        raw = (token or "").strip()
        if not raw:
            raise P1TicketError("empty", "缺少 P1 票据")
        if not raw.startswith(TICKET_PREFIX):
            raise P1TicketError("not_ticket", "不是 P1 票据格式")

        body = raw[len(TICKET_PREFIX):]
        encoded, _, signature = body.rpartition(".")
        if not encoded or not signature:
            raise P1TicketError("malformed", "票据结构不完整")

        signing_input = f"{TICKET_PREFIX}{encoded}"
        expected = hmac.new(
            self._secret, signing_input.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise P1TicketError("bad_signature", "票据签名不匹配")

        try:
            payload: dict[str, Any] = json.loads(_b64url_decode(encoded))
        except (ValueError, json.JSONDecodeError) as exc:  # pragma: no cover - 防御性
            raise P1TicketError("malformed", f"票据载荷无法解析：{exc}") from exc

        if int(payload.get("v") or 0) != 1:
            raise P1TicketError("unsupported_version", "票据版本不受支持")

        expires_at = int(payload.get("exp") or 0)
        current = int(now if now is not None else time.time())
        # 允许 clock_skew：exp 之后再宽限一小段时间，避免两台机器的时钟差
        # 把"本来有效"的票据判死（排障成本远高于那几分钟的风险）。
        if current > expires_at + self.clock_skew_seconds:
            raise P1TicketError("expired", "票据已过期")

        claimed_filename = str(payload.get("fn") or "")
        if filename is not None and claimed_filename != filename:
            raise P1TicketError(
                "filename_mismatch",
                f"票据绑定的产物名 {claimed_filename} 与请求的 {filename} 不一致",
            )

        case_id = str(payload.get("cid") or "")
        if not case_id:
            raise P1TicketError("malformed", "票据缺少 case_id")

        return P1TicketClaims(
            case_id=case_id,
            filename=claimed_filename,
            expires_at=expires_at,
            ticket_id=str(payload.get("jti") or ""),
            issued_at=int(payload.get("iat") or 0),
        )


def archive_filename_for(case_id: str) -> str:
    """平台与 5888 约定的产物名：工单号 + ``.zip``。

    5888 自己的文件名校验也是这个形状（``Q`` + 13 位数字 + ``.zip``），
    所以这是两侧唯一的公共约定，任何一方改动都会导致对账落空。
    """
    return f"{case_id}.zip"
