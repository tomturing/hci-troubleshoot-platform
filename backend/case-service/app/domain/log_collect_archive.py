"""日志服务器归档件的统一判据（P1/P2 共用）。

第一性原理：平台不持有 P1 的源包副本，P2 的出站摘要也不能证明落盘后的字节仍完整；
唯一能回答"包是否在日志服务器落盘"的是 5888 的 ``/archive_status``。该端点只回报
O(1) 可得的落盘事实（存在、体积、mtime），不回摘要——大包现算 sha256 会阻塞其它上传。
因此判据只用这些事实，不伪造字节级完整性结论。
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

# 5888 与平台未必严格对时；只拦"包在任务创建前很久就存在"的历史同名包错配。
ARCHIVE_LOOKBACK_SECONDS = 300


def judge_archive(
    task: Any,
    archive: Mapping[str, Any],
    expected_size: int | None,
) -> tuple[bool, str, int | None, datetime | None]:
    """返回 (是否确认落地, 原因码, 实际大小, 归档时间)。

    - ``archive_missing``：5888 明确回 found=false；
    - ``archive_size_invalid``：落盘体积缺失或非法；
    - ``size_mismatch``：落盘体积小于任务记录值（截断只可能变小）；
    - ``stale_archive``：包 mtime 早于任务创建太久，是上一轮的同名历史包；
    - ``verified``：存在、体积够、时间对得上。

    体积不要求相等：expected_size 可能是插件自删前的峰值观测，强求相等会把完整包
    判成失败（2026-11-10 Q2026111000243 事故）。
    """
    if not archive.get("found"):
        return False, "archive_missing", None, None

    archived_size = archive.get("size")
    if not isinstance(archived_size, int) or archived_size <= 0:
        return False, "archive_size_invalid", None, None
    if expected_size is not None and archived_size < expected_size:
        return False, "size_mismatch", archived_size, None

    mtime = archive.get("mtime")
    archived_at: datetime | None = None
    if isinstance(mtime, (int, float)):
        archived_at = datetime.fromtimestamp(int(mtime), tz=UTC)

    created_at = getattr(task, "created_at", None)
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if archived_at is not None and archived_at < created_at - timedelta(seconds=ARCHIVE_LOOKBACK_SECONDS):
            return False, "stale_archive", archived_size, archived_at

    return True, "verified", archived_size, archived_at
