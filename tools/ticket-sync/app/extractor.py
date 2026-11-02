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
# .rar 来自客户现场自行打包的转储（实测 Q2026102700323 是 RAR5）。
# 解压优先用 rarlab 的 unrar（RAR5 全方法），回退 p7zip 的 Rar5 解码器。
ARCHIVE_SUFFIXES = (".tar.gz", ".tgz", ".tar.xz", ".tar.bz2", ".tar", ".zip", ".rar", ".7z")

# 扩展名 → 实际格式。只作为魔数失配时的兜底，不再作为唯一判据。
_SUFFIX_FORMAT = {
    ".tar.gz": "tar.gz",
    ".tgz": "tar.gz",
    ".tar.xz": "tar.xz",
    ".tar.bz2": "tar.bz2",
    ".tar": "tar",
    ".zip": "zip",
    ".rar": "rar",
    ".7z": "7z",
}

# 归档格式的魔数判定。
# 为什么必须看魔数：2026-11-01 实测 Q2026102700681 / Q2026102800262 / Q2026102801855
# 三个工单的 `<工单号>.zip` 实际是**未压缩 tar**（offset 257 是 ustar magic，
# 内容是 10.137.x.x-时间.tgz）。旧实现按扩展名调用 unzip，必然 exit=9，
# 被归类为 archive-corrupt 后进入永不成功的重试循环，累计 12.2 GiB 卡死。
_MAGIC_ZIP = (b"PK\x03\x04", b"PK\x05\x06", b"PK\x07\x08")
_MAGIC_RAR = b"Rar!\x1a\x07"
_MAGIC_7Z = b"7z\xbc\xaf\x27\x1c"
_MAGIC_GZIP = b"\x1f\x8b"
_MAGIC_XZ = b"\xfd7zXZ\x00"
_MAGIC_BZ2 = b"BZh"
_TAR_MAGIC_OFFSET = 257
_TAR_MAGIC = b"ustar"


def _format_from_suffix(path: Path) -> str | None:
    name = path.name.lower()
    for suffix, fmt in _SUFFIX_FORMAT.items():
        if name.endswith(suffix):
            return fmt
    return None


def detect_archive_format(path: Path) -> tuple[str | None, str]:
    """按魔数判定归档真实格式，返回 (格式, 判据)。

    判据取值 `magic` / `suffix`，用于日志区分"我们识别错了"与"文件本身异常"。
    只读前 512 字节：tar 的 magic 固定在 offset 257，再往后没有识别价值。
    """
    try:
        with path.open("rb") as handle:
            head = handle.read(512)
    except OSError:
        return _format_from_suffix(path), "suffix"

    if head[:4] in _MAGIC_ZIP:
        return "zip", "magic"
    if head[: len(_MAGIC_RAR)] == _MAGIC_RAR:
        return "rar", "magic"
    if head[:6] == _MAGIC_7Z:
        return "7z", "magic"
    if head[:2] == _MAGIC_GZIP:
        return "tar.gz", "magic"
    if head[:6] == _MAGIC_XZ:
        return "tar.xz", "magic"
    if head[:3] == _MAGIC_BZ2:
        return "tar.bz2", "magic"
    if len(head) >= _TAR_MAGIC_OFFSET + len(_TAR_MAGIC) and (
        head[_TAR_MAGIC_OFFSET : _TAR_MAGIC_OFFSET + len(_TAR_MAGIC)] == _TAR_MAGIC
    ):
        return "tar", "magic"
    # 魔数无法识别（分卷 zip、加密容器、采集端半成品等）时才回退扩展名。
    return _format_from_suffix(path), "suffix"

# 占位包阈值：合法 zip 的最小体积是空归档的 22 字节（EOCD 记录），
# 合法 tar.gz 至少含 10 字节 gzip 头 + 1024 字节 tar 结束块。
# 低于该值只可能是采集端写坏的占位文件（生产实测有 1 字节的 LOG_run_shm.txt.zip，
# unzip 报 exit=9）。它的缺失不影响主体日志，跳过比整包失败更合理。
PLACEHOLDER_MAX_BYTES = 64


async def extract(
    ticket_id: str,
    archive: Path,
    work_dir: Path,
    cfg: Config,
    job_id: str,
    on_progress: Callable[[ExtractStats], None] | None = None,
    on_archive: Callable[[str], None] | None = None,
) -> ExtractStats:
    """把 archive 解压到 work_dir，返回统计。

    work_dir 必须是调用方新建的空目录；成功后由调用方负责原子发布。
    """
    stats = ExtractStats()
    # 顶层归档暂存在 work_dir 内（供失败续传），必须从扫描与统计里排除。
    staged = frozenset({archive})

    # 第一层：工单 zip 直接解到 work_dir 根，不额外套目录。
    # 实测归档根就是 log_list.json，套一层会让用户在选择器里多点一次。
    logger.info("ticket=%s job=%s phase=extracting depth=1 开始解压顶层归档", ticket_id, job_id)
    _notify_archive(on_archive, archive.name)
    # 顶层同样走魔数判定：工单包被采集端命名为 .zip 而实际是 tar 的情况真实存在。
    # 降级判据必须是"本次是否解出了东西"，不能用"目录里有文件"——work_dir 可能
    # 留有上一次的残留，否则坏包会被误判成"可降级成功"并发布出去。
    before = _tree_stats(work_dir, staged)[1]
    try:
        await _extract_any(
            archive, work_dir, cfg.archive_password, ticket_id=ticket_id, job_id=job_id
        )
    except TicketError as error:
        if not error.degradable:
            raise
        # 顶层归档截断：与"层数超限只停不判死"同源——已解出的内容是可用日志，
        # 为残缺的尾部丢掉整包不划算。若本次一条都没解出来，则仍按失败处理。
        if _tree_stats(work_dir, staged)[1] <= before:
            raise
        stats.truncated_archives += 1
        logger.warning(
            "ticket=%s job=%s 顶层归档截断，保留已解出内容继续 path=%s code=%s detail=%s",
            ticket_id,
            job_id,
            archive.name,
            error.code,
            error.detail,
        )
    # 归档不在这里删除。旧实现在解压开始时 unlink 归档"腾空间"，导致解压中途失败时
    # work_dir 里连归档都没有，下次重试只能全量重拉——#1408 想要的续传在 extract
    # 阶段完全失效。改为由调用方在解压成功、发布之前释放（见 JobRegistry._release_archive）。
    stats.archives += 1
    stats.depth = 1
    # 必须在扫描嵌套归档之前补齐权限：源端目录可能缺执行位，
    # 不补的话下一轮 os.walk 进不去，里面的归档会被整片漏扫。
    stats.permissions_fixed += _normalize_permissions(work_dir)
    _audit(work_dir, cfg, stats, staged)
    _notify(on_progress, stats)

    depth = 1
    # 已确认解不开、但按策略保留原件的归档。必须显式记住，
    # 否则下一轮 _find_archives 会再次扫到它，形成"解压失败→降级→再扫到"的死循环。
    skipped_archives: set[Path] = set()
    while True:
        # 先剔除采集端写坏的占位包：它们解不出来，但也不该让整个工单失败，
        # 更不能因为留在原地而在下一轮被反复扫到。
        pending = [
            path
            for path in _drop_placeholders(
                _find_archives(work_dir, staged), ticket_id, job_id, stats
            )
            if path not in skipped_archives
        ]
        if not pending:
            break
        if depth >= cfg.max_depth:
            # 深度上限是「停止下钻」的护栏，不是「整单失败」的判据：
            # 主体日志在浅层已经解出，为几个深层附件丢掉全部产物并不划算
            # （实测最深 7 层，而历史阈值只有 4 层，会把正常包判死）。
            names = ", ".join(p.name for p in pending[:5])
            stats.depth_truncated += len(pending)
            logger.warning(
                "ticket=%s job=%s 达到嵌套层数上限 depth=%d 停止下钻 未解=%d 前几个=%s",
                ticket_id,
                job_id,
                depth,
                len(pending),
                names,
            )
            break

        depth += 1
        for nested in pending:
            target = _target_for(nested)
            relative = str(nested.relative_to(work_dir))
            logger.info(
                "ticket=%s job=%s depth=%d 解压 %s",
                ticket_id,
                job_id,
                depth,
                relative,
            )
            _notify_archive(on_archive, relative)
            try:
                await _extract_any(
                    nested, target, cfg.archive_password, ticket_id=ticket_id, job_id=job_id
                )
            except TicketError as error:
                if error.degradable:
                    # 截断：保留已解出的目录（日志主体通常完整），删掉残缺压缩包
                    # 否则它会在下一轮 _find_archives 被反复扫到、反复失败。
                    skipped_archives.add(nested)
                    try:
                        nested.unlink()
                    except OSError as unlink_error:
                        logger.warning(
                            "ticket=%s job=%s 删除截断归档失败 path=%s error=%s",
                            ticket_id,
                            job_id,
                            relative,
                            unlink_error,
                        )
                    stats.truncated_archives += 1
                    logger.warning(
                        "ticket=%s job=%s 归档截断，保留已解出内容 path=%s code=%s detail=%s",
                        ticket_id,
                        job_id,
                        relative,
                        error.code,
                        error.detail,
                    )
                    continue
                # .rar 降级：宿主的 p7zip 不支持部分 RAR5 压缩方法（实测报
                # "Unsupported Method"），而这类包通常是客户自打的单个转储。
                # 保留原件、记告警、继续走其它归档，避免一个解不开的附件
                # 让整包日志都不可用。
                if not nested.name.lower().endswith(".rar"):
                    raise
                shutil.rmtree(target, ignore_errors=True)
                skipped_archives.add(nested)
                stats.unreadable_archives += 1
                logger.warning(
                    "ticket=%s job=%s 跳过无法解压的归档 path=%s code=%s detail=%s",
                    ticket_id,
                    job_id,
                    relative,
                    error.code,
                    error.detail,
                )
                continue
            nested.unlink(missing_ok=True)
            stats.archives += 1
            stats.permissions_fixed += _normalize_permissions(target)

        stats.depth = depth
        _audit(work_dir, cfg, stats, staged)
        _notify(on_progress, stats)

    logger.info(
        "ticket=%s job=%s phase=extracting 完成 depth=%d files=%d bytes=%d archives=%d "
        "清理链接=%d 跳过占位=%d 补权限=%d 无法解压=%d 截断=%d",
        ticket_id,
        job_id,
        stats.depth,
        stats.files,
        stats.bytes,
        stats.archives,
        stats.symlinks_removed,
        stats.placeholders_skipped,
        stats.permissions_fixed,
        stats.unreadable_archives,
        stats.truncated_archives,
    )
    return stats


def _notify(callback: Callable[[ExtractStats], None] | None, stats: ExtractStats) -> None:
    if callback is not None:
        callback(stats)


def _notify_archive(callback: Callable[[str], None] | None, name: str) -> None:
    if callback is not None:
        callback(name)


def _find_archives(root: Path, exclude: frozenset[Path] = frozenset()) -> list[Path]:
    """扫描待处理的归档。不跟随符号链接目录，避免遍历到根目录之外。

    `exclude` 用于排除顶层工单包本身：它为了支持续传而保留在 work_dir 里，
    若不过滤就会被当成嵌套归档再解一次（实测会在产物里多出一层同名目录）。
    """
    found: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        dirnames[:] = [
            name for name in dirnames if not os.path.islink(os.path.join(dirpath, name))
        ]
        for name in filenames:
            path = os.path.join(dirpath, name)
            if os.path.islink(path):
                continue
            candidate = Path(path)
            if candidate in exclude:
                continue
            if name.lower().endswith(ARCHIVE_SUFFIXES):
                found.append(candidate)
    return sorted(found)


def _drop_placeholders(
    pending: list[Path],
    ticket_id: str,
    job_id: str,
    stats: ExtractStats,
) -> list[Path]:
    """剔除采集端写坏的占位归档，返回真正可解压的归档列表。

    判定只依赖体积：合法 zip 最小 22 字节、合法 tar.gz 至少 1 KiB 量级，
    因此小于 PLACEHOLDER_MAX_BYTES 的文件在两种格式下都不可能被解析，删除它
    不会丢失任何可用信息。反过来，若保留，它会让整个工单在每轮自动巡检里
    反复失败、反复重拉（生产实测单工单 192 次，累计数百 GiB 无效流量）。

    删除失败时直接终止任务而不是跳过：残留文件会在下一轮被重新扫到，
    形成永不结束的失败循环。
    """
    kept: list[Path] = []
    for path in pending:
        try:
            size = path.stat().st_size
        except OSError as exc:
            logger.warning(
                "ticket=%s job=%s 读取归档体积失败，跳过 path=%s error=%s",
                ticket_id,
                job_id,
                path,
                exc,
            )
            continue
        if size >= PLACEHOLDER_MAX_BYTES:
            kept.append(path)
            continue

        try:
            path.unlink()
        except OSError as exc:
            raise TicketError(
                ErrorCode.EXTRACT_FAILED,
                "无法清理占位归档，已中止",
                f"path={path} size={size} error={exc}",
            ) from exc

        stats.placeholders_skipped += 1
        logger.warning(
            "ticket=%s job=%s 跳过占位归档 path=%s size=%d 原因=小于 %d 字节，不可能是合法归档",
            ticket_id,
            job_id,
            path,
            size,
            PLACEHOLDER_MAX_BYTES,
        )
    return kept


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


async def _extract_any(
    archive: Path,
    target: Path,
    password: str,
    ticket_id: str = "",
    job_id: str = "",
) -> None:
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        # 父路径被文件占位等情况：归档结构异常，不是可恢复错误。
        raise TicketError(
            ErrorCode.EXTRACT_FAILED,
            f"无法创建解压目录 {target.name}",
            f"path={target} error={exc}",
        ) from exc

    fmt, basis = detect_archive_format(archive)
    suffix_fmt = _format_from_suffix(archive)
    if fmt is None:
        raise TicketError(
            ErrorCode.EXTRACT_FAILED,
            f"无法识别归档格式 {archive.name}",
            f"path={archive} head=未知魔数 扩展名={suffix_fmt}",
        )
    if basis == "magic" and suffix_fmt is not None and suffix_fmt != fmt:
        # 扩展名与真实格式不一致必须留痕：这不是"坏包"，是我们的格式判据错了。
        logger.warning(
            "ticket=%s job=%s 归档扩展名与实际格式不一致 path=%s 扩展名=%s 实际=%s 按实际格式解压",
            ticket_id,
            job_id,
            archive.name,
            suffix_fmt,
            fmt,
        )

    if fmt == "zip":
        await _unzip(archive, target, password)
    elif fmt == "rar":
        await _unrar(archive, target, password)
    elif fmt == "7z":
        await _seven_zip(archive, target, password)
    else:
        await _untar(archive, target, fmt)


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
    stdout, stderr = await _communicate(proc)
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
        # 成员级数据损坏允许降级：报错前已解出的成员仍然可用。
        # 顶层归档若一条都没解出，由 extract() 的零输出检查重新判定失败。
        degradable=error_code is ErrorCode.ARCHIVE_CORRUPT,
    )


async def _unrar(archive: Path, target: Path, password: str) -> None:
    """解 RAR：优先 unrar，不可用时回退 7z。

    宿主机没有 root，装不了发行版包，因此 rarlab 的静态 unrar 由
    deploy/install-user.sh 放到 ~/.local/bin/unrar。两者对 RAR5 的方法覆盖
    并不相同：实测 1.dump（v50 -m3）p7zip 23.01 报 Unsupported Method，
    而 unrar 7.23 能正常读。所以 unrar 失败时仍给 7z 一次机会；
    只有密码错才直接放弃——那是归档本身的问题，换工具没用。
    """
    unrar = shutil.which("unrar")
    if unrar:
        try:
            await _unrar_binary(unrar, archive, target, password)
            return
        except TicketError as error:
            if error.code is ErrorCode.BAD_PASSWORD:
                raise
            logger.warning(
                "归档 unrar 解压失败，回退 7z archive=%s detail=%s",
                archive.name,
                error.detail,
            )
    await _seven_zip(archive, target, password)


async def _unrar_binary(unrar: str, archive: Path, target: Path, password: str) -> None:
    """rarlab unrar：退出码 0 成功、1 警告、3 CRC 错、10 密码错，其余致命。"""
    # 无密码时必须传 -p- 明确「不给密码」，否则 unrar 会交互式询问并挂住。
    cmd = [unrar, "x", "-o+", "-y", "-idq"]
    cmd.append(f"-p{password}" if password else "-p-")
    cmd.extend([str(archive), f"{target}/"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate(proc)
    code = proc.returncode or 0
    if code in (0, 1):
        return

    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    lowered = text.lower()
    if code == 10 or "wrong password" in lowered:
        error_code = ErrorCode.BAD_PASSWORD
    elif code == 3 or "checksum error" in lowered:
        error_code = ErrorCode.ARCHIVE_CORRUPT
    elif "no space left" in lowered:
        error_code = ErrorCode.NO_SPACE
    else:
        error_code = ErrorCode.EXTRACT_FAILED
    raise TicketError(
        error_code,
        _unzip_message(error_code),
        f"unrar exit={code} archive={archive.name} stderr={text[:800]}",
    )


async def _seven_zip(archive: Path, target: Path, password: str) -> None:
    """用 7z 解压：既是 RAR 的回退通道（p7zip 内置 RAR5 解码器），
    也是 .7z 格式的唯一通道。

    注意 -o 与路径之间不能有空格，-p 同理；密码走 argv 的既有风险与 unzip 一致。
    """
    # -bb0 只关掉逐文件输出，错误信息必须保留，否则无法分类失败原因。
    cmd = ["7z", "x", "-y", "-bb0", f"-o{target}"]
    if password:
        cmd.append(f"-p{password}")
    cmd.append(str(archive))

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await _communicate(proc)
    code = proc.returncode or 0
    # 7z：0 成功；1 有警告但已解压。其余均为失败。
    if code in (0, 1):
        return

    text = (stdout + b"\n" + stderr).decode("utf-8", errors="replace")
    lowered = text.lower()
    if "wrong password" in lowered or "can not open encrypted archive" in lowered:
        error_code = ErrorCode.BAD_PASSWORD
    elif "no space left" in lowered:
        error_code = ErrorCode.NO_SPACE
    elif "can not open the file as archive" in lowered or "is not archive" in lowered:
        error_code = ErrorCode.ARCHIVE_CORRUPT
    elif "unsupported method" in lowered:
        # p7zip 内置的 RAR5 解码器不支持较新的压缩方法（实测 1.dump 报此错）。
        # 这是工具能力问题，不是包损坏，交给上层按"无法解压"降级处理。
        error_code = ErrorCode.EXTRACT_FAILED
    else:
        error_code = ErrorCode.EXTRACT_FAILED
    raise TicketError(
        error_code,
        _unzip_message(error_code),
        f"7z exit={code} archive={archive.name} stderr={text[:800]}",
    )


_TAR_MODE = {
    "tar": "-xf",
    "tar.gz": "-xzf",
    "tar.xz": "-xJf",
    "tar.bz2": "-xjf",
}


async def _untar(archive: Path, target: Path, fmt: str = "tar.gz") -> None:
    """解 tar 及其压缩变体。

    必须按实际格式选参数：`-xzf` 对未压缩 tar 会因 gzip 头缺失直接失败
    （实测 Q2026102800262 的 .zip 其实是未压缩 tar），而 `-xf` 对 gzip 流
    依赖 tar 版本自动识别压缩，不做假设。
    """
    # --no-same-owner / --no-same-permissions：非 root 用户下避免 chown 失败。
    # GNU tar 默认拒绝 ../ 路径，但我们仍在解压后做一次符号链接审查，双保险。
    proc = await asyncio.create_subprocess_exec(
        "tar",
        _TAR_MODE.get(fmt, "-xf"),
        str(archive),
        "-C",
        str(target),
        "--no-same-owner",
        "--no-same-permissions",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await _communicate(proc)
    code = proc.returncode or 0
    if code == 0:
        return
    text = stderr.decode("utf-8", errors="replace")
    lowered = text.lower()
    if "no space left" in lowered:
        raise TicketError(
            ErrorCode.NO_SPACE,
            "服务器磁盘空间不足",
            f"tar exit={code} archive={archive.name} stderr={text[:800]}",
        )
    if "unexpected end of file" in lowered or "unexpected eof" in lowered:
        # 压缩流被截断：数据本身残缺，重试不可能变好（实测 Q2026102802029 的
        # 内层 tgz 缺尾部 CRC+ISIZE，gzip 报 unexpected end of file）。
        # 但报错之前的条目通常已完整解出，所以标记 degradable，由调用方决定保留。
        raise TicketError(
            ErrorCode.ARCHIVE_CORRUPT,
            _unzip_message(ErrorCode.ARCHIVE_CORRUPT),
            f"tar exit={code} archive={archive.name} truncated=1 stderr={text[:800]}",
            degradable=True,
        )
    raise TicketError(
        ErrorCode.EXTRACT_FAILED,
        "解压归档失败",
        f"tar exit={code} archive={archive.name} stderr={text[:800]}",
    )


async def _communicate(proc: asyncio.subprocess.Process) -> tuple[bytes, bytes]:
    """等待子进程结束，并把取消传递下去。

    任务被取消时抛给我们的只是 CancelledError，解压进程本身仍在运行；
    不显式 kill 会留下孤儿进程继续写磁盘，而它的目标目录马上就会被清理。
    """
    try:
        return await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        await proc.wait()
        raise


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
        # exit=80 是"成员数据无法解压"（invalid compressed data / bad CRC）：
        # 打包时该成员就没写完整。实测 Q2026102802029 的 LOG_tcpstat.txt.zip
        # 就是这种——坏的是单个成员，不该让整包（26 GB 已解出内容）失败。
        or code == 80
        or "invalid compressed data" in text
        or "bad crc" in text
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


def _audit(
    root: Path,
    cfg: Config,
    stats: ExtractStats,
    exclude: frozenset[Path] = frozenset(),
) -> None:
    """每层解压后的四项审查。顺序有意为之：先清链接再做统计。"""
    stats.symlinks_removed += _strip_escaped_symlinks(root)

    total, count = _tree_stats(root, exclude)
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


def _normalize_permissions(root: Path) -> int:
    """补齐产物属主的读写位，目录额外补执行位，返回实际修正的条目数。

    为什么必须做：
    - 源端 HCI 的 `sf/log/kubelet`、`sf/log/containerd` 目录权限是 `drw-r--r--`，
      缺执行位。tar 的 `--no-same-permissions` 只是把归档权限与 umask 求交，
      不会补回缺失的 x 位；unzip 则直接照搬 zip 外部属性。
    - 目录缺 x 的后果是连属主都进不去：DSH（uid 1000）读不到 kubelet.log，
      本服务的 os.walk 也遍历不进，深层嵌套归档被整片漏扫、quota 统计偏低。

    只改属主是自己的条目，避免对解压时保留了他人属主的文件触发 EPERM。
    """
    uid = os.getuid()
    fixed = 0

    def fix(path: str, want: int) -> int:
        try:
            info = os.lstat(path)
        except OSError:
            return 0
        # 符号链接与设备文件不碰：chmod 会跟随链接，改到目标上去。
        if not stat.S_ISDIR(info.st_mode) and not stat.S_ISREG(info.st_mode):
            return 0
        if info.st_uid != uid:
            return 0
        mode = stat.S_IMODE(info.st_mode)
        if mode & want == want:
            return 0
        try:
            os.chmod(path, mode | want)
        except OSError as exc:
            logger.warning("补齐权限失败 path=%s error=%s", path, exc)
            return 0
        return 1

    fixed += fix(str(root), stat.S_IRWXU)
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        # 先修子目录再继续下行：os.walk 下一步就要进入它们，缺 x 会直接失败。
        for name in dirnames:
            fixed += fix(os.path.join(dirpath, name), stat.S_IRWXU)
        for name in filenames:
            fixed += fix(os.path.join(dirpath, name), stat.S_IRUSR | stat.S_IWUSR)
    return fixed


def _tree_stats(root: Path, exclude: frozenset[Path] = frozenset()) -> tuple[int, int]:
    """统计真实文件的总字节与数量，不跟随、不统计符号链接。

    `exclude` 同样用于剔除暂存的顶层归档：它是输入缓存，不是产物。若计入，
    `.sync-meta.json` 的 bytes 会比真实产物大一个归档体积，容量淘汰随之失真。
    """
    total = 0
    count = 0
    for dirpath, _dirnames, filenames in os.walk(root, followlinks=False):
        for name in filenames:
            candidate = Path(os.path.join(dirpath, name))
            if candidate in exclude:
                continue
            try:
                info = os.lstat(candidate)
            except OSError:
                continue
            if stat.S_ISLNK(info.st_mode):
                continue
            total += info.st_size
            count += 1
    return total, count
