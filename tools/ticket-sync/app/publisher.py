"""原子发布：把解压完成的临时目录切换到正式路径。

唯一约束：work_dir 与 final_dir 必须在同一文件系统。
两者都在 <root> 下（.tmp/<id> 与 <id>），因此 rename 是原子的，
且不需要额外空间——这是用户永远看不到半成品目录的根本保证。
"""

from __future__ import annotations

import logging
import os
import shutil
import stat
import time
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
    """失败路径上的清理。

    不能只用 rmtree(ignore_errors=True)：解压产物里存在权限为 000 的文件、
    缺执行位的目录，rmtree 会在中途失败但异常被吞掉，日志却照常打印
    "已清理"，于是 .tmp 里越积越多（实测单机 588 个残留目录），
    属于"谎报成功 + 静默泄漏"。这里改为：先补权限再删，失败必须留痕。
    """
    if not work_dir.exists():
        return

    try:
        _make_removable(work_dir)
        shutil.rmtree(work_dir)
    except OSError as exc:
        logger.error("清理临时目录失败 path=%s error=%s 残留需人工或下次启动处理", work_dir, exc)
        return
    logger.info("已清理临时目录 path=%s", work_dir)


def prepare_workspace(work_dir: Path, keep: str) -> int:
    """为「可续传」准备临时目录：只保留已下载的归档，其余解压产物全部清掉。

    为什么必须清：work_dir 现在按工单号固定（不再带 job_id），上次失败留下的
    半套解压产物会和这次解出来的混在一起，publish 出去的目录就是脏的。
    为什么必须留归档：65 GiB 级的包重拉一次要几十分钟，`rsync --partial`
    只有看到上次的文件才谈得上续传。

    返回清理条目数。
    """
    if not work_dir.is_dir():
        return 0

    removed = 0
    for entry in work_dir.iterdir():
        # rsync --inplace 直接写目标文件；仍保留点开头的同名临时文件以防万一。
        if entry.name == keep or entry.name.startswith(f".{keep}."):
            continue
        try:
            if entry.is_dir():
                _make_removable(entry)
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            logger.warning("清理临时条目失败 path=%s error=%s", entry, exc)
            continue
        removed += 1
    return removed


def cleanup_stale_workspaces(tmp_root: Path, max_age_seconds: float) -> int:
    """回收 `.tmp` 下超龄的半成品，返回清理条目数。

    work_dir 按工单号固定之后，失败残留会一直留着供续传——这必须有回收阀门，
    否则一个再也不会被重试的工单会永久占着几十 GiB。
    判据用 mtime：`rsync --inplace` 持续写入会不断刷新它，
    所以"正在下载"的目录不会被误判成陈旧。
    """
    if not tmp_root.is_dir():
        return 0

    cutoff = time.time() - max_age_seconds
    removed = 0
    for entry in tmp_root.iterdir():
        try:
            info = entry.stat()
        except OSError:
            continue
        if info.st_mtime > cutoff:
            continue
        try:
            if entry.is_dir():
                _make_removable(entry)
                shutil.rmtree(entry)
            else:
                entry.unlink()
        except OSError as exc:
            logger.error("回收陈旧临时目录失败 path=%s error=%s", entry, exc)
            continue
        removed += 1
        logger.info("已回收陈旧临时目录 path=%s age_min=%.0f", entry, (time.time() - info.st_mtime) / 60)
    return removed


def _make_removable(root: Path) -> None:
    """给目录树补上属主读写执行位，使 rmtree 能够真正删掉。

    删除文件依赖父目录的 w+x，删除目录还依赖目录自身的 w+x，
    因此两类条目都要补，且目录必须先于其子项生效（os.walk 自顶向下）。
    """
    want_dir = stat.S_IRWXU
    want_file = stat.S_IRUSR | stat.S_IWUSR
    uid = os.getuid()

    def widen(path: Path, want: int) -> None:
        try:
            info = os.lstat(path)
        except OSError:
            return
        if not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
            return
        if info.st_uid != uid:
            return
        mode = stat.S_IMODE(info.st_mode)
        if mode & want == want:
            return
        try:
            os.chmod(path, mode | want)
        except OSError:
            # 补不上就交给上层的 rmtree 报错，由调用方记录残留。
            return

    widen(root, want_dir)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in dirnames:
            widen(Path(dirpath) / name, want_dir)
        for name in filenames:
            widen(Path(dirpath) / name, want_file)
