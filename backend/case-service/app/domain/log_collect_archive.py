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
    *,
    expected_size_trustworthy: bool = True,
) -> tuple[bool, str, int | None, datetime | None]:
    """返回 (是否确认落地, 原因码, 实际大小, 归档时间)。

    - ``archive_missing``：5888 明确回 found=false；
    - ``archive_size_invalid``：落盘体积缺失或非法；
    - ``stale_archive``：包 mtime 早于任务创建太久，规范名下挂的还是上一轮的历史包。
      **瞬态**：5888 收完整包才把规范名切到新包，新包上传期间规范名仍指向旧包，
      所以"旧包 + 体积不够"首先说明的是"新包还没发布"，不是"本次包被截断"
      （2026-11-17 Q2026111700614：4.95 GB 新包上传中，核验读到 14:31 的 314 MB
      旧包，被旧顺序误判 size_mismatch，客户看到红卡时包正在落地）；
    - ``size_mismatch``：包在任务窗口内发布但体积小于**可信**记录值（截断只可能变小），
      确定性否定；
    - ``archive_size_untrusted``：体积小于记录值，但记录值来自采集过程中的观测，
      无法证明属于本次任务。同工单换模块重收时，固定路径上残留的上一批次包会被
      stat 成本次产物（Q2026112700769：旧包 4.17 GiB 压住新包 146 MiB）；
      这类结论只作待核对，不能按截断报红卡；
    - ``verified``：存在、体积够、时间对得上。

    体积不要求相等：expected_size 可能是插件自删前的峰值观测，强求相等会把完整包
    判成失败（2026-11-10 Q2026111000243 事故）。
    """
    if not archive.get("found"):
        return False, "archive_missing", None, None

    archived_size = archive.get("size")
    if not isinstance(archived_size, int) or archived_size <= 0:
        return False, "archive_size_invalid", None, None

    mtime = archive.get("mtime")
    archived_at: datetime | None = None
    if isinstance(mtime, (int, float)):
        archived_at = datetime.fromtimestamp(int(mtime), tz=UTC)

    # 时间判定必须先于体积判定：历史包几乎必然也更小，先查体积会把
    # "新包还没发布"误报成"本次包被截断"，两类结论的处置完全不同。
    created_at = getattr(task, "created_at", None)
    if created_at is not None:
        if created_at.tzinfo is None:
            created_at = created_at.replace(tzinfo=UTC)
        if archived_at is not None and archived_at < created_at - timedelta(seconds=ARCHIVE_LOOKBACK_SECONDS):
            return False, "stale_archive", archived_size, archived_at

    if expected_size is not None and archived_size < expected_size:
        # 只有「确认体积」才能证明落盘包被截断；采集过程中的观测可能来自同工单
        # 上一批次残留在固定路径上的旧包，直接判截断会把完整包误报成红卡。
        # 此时降级为待核对（archive_size_untrusted），既不红卡也不写 delivered。
        if not expected_size_trustworthy:
            return False, "archive_size_untrusted", archived_size, archived_at
        return False, "size_mismatch", archived_size, archived_at

    return True, "verified", archived_size, archived_at
