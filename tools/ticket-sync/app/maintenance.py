"""ticket-sync 的本地目录保留期维护。"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TICKET_DIR = re.compile(r"^Q\d{13}$")
_TRASH_DIR = re.compile(r"^Q\d{13}-\d{8}-\d{6}$")


def cleanup_expired(
    root: Path,
    now: datetime,
    retention_days: int,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
) -> int:
    """删除超过保留期的已发布工单目录，返回删除数量。

    只处理符合工单号形态的直接子目录，以及服务生成的 `.trash` 旧版本目录。
    `.tmp` 和其他宿主机目录不会被触碰。正在同步的工单始终跳过，避免清理任务
    与发布流程互相踩踏。
    """
    cutoff = now.timestamp() - retention_days * 24 * 60 * 60
    removed = 0
    if not root.is_dir():
        return removed

    entries = [(entry, _TICKET_DIR) for entry in root.iterdir()]
    trash = root / ".trash"
    if trash.is_dir():
        entries.extend((entry, _TRASH_DIR) for entry in trash.iterdir())

    for entry, pattern in entries:
        if not entry.is_dir() or entry.is_symlink() or not pattern.fullmatch(entry.name):
            continue
        if entry.name in active_ticket_ids:
            logger.info("保留正在同步的工单目录 ticket=%s", entry.name)
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError as exc:
            logger.warning("读取工单目录时间失败 path=%s error=%s", entry, exc)
            continue
        if mtime >= cutoff:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.error("清理过期工单目录失败 ticket=%s error=%s", entry.name, exc)
            continue
        removed += 1
        logger.info("清理过期本地目录 path=%s age_days=%.1f", entry, (now.timestamp() - mtime) / 86400)
    return removed
