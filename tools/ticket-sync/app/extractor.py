"""分层解压：zip（ZipCrypto）→ tar.gz → …，每层都做安全与配额审查。

设计取舍：
- 解压交给系统的 unzip / tar（C 实现），Python 只做编排与审计。
  理由：ZipCrypto 需要密码，Python 标准库 zipfile 不支持；大文件流式解压
  在子进程里完成，不会把内存打爆。
- 每解一层立即审查，而不是全部解完再查。否则一个恶意归档可以在被拦截前
  先把磁盘写满。
- 审查包含四项：逃逸符号链接、累计字节、文件数、磁盘剩余。
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import stat
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .models import ErrorCode, ExtractStats, TicketError

logger = logging.getLogger(__name__)

# 顺序必须长后缀在前：.tar.gz 先于 .gz 判断，否则会得到错误的去后缀结果。
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar", ".zip")


async def extract(
    ticket_id: str,
    archive: Path,
    work_dir: Path,
    cfg: Config,
    job_id: str,
    on_progress: Callable[[ExtractStats], None] | None = None,
) -> ExtractStats:
    """把 archive 解压到 work_dir，返回统计。

    work_dir 必须是调用方新建的空目录；成功后由调用方负责原子发布。
    """
    stats = ExtractStats()

    # 第一层：工单 zip 直接解到 work_dir 根，不额外套目录。
    # 实测归档根就是 log_list.json，套一层会让用户在选择器里多点一次。
    logger.info("ticket=%s job=%s phase=extracting depth=1 开始解压顶层归档", ticket_id, job_id)
    await _unzip(archive, work_dir, cfg.archive_password)
    archive.unlink(missing_ok=True)  # 归档不再需要，先删掉腾空间
    stats.archives += 1
    stats.depth = 1
    _audit(work_dir, cfg, stats)
    _notify(on_progress, stats)

    depth = 1
    while True:
        pending = _find_archives(work_dir)
        if not pending:
            break
        if depth >= cfg.max_depth:
            names = ", ".join(p.name for p in pending[:5])
            raise TicketError(
                ErrorCode.DEPTH_EXCEEDED,
                f"归档嵌套超过 {cfg.max_depth} 层",
                f"depth={depth} remaining={names}",
            )

        depth += 1
        for nested in pending:
            target = _target_for(nested)
            logger.info(
                "ticket=%s job=%s depth=%d 解压 %s",
                ticket_id,
                job_id,
                depth,
                nested.relative_to(work_dir),
            )
            await _extract_any(nested, target, cfg.archive_password)
            nested.unlink(missing_ok=True)
            stats.archives += 1

        stats.depth = depth
        _audit(work_dir, cfg, stats)
        _notify(on_progress, stats)

    logger.info(
        "ticket=%s job=%s phase=extracting 完成 depth=%d files=%d bytes=%d archives=%d 清理链接=%d",
        ticket_id,
        job_id,
        stats.depth,
        stats.files,
        stats.bytes,
        stats.archives,
        stats.symlinks_removed,
    )
    return stats


def _notify(callback: Callable[[ExtractStats], None] | None, stats: ExtractStats) -> None:
    if callback is not None:
        callback(stats)


def _find_archives(root: Path) -> list[Path]:
    """扫描待处理的归档。不跟随符号链接目录，避免遍历到根目录之外。"""
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            if name.lower().endswith(ARCHIVE_SUFFIXES):
                found.append(Path(path))
    return sorted(found)


def _target_for(archive: Path) -> Path:
    """归档解压到与它同名的目录，去掉归档后缀。

    sp_10.46.16.71.tgz          -> sp_10.46.16.71/
    cmd-output/raid.tar.gz      -> cmd-output/raid/
    LOG_cgroup.txt.zip          -> LOG_cgroup.txt.d/   （同名文件已存在时）

    第三种情况在真实工单里普遍存在：HCI 会把日志原文件和它的压缩版一起打包，
    LOG_cgroup.txt 与 LOG_cgroup.txt.zip 并存。若直接解到 LOG_cgroup.txt/
    会与同名文件冲突，因此退让为 .d 目录，绝不覆盖原始日志。
    """
    name = archive.name
    base = archive
    for suffix in ARCHIVE_SUFFIXES:
        if name.lower().endswith(suffix):
            base = archive.with_name(name[: -len(suffix)])
            break

    if not base.exists() or base.is_dir():
        return base

    # base 已被同名文件占用：加 .d 后缀。若 .d 也被文件占用则继续编号。
    candidate = base.with_name(base.name + ".d")
    index = 2
    while candidate.exists() and not candidate.is_dir():
        candidate = base.with_name(f"{base.name}.d{index}")
        index += 1
    logger.warning("归档目录与已有文件同名，改用 %s", candidate.name)
    return candidate


async def _extract_any(archive: Path, target: Path, password: str) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 父路径被文件占位等情况：归档结构异常，不是可恢复错误。
        raise TicketError(
            ErrorCode.EXTRACT_FAILED,
            f"无法创建解压目录 {target.name}",
            f"path={target} error={exc}",
        ) from exc
    if archive.name.lower().endswith(".zip"):
        await _unzip(archive, target, password)
    else:
        await _untar(archive, target)


async def _unzip(archive: Path, target: Path, password: str) -> None:
    # -o 覆盖已存在文件：同名归档在多层中出现时不应中断整个任务。
    # -q 抑制每个文件的输出，进度由我们自己统计。
    proc = await asyncio.create_subprocess_exec(
        "unzip",
        "-P",
        password,
        "-q",
        "-o",
        str(archive),
        "-d",
        str(target),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    code = proc.returncode or 0
    # Info-ZIP 用 1 表示"有警告但已解压"，属于可接受范围。
    if code in (0, 1):
        return
    # 密码错误时 -q 会同时抑制 stdout 与 stderr，只能靠退出码判定；
    # 非 -q 场景下错误信息可能落在任一管道，因此两者都保留。
    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    error_code = _classify_unzip(code, text)
    raise TicketError(
        error_code,
        _unzip_message(error_code),
        f"unzip exit={code} archive={archive.name} stderr={text[:800]}",
    )


async def _untar(archive: Path, target: Path) -> None:
    # --no-same-owner / --no-same-permissions：非 root 用户下避免 chown 失败。
    # GNU tar 默认拒绝 ../ 路径，但我们仍在解压后做一次符号链接审查，双保险。
    proc = await asyncio.create_subprocess_exec(
        "tar",
        "-xzf",
        str(archive),
        "-C",
        str(target),
        "--no-same-owner",
        "--no-same-permissions",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    code = proc.returncode or 0
    if code == 0:
        return
    text = stderr.decode("utf-8", errors="replace")
    error_code = ErrorCode.NO_SPACE if "no space left" in text.lower() else ErrorCode.EXTRACT_FAILED
    raise TicketError(
        error_code,
        "解压归档失败",
        f"tar exit={code} archive={archive.name} stderr={text[:800]}",
    )


def _classify_unzip(code: int, stderr: str) -> ErrorCode:
    text = stderr.lower()
    if "incorrect password" in text or "bad password" in text:
        return ErrorCode.BAD_PASSWORD
    # 实测 Info-ZIP 6.0 在密码错误时返回 82 且 stdout/stderr 均为空，
    # 归档本身完好（-l 可正常列表），因此在这个场景下 82 只能归为密码问题。
    if code == 82:
        return ErrorCode.BAD_PASSWORD
    if (
        "end-of-central-directory" in text
        or "cannot find zipfile directory" in text
        or "not a zipfile" in text
        or "unexpected end of file" in text
    ):
        return ErrorCode.ARCHIVE_CORRUPT
    if code == 50 or "no space left" in text:
        return ErrorCode.NO_SPACE
    if "unsupported compression method" in text:
        return ErrorCode.EXTRACT_FAILED
    return ErrorCode.EXTRACT_FAILED


def _unzip_message(code: ErrorCode) -> str:
    return {
        ErrorCode.BAD_PASSWORD: "归档密码错误，请联系运维",
        ErrorCode.ARCHIVE_CORRUPT: "压缩包已损坏",
        ErrorCode.NO_SPACE: "服务器磁盘空间不足",
    }.get(code, "解压失败")


def _audit(root: Path, cfg: Config, stats: ExtractStats) -> None:
    """每层解压后的四项审查。顺序有意为之：先清链接再做统计。"""
    stats.symlinks_removed += _strip_escaped_symlinks(root)

    total, count = _tree_stats(root)
    stats.bytes, stats.files = total, count

    if count > cfg.max_files:
        raise TicketError(
            ErrorCode.QUOTA_EXCEEDED,
            f"解压后文件数 {count} 超过上限 {cfg.max_files}",
            f"files={count} threshold={cfg.max_files}",
        )
    if total > cfg.max_extract_bytes:
        raise TicketError(
            ErrorCode.QUOTA_EXCEEDED,
            f"解压后体积 {total} 字节超过上限 {cfg.max_extract_bytes}",
            f"bytes={total} threshold={cfg.max_extract_bytes}",
        )

    free = shutil.disk_usage(root).free
    if free < cfg.min_free_bytes:
        raise TicketError(
            ErrorCode.NO_SPACE,
            "服务器磁盘空间不足",
            f"free={free} threshold={cfg.min_free_bytes} root={root}",
        )


def _strip_escaped_symlinks(root: Path) -> int:
    """删除指向 root 之外的符号链接，返回删除数量。

    删除失败时直接终止任务而不是跳过：残留的悬空链接会被后续工具跟随，
    风险高于任务失败本身。
    """
    root_real = os.path.realpath(root)
    prefix = root_real + os.sep
    removed = 0

    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        for name in list(dirnames) + filenames:
            path = os.path.join(dirpath, name)
            if not os.path.islink(path):
                continue
            real = os.path.realpath(path)
            if real == root_real or real.startswith(prefix):
                continue
            try:
                os.unlink(path)
                removed += 1
                logger.warning("清理逃逸符号链接 path=%s target=%s", path, real)
            except OSError as exc:
                raise TicketError(
                    ErrorCode.EXTRACT_FAILED,
                    "无法清理逃逸符号链接，已中止",
                    f"path={path} target={real} error={exc}",
                ) from exc

    return removed


def _tree_stats(root: Path) -> tuple[int, int]:
    """统计真实文件的总字节与数量，不跟随、不统计符号链接。"""
    total = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            path = os.path.join(dirpath, name)
            try:
                info = os.lstat(path)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            total += info.st_size
            count += 1
    return total, count
