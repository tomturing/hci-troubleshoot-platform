"""ticket-sync 的本地目录保留期与容量维护。

设计取舍（2026-10-31 重构）：
- 「同步完成时间」与「产物体积」在发布时就已经是确定事实，因此写进
  `<工单目录>/.sync-meta.json`，而不是每天重新扫描文件系统。
- 目录 mtime 不可靠：它只反映"直接子项最后一次增删"，DSH 在产物里生成报告、
  人工 touch 都会改写它，会让保留期误判。
- os.walk + lstat 在冷缓存下极慢：实测 550 GiB / 44 万文件耗时约 3.5 分钟，
  而这段是同步代码，会把整个事件循环钉死（同期 HTTP 请求全部排队）。
  现在体积优先取 meta，缺失才扫一次并回填缓存。
"""

from __future__ import annotations

import json
import logging
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_TICKET_DIR = re.compile(r"^Q\d{13}$")
_TRASH_DIR = re.compile(r"^Q\d{13}-\d{8}-\d{6}$")

# 发布时写入的同步元数据。文件名以点开头，DSH 的文件选择器不会展示它。
SYNC_META_NAME = ".sync-meta.json"

# 容量淘汰优先级：数值越小越先删。
# 非日志载荷（只有转储、没有 log_list.json）留着没有任何诊断价值，
# 却往往是 60 GiB 量级的大块头，必须排在正常日志工单之前被淘汰。
_RANK_NO_LOGS = 0
_RANK_HAS_LOGS = 1


def read_sync_meta(ticket_dir: Path) -> dict[str, Any]:
    """读取同步元数据；不存在或损坏一律返回空 dict（调用方按旧逻辑回退）。"""
    try:
        raw = (ticket_dir / SYNC_META_NAME).read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}


def write_sync_meta(ticket_dir: Path, **fields: Any) -> None:
    """合并写入同步元数据（原子替换，避免半截 JSON）。

    只覆盖传入的键，保留已有内容——发布后补写尺寸时不会冲掉 completedAt。
    """
    data = read_sync_meta(ticket_dir)
    data.update(fields)
    target = ticket_dir / SYNC_META_NAME
    tmp = target.with_name(f"{SYNC_META_NAME}.tmp")
    try:
        tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
    except OSError as exc:
        # 元数据只影响清理精度，写失败不能连累同步结果。
        logger.warning("写同步元数据失败 path=%s error=%s", target, exc)
        tmp.unlink(missing_ok=True)


def enforce_capacity(
    root: Path,
    max_total_bytes: int,
    min_keep_days: int,
    now: datetime,
    active_ticket_ids: set[str] | frozenset[str] = frozenset(),
) -> tuple[int, int]:
    """产物总量超过上限时淘汰，返回 (删除数, 释放字节)。

    排序规则：先按"有没有日志"（非日志载荷先删），再按同步时间最旧优先。
    只按最旧排序会让一个 65 GiB 的 dump 工单挤掉多个真正可用的日志工单。
    """
    if not root.is_dir():
        return 0, 0

    candidates: list[tuple[int, float, Path, int]] = []
    # total 统计磁盘上真实占用（含活跃与受保护工单），否则会低估压力、
    # 把"删无可删"的告警吞掉。
    total = 0
    for entry in root.iterdir():
        if not entry.is_dir() or entry.is_symlink() or _TICKET_DIR.fullmatch(entry.name) is None:
            continue
        try:
            mtime = entry.stat().st_mtime
        except OSError:
            continue
        size = _cached_bytes(entry)
        total += size
        if entry.name in active_ticket_ids:
            continue
        synced_at = _synced_at(entry, mtime)
        age_days = (now.timestamp() - synced_at) / 86400
        if age_days < min_keep_days:
            # 刚同步进来的工单不参与淘汰，宁可超限告警也不误删用户在用的数据。
            continue
        candidates.append((_payload_rank(entry), synced_at, entry, size))
    if total <= max_total_bytes:
        return 0, 0

    removed = 0
    freed = 0
    for _, _, entry, size in sorted(candidates, key=lambda item: (item[0], item[1])):
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
        synced_at = _synced_at(entry, mtime)
        if synced_at >= cutoff:
            continue
        try:
            shutil.rmtree(entry)
        except OSError as exc:
            logger.error("清理过期工单目录失败 ticket=%s error=%s", entry.name, exc)
            continue
        removed += 1
        logger.info(
            "清理过期本地目录 path=%s age_days=%.1f",
            entry,
            (now.timestamp() - synced_at) / 86400,
        )
    return removed


def has_usable_logs(root: Path, max_depth: int = 2) -> bool:
    """产物里是否有可用于诊断的日志。

    判据对齐采集端约定：存在 `log_list.json` 或任意 `sp_*` 条目。
    允许下探 max_depth 层，因为实测产物会再套一层与工单号同名的目录
    （`Q2026102700323/Q2026102700323/1.rar`）；但只对工单号形态的目录下探，
    不进入 sp_* 子树——那是几十万文件。
    """
    try:
        entries = list(root.iterdir())
    except OSError:
        return False

    for entry in entries:
        name = entry.name.lower()
        if name == "log_list.json" or name.startswith("sp_"):
            return True

    if max_depth <= 0:
        return False
    for entry in entries:
        if entry.is_dir() and _TICKET_DIR.fullmatch(entry.name):
            if has_usable_logs(entry, max_depth - 1):
                return True
    return False


def _synced_at(ticket_dir: Path, fallback_mtime: float) -> float:
    """产物何时同步完成：优先显式标记，回退目录 mtime。"""
    value = read_sync_meta(ticket_dir).get("completedAt")
    if isinstance(value, (int, float)):
        return float(value)
    return fallback_mtime


def _payload_rank(ticket_dir: Path) -> int:
    """淘汰优先级。历史目录无 meta 时按"有日志"对待，避免误删可用产物。"""
    return _RANK_NO_LOGS if read_sync_meta(ticket_dir).get("hasLogs") is False else _RANK_HAS_LOGS


def _cached_bytes(ticket_dir: Path) -> int:
    """产物字节数：优先读 meta，缺失时扫一次并回填。

    历史目录（本次改造之前发布的）没有 meta，不补写的话每天容量检查都要
    重新走一遍全盘遍历。

    注意回填后必须把目录 mtime 恢复原值：新增 `.sync-meta.json` 会把目录
    mtime 刷成当前时间，而历史目录没有 completedAt、保留期仍回退用 mtime，
    刷掉就等于给它无限续命，永远清不掉。
    """
    value = read_sync_meta(ticket_dir).get("bytes")
    if isinstance(value, int) and value >= 0:
        return value
    size = _tree_bytes(ticket_dir)
    try:
        info = ticket_dir.stat()
    except OSError:
        info = None
    write_sync_meta(ticket_dir, bytes=size)
    if info is not None:
        try:
            os.utime(ticket_dir, (info.st_atime, info.st_mtime))
        except OSError:
            logger.warning("恢复目录 mtime 失败 path=%s", ticket_dir)
    return size


def _tree_bytes(root: Path) -> int:
    """统计目录真实占用字节数，不跟随符号链接。

    用 os.scandir 而非 os.walk + os.lstat：scandir 复用目录项里的类型与尺寸
    信息，44 万文件时省掉一次多余的系统调用。
    """
    total = 0
    stack = [str(root)]
    while stack:
        current = stack.pop()
        try:
            with os.scandir(current) as iterator:
                for entry in iterator:
                    try:
                        if entry.is_dir(follow_symlinks=False):
                            stack.append(entry.path)
                        elif entry.is_file(follow_symlinks=False):
                            total += entry.stat(follow_symlinks=False).st_size
                    except OSError:
                        continue
        except OSError:
            continue
    return total
