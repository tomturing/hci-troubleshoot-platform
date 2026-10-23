"""原子发布：把解压完成的临时目录切换到正式路径。

唯一约束：work_dir 与 final_dir 必须在同一文件系统。
两者都在 <root> 下（.tmp/<id>-<job> 与 <id>），因此 rename 是原子的，
且不需要额外空间——这是用户永远看不到半成品目录的根本保证。
"""

from __future__ import annotations

import logging
import os
import shutil
from datetime import datetime
from pathlib import Path

logger = logging.getLogger(__name__)


def publish(work_dir: Path, final_dir: Path, trash_dir: Path) -> Path | None:
    """把 work_dir 发布为 final_dir。

    返回被替换的旧版本路径（若存在），便于调用方记录与清理。
    """
    replaced: Path | None = None

    if final_dir.exists() or final_dir.is_symlink():
        trash_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        replaced = trash_dir / f"{final_dir.name}-{stamp}"
        # 先移走旧版本，保证随后的 rename 目标为空。目录非空时 rename 会失败。
        final_dir.rename(replaced)
        logger.info("旧版本已移入回收站 from=%s to=%s", final_dir, replaced)

    # 同一文件系统内的 rename：原子替换。
    os.rename(work_dir, final_dir)
    logger.info("发布完成 path=%s", final_dir)
    return replaced


def prune_trash(trash_dir: Path, keep: int = 1) -> int:
    """只保留最近 keep 个旧版本，返回删除数量。

    旧版本保留一份的意义是允许人工回退；超过一份只会白占磁盘。
    """
    if not trash_dir.is_dir():
        return 0

    entries = sorted(
        (entry for entry in trash_dir.iterdir() if entry.is_dir()),
        key=lambda entry: entry.stat().st_mtime,
        reverse=True,
    )

    removed = 0
    for entry in entries[keep:]:
        shutil.rmtree(entry, ignore_errors=True)
        removed += 1
        logger.info("清理回收站 path=%s", entry)
    return removed


def cleanup_workspace(work_dir: Path) -> None:
    """失败路径上的清理。尽力而为，不掩盖原始异常。"""
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
        logger.info("已清理临时目录 path=%s", work_dir)
