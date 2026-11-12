"""日志收集证据数据面的票据与产物判定。

第一性原理：bridge 只能上报「观测事实」，不能上报「结论」。
包质量、网络分级这类结论必须由平台按事实推导，否则一个老版本或异常
bridge 就能把 608 字节空壳包标成成功——那正是 Q2026111200624 的形态。
"""

from __future__ import annotations

import hashlib
import hmac
import time
from typing import Any

from ..config import settings
from ..domain.log_collect_display_generated import MIN_ARCHIVE_BYTES

# 采集 + 打包 + 交付可能跨小时，票据必须覆盖整趟任务生命周期；
# 仍远短于上传令牌（按天计），且绑定 task_id，泄露也只影响单个任务。
_TICKET_TTL_SECONDS = 24 * 3600
_PROBE_TICKET_TTL_SECONDS = 600
_MAX_BANDWIDTH_BYTES = 4 * 1024 * 1024


class LogCollectEvidenceTicket:
    """短期票据：绑定 ``bound_id``（task_id 或 probe_id），不依赖任何存储元数据。

    为什么不复用上传令牌：``verify_token`` 要读会话 meta 里的代际，
    而网络探测发生在采集下发之前，会话根本不存在；硬复用会全部 401。
    """

    @staticmethod
    def _secret() -> bytes:
        raw = settings.LOG_COLLECT_UPLOAD_SECRET or settings.INTERNAL_API_TOKEN
        return hashlib.sha256(f"log-collect-evidence:{raw}".encode()).digest()

    def issue(self, bound_id: str, *, probe: bool = False) -> str:
        ttl = _PROBE_TICKET_TTL_SECONDS if probe else _TICKET_TTL_SECONDS
        expires_at = int(time.time()) + ttl
        signature = hmac.new(self._secret(), f"evidence:{bound_id}:{expires_at}".encode(), hashlib.sha256).hexdigest()
        return f"{expires_at}.{signature}"

    def verify(self, bound_id: str, token: str) -> bool:
        expires_text, _, signature = token.partition(".")
        if not expires_text.isdigit() or not signature:
            return False
        if int(expires_text) < int(time.time()):
            return False
        expected = hmac.new(
            self._secret(),
            f"evidence:{bound_id}:{expires_text}".encode(),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(signature, expected)


def derive_manifest_quality(
    *,
    archive_size: int | None,
    entry_count: int,
    entry_total_bytes: int,
    readable: bool,
) -> tuple[str, str]:
    """从中央目录事实推导产物质量，返回 ``(quality, reason)``。

    只读 zip 中央目录，绝不用 ``unzip -t`` 判完整性：客户包全条目加密，
    密码不对时 ``unzip -t`` 会把一个完全正常的包报成损坏。
    """
    if not readable:
        return "invalid", "unreadable"
    if archive_size is None or archive_size <= 0:
        return "invalid", "missing"
    if archive_size < MIN_ARCHIVE_BYTES:
        return "invalid", "too_small"
    if entry_count <= 0:
        return "invalid", "empty_archive"
    if entry_total_bytes < MIN_ARCHIVE_BYTES and entry_count <= 2:
        return "invalid", "metadata_only"
    if entry_total_bytes < MIN_ARCHIVE_BYTES:
        # 包够大但条目总字节很小（填充/伪装包）不能判成功，也不能武断判失败。
        return "suspect", "content_too_small"
    if entry_count < 3:
        return "suspect", "entry_count_low"
    return "ok", "ok"


def probe_grade(*, reachable: bool, loss_rate: Any, rtt_avg_ms: Any, jitter_ms: Any) -> str:
    """网络分级：事实 → 档位，客户端不上传 grade 结论。

    阈值按「能否稳定承载 GiB 级包」划分，而非通用网页体验：
    丢包 >2% 或平均 RTT >200ms 直接判较差，抖动 >80ms 判一般。
    """
    if not reachable:
        return "poor"

    def as_float(value: Any) -> float | None:
        try:
            return float(value) if value is not None else None
        except (TypeError, ValueError):
            return None

    loss = as_float(loss_rate)
    rtt = as_float(rtt_avg_ms)
    jitter = as_float(jitter_ms)
    if (loss is not None and loss > 0.02) or (rtt is not None and rtt > 200):
        return "poor"
    if (loss is not None and loss > 0.005) or (rtt is not None and rtt > 80) or (jitter is not None and jitter > 80):
        return "fair"
    if loss is None and rtt is None and jitter is None:
        return "unknown"
    return "good"


MAX_BANDWIDTH_BYTES = _MAX_BANDWIDTH_BYTES
