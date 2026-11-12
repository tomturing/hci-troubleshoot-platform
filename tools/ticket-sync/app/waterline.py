"""写盘前的磁盘水位闸门。

独立成模块的唯一理由：拉取（fetcher）、解压（extractor）、受理（main）
三处都要在**动手之前**做同一件事，而分散实现必然漂移——一处查 60 GiB、
另一处查 250 GiB，事故复盘时无法解释"为什么这里拦住了那里没拦住"。

设计约束（第一性原理）：
- 唯一可信的判据是 `shutil.disk_usage().free`。产物记账、保留天数都是代理
  指标，会漂移也会漏算（`.tmp` 就在记账之外）。剩余空间是唯一真值。
- 判断必须发生在写入之前。写满之后再检查只能得到"失败"，得不到"避免"，
  而且 2026-11-12 的教训是：整机被 kubelet 驱逐时，清理逻辑连跑的机会都没有。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .models import ErrorCode, TicketError

logger = logging.getLogger(__name__)


def free_bytes(root: Path) -> int | None:
    """返回 root 所在文件系统的剩余字节；取不到时返回 None。"""
    try:
        return shutil.disk_usage(root).free
    except OSError as exc:
        logger.warning("读取磁盘余量失败 root=%s error=%s", root, exc)
        return None


def ensure_waterline(
    root: Path,
    min_free_bytes: int,
    *,
    stage: str,
    ticket_id: str = "",
    job_id: str = "",
) -> None:
    """剩余空间低于熔断阈值时抛 `low-disk`，拒绝写入。

    取不到磁盘信息时**放行**：这里拦的是"确定写不下"，不是"可能写不下"。
    探测失败就拦住正常任务，代价比偶尔放过一次大得多。

    异常里的 message 是给用户看的结论（带具体数字），detail 是给排障看的
    依据（阶段、阈值、工单、任务号）——前端只渲染 message。
    """
    free = free_bytes(root)
    if free is None:
        return
    if free >= min_free_bytes:
        return

    raise TicketError(
        ErrorCode.LOW_DISK,
        (
            f"磁盘剩余 {free / 1024**3:.1f} GiB，低于安全水位 "
            f"{min_free_bytes / 1024**3:.0f} GiB，已暂停写入（自动清理执行中，稍后重试）"
        ),
        f"stage={stage} free={free} threshold={min_free_bytes} "
        f"ticket={ticket_id or '-'} job={job_id or '-'} root={root}",
    )
