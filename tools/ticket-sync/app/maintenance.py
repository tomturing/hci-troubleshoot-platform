"""ticket-sync 的本地目录保留期维护。"""

from __future__ import annotations

import logging
import os
import re
import shutil
import stat
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)

_TICKET_DIR = re.compile(r"^Q\d{13}$")
_TRASH_DIR = re.compile(r"^Q\d{13}-\d{8}-\d{6}$")


def enforce_capacity(
    root: Path,
    max_total_bytes: int,
    min_keep_days: int,
    now: datetime,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[int, int]:
    """产物总量超过上限时按"最旧优先"淘汰，返回 (删除数, 释放字节)。

    为什么保留期之外还要总量控制：保留期是时间维度，容量是空间维度。
    实测客户一天推来 5 个 50~65 GiB 的工单，7 天保留期足够把 1.2T 的盘填满；
    而 min_free_bytes 只是拒绝新任务，不会自愈——盘满之后所有工单都同步不了。
    """
    if not root.is_dir():
        return 0, 0

    candidates: list[tuple[float, Path, int]] = []
    # total 统计的是磁盘上真实占用（含活跃与受保护工单），
    # 否则会低估实际压力、把"删无可删"的告警吞掉。
    total = 0
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or _TICKET_DIR.fullmatch(entry.name) is None:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        size = _tree_bytes(entry)
        total += size
        if entry.name in active_ticket_ids:
            continue
        age_days = (now.timestamp() - mtime) / 86400
        if age_days < min_keep_days:
            # 刚同步进来的工单不参与淘汰，宁可超限告警也不误删用户在用的数据。
            continue
        candidates.append((mtime, entry, size))
    if total <= max_total_bytes:
        return 0, 0

    removed = 0
    freed = 0
    for _, entry, size in sorted(candidates):  # 最旧优先
        if total <= max_total_bytes:
            break
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.error("容量淘汰删除失败 ticket=%s error=%s", entry.name, exc)
            continue
        total -= size
        removed += 1
        freed += size
        logger.warning(
            "容量淘汰 ticket=%s size=%.1fGiB 剩余总量=%.1fGiB 上限=%.1fGiB",
            entry.name,
            size / 1024**3,
            total / 1024**3,
            max_total_bytes / 1024**3,
        )

    if total > max_total_bytes:
        logger.error(
            "容量淘汰后仍超限 total=%.1fGiB 上限=%.1fGiB 可用工单均未满 %d 天",
            total / 1024**3,
            max_total_bytes / 1024**3,
            min_keep_days,
        )
    return removed, freed


def _tree_bytes(root: Path) -> int:
    """统计目录真实占用字节数，不跟随符号链接。"""
    total = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            try:
                info = os.lstat(os.path.join(dirpath, name))
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            total += info.st_size
    return total


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
