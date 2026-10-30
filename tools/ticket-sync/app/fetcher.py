"""归档拉取：rsync over ssh，支持断点续传与进度回调。

为什么用 rsync 而不是 SFTP：
- 断点续传（--partial --inplace）与完整性校验是内建的；
- 0.2 侧已确认 /usr/bin/rsync 可用，无需部署任何服务；
- 大文件（最大 93 GiB）传输中断后重跑成本最低。

不用 -a：-a 含 -o/-g，非 root 用户无法保留属主，会以 exit 23 失败。
改用 -rlt（递归、保留符号链接语义、保留时间），权限按本机 umask 生成。
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from .config import Config
from .models import ErrorCode, TicketError

logger = logging.getLogger(__name__)
TZ = ZoneInfo("Asia/Shanghai")

# rsync --info=progress2 的单行格式：`  12,345,678  45%  1.23MB/s  0:00:01`
_PROGRESS_RE = re.compile(r"([\d,]+)\s+(\d+)%")

# 退出码语义见 rsync 手册。这里只映射需要区分的几类，其余归入 ssh-failed / extract-failed。
_EXIT_CODES = {
    1: ErrorCode.EXTRACT_FAILED,  # 语法或用法错误
    3: ErrorCode.EXTRACT_FAILED,  # 文件选择错误
    5: ErrorCode.EXTRACT_FAILED,  # 客户端协议启动失败
    10: ErrorCode.SSH_FAILED,  # socket I/O 错误
    11: ErrorCode.QUOTA_EXCEEDED,  # 文件过大
    12: ErrorCode.SSH_FAILED,  # 协议数据流错误
    23: ErrorCode.NOT_FOUND,  # 部分文件未传输（源端缺失/权限）
    24: ErrorCode.NOT_FOUND,  # 传输中文件消失
    30: ErrorCode.SSH_FAILED,  # 超时
    35: ErrorCode.SSH_FAILED,  # 连接超时
    255: ErrorCode.SSH_FAILED,  # ssh 本身失败
}


@dataclass(frozen=True)
class RemoteArchive:
    """远端当天生成的顶层工单归档元数据。"""

    ticket_id: str
    path: str
    size: int
    mtime: float


# ssh 自身失败的退出码：连接、认证、主机密钥等原因，与远端命令无关。
SSH_CONNECT_FAILED = 255
# 连接层失败的重试间隔。实测这类失败多为瞬时（对端 sshd 重启、网络抖动），
# 2026-10-30 16:25 那次就是单次失败即报错，下一轮又自愈了。
SSH_RETRY_DELAY_SECONDS = 5


def _ssh_argv(cfg: Config, command: str) -> list[str]:
    """统一的 ssh 参数。

    BatchMode 禁止交互（否则子进程会挂在密码提示上），ConnectTimeout 防止
    半开连接挂死；ServerAlive 让"物理断开但 TCP 未感知"的场景尽快收敛。
    """
    return [
        "ssh",
        "-p",
        str(cfg.source_port),
        "-i",
        str(cfg.ssh_key),
        "-o",
        "BatchMode=yes",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "-o",
        "ConnectTimeout=10",
        "-o",
        "ServerAliveInterval=15",
        "-o",
        "ServerAliveCountMax=3",
        cfg.source,
        command,
    ]


async def _ssh_run(cfg: Config, command: str, message: str, timeout: float = 20) -> bytes:
    """执行一条远端只读命令并返回 stdout。

    失败信息必须可归因：历史实现只把 stderr 写进 detail，而 exit=255 时
    stderr 往往是空的（实测 `ssh exit=255 stderr=`），导致事故无从查起。
    这里固定带上 exit code、尝试次数、耗时、远端地址，stderr 为空时显式标注。
    """
    last_error: TicketError | None = None
    for attempt in (1, 2):
        started = time.monotonic()
        proc = await asyncio.create_subprocess_exec(
            *_ssh_argv(cfg, command),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        try:
            stdout, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout)
        except asyncio.CancelledError:
            proc.kill()
            await proc.wait()
            raise
        except TimeoutError as exc:
            proc.kill()
            await proc.wait()
            elapsed_ms = int((time.monotonic() - started) * 1000)
            raise TicketError(
                ErrorCode.SSH_FAILED,
                message,
                f"timeout attempt={attempt} elapsed_ms={elapsed_ms} remote={cfg.source}",
            ) from exc

        code = proc.returncode or 0
        elapsed_ms = int((time.monotonic() - started) * 1000)
        if code == 0:
            return stdout

        detail = stderr.decode("utf-8", errors="replace")[:800]
        logger.warning(
            "ssh 失败 message=%s attempt=%d exit=%d elapsed_ms=%d remote=%s stderr=%s",
            message,
            attempt,
            code,
            elapsed_ms,
            cfg.source,
            detail or "(空)",
        )
        last_error = TicketError(
            ErrorCode.SSH_FAILED,
            message,
            f"ssh exit={code} attempt={attempt} elapsed_ms={elapsed_ms} "
            f"remote={cfg.source} stderr={detail or '(空)'}",
        )
        # 只有连接层失败值得重试；远端命令本身报错（如 unzip 缺失）重试无意义。
        if code != SSH_CONNECT_FAILED or attempt == 2:
            break
        await asyncio.sleep(SSH_RETRY_DELAY_SECONDS)

    assert last_error is not None  # 循环只有两种出口：返回 stdout 或留下 last_error
    raise last_error


async def list_today_archives(cfg: Config, now: datetime | None = None) -> list[RemoteArchive]:
    """列出日志源服务器当天生成的顶层工单 zip。

    只接受固定的 `/sf/log/<ticket>/<ticket>.zip` 形态，避免远端目录中其他 zip
    被误当成工单任务。日期边界由本地 Asia/Shanghai 计算，与服务接口时间口径一致。
    """
    current = (now or datetime.now(TZ)).astimezone(TZ)
    start = current.replace(hour=0, minute=0, second=0, microsecond=0)
    end = start + timedelta(days=1)
    command = (
        "LC_ALL=C find /sf/log -mindepth 2 -maxdepth 2 -type f -name '*.zip' "
        f"-newermt '{start:%Y-%m-%d %H:%M:%S}' "
        f"! -newermt '{end:%Y-%m-%d %H:%M:%S}' "
        "-printf '%p\\t%s\\t%T@\\n'"
    )

    logger.info("自动扫描日志源服务器当天归档 source=%s", cfg.source)
    stdout = await _ssh_run(cfg, command, "无法扫描日志源服务器")

    archives = _parse_remote_archives(stdout.decode("utf-8", errors="replace"))
    logger.info("自动扫描完成 当天归档数=%d", len(archives))
    return archives


async def list_remote_entries(cfg: Config, ticket_id: str, limit: int = 50) -> list[str]:
    """只读远端归档的中央目录，返回条目名（不解压、不下载）。

    zip 的中央目录不加密，所以不需要归档密码就能列出内容——这是唯一能在
    "下载 30 GiB 之前"判断包里到底是什么的手段（实测有工单只含 vmcore）。
    """
    remote = cfg.remote_archive(ticket_id)
    command = f"unzip -Z1 {remote} 2>/dev/null | head -n {limit}"
    stdout = await _ssh_run(cfg, command, "无法读取归档目录")
    return [line.strip() for line in stdout.decode("utf-8", errors="replace").splitlines() if line.strip()]


def looks_like_non_log_bundle(entries: list[str]) -> bool:
    """判断归档是否为"非日志载荷"（如仅含 vmcore 的崩溃转储）。

    判定刻意保守，只在特征非常明确时才跳过，避免把正常日志包误判：
    - 条目数 <= 3（日志包必然有 log_list.json + 多个 sp_*.tgz）
    - 不含 .tgz，也不含 log_list.json
    - 含 vmdumpcore 或 .core 结尾的条目
    """
    if not entries or len(entries) > 3:
        return False
    lowered = [entry.lower() for entry in entries]
    if any(entry.endswith(".tgz") or entry == "log_list.json" for entry in lowered):
        return False
    return any("vmdumpcore" in entry or entry.endswith(".core") for entry in lowered)


def _parse_remote_archives(output: str) -> list[RemoteArchive]:
    """解析 find 的固定三列输出，并丢弃不符合工单目录契约的文件。"""
    result: list[RemoteArchive] = []
    pattern = re.compile(r"^/sf/log/(Q\d{13})/\1\.zip$")
    for raw in output.splitlines():
        path, separator, rest = raw.partition("\t")
        if not separator:
            continue
        size_text, separator, mtime_text = rest.partition("\t")
        if not separator:
            continue
        match = pattern.fullmatch(path)
        if match is None:
            continue
        try:
            size = int(size_text)
            mtime = float(mtime_text)
        except ValueError:
            continue
        if size < 0 or mtime <= 0:
            continue
        result.append(RemoteArchive(match.group(1), path, size, mtime))
    return sorted(result, key=lambda item: item.ticket_id)


async def fetch(
    ticket_id: str,
    cfg: Config,
    work_dir: Path,
    job_id: str,
    on_progress: Callable[[int, int], None] | None = None,
) -> Path:
    """把 <工单号>.zip 拉到 work_dir，返回本地路径。

    失败一律抛 TicketError，调用方不需要关心 subprocess 细节。
    """
    work_dir.mkdir(parents=True, exist_ok=True)
    target = work_dir / f"{ticket_id}.zip"
    remote = f"{cfg.source}:{cfg.remote_archive(ticket_id)}"

    # ssh 参数：BatchMode 禁止交互提示，避免子进程挂死在密码输入上。
    # accept-new 只接受首次出现的 host key，已存在且不匹配时仍会失败，不做降级。
    ssh_command = (
        f"ssh -p {cfg.source_port} -i {cfg.ssh_key} "
        "-o BatchMode=yes -o StrictHostKeyChecking=accept-new -o ConnectTimeout=10"
    )

    cmd = [
        "rsync",
        "-rlt",
        "--partial",
        "--inplace",
        "--info=progress2",
        "--no-inc-recursive",
        "--timeout=120",
        "-e",
        ssh_command,
        remote,
        str(target),
    ]

    logger.info("ticket=%s job=%s phase=fetching 开始拉取 %s", ticket_id, job_id, remote)

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )

    stderr_lines: list[str] = []
    last_total = 0

    def handle_progress(line: str) -> None:
        nonlocal last_total
        match = _PROGRESS_RE.search(line)
        if not match:
            return
        received = int(match.group(1).replace(",", ""))
        # 百分比足够驱动前端，但 total 只有 rsync 内部知道；
        # 这里把百分比换算成总量，避免前端再要一个字段。
        percent = int(match.group(2))
        if percent > 0 and received > 0 and on_progress is not None:
            total = int(received * 100 / percent)
            last_total = max(last_total, total)
            on_progress(received, last_total)

    def handle_stderr(line: str) -> None:
        stderr_lines.append(line)
        if len(stderr_lines) <= 20:  # 只保留前 20 行，避免脏输出撑爆日志
            logger.debug("ticket=%s job=%s rsync-stderr %s", ticket_id, job_id, line)

    if proc.stdout is None or proc.stderr is None:
        raise TicketError(ErrorCode.SSH_FAILED, "无法建立 rsync 管道")

    try:
        await asyncio.gather(
            _pump(proc.stdout, handle_progress),
            _pump(proc.stderr, handle_stderr),
        )
        code = await proc.wait()
    except asyncio.CancelledError:
        # 取消时被取消的是等待方，rsync 本身不会退出，会继续在后台吞噬带宽
        # 和磁盘。必须显式终止并回收，避免留下孤儿进程。
        proc.kill()
        await proc.wait()
        raise

    if code != 0:
        stderr_text = "\n".join(stderr_lines)
        error_code = _classify(code, stderr_text)
        raise TicketError(
            error_code,
            _user_message(error_code),
            f"rsync exit={code} cmd={remote} stderr={stderr_text[:800]}",
        )

    if not target.exists():
        raise TicketError(
            ErrorCode.NOT_FOUND,
            "源端未返回归档文件",
            f"rsync exit=0 但本地不存在 {target}",
        )

    size = target.stat().st_size
    logger.info("ticket=%s job=%s phase=fetching 完成 bytes=%d", ticket_id, job_id, size)

    if size > cfg.max_archive_bytes:
        target.unlink(missing_ok=True)
        raise TicketError(
            ErrorCode.TOO_LARGE,
            f"归档 {size} 字节超过单工单上限 {cfg.max_archive_bytes}",
            f"threshold={cfg.max_archive_bytes} actual={size}",
        )

    return target


async def _pump(stream: asyncio.StreamReader, on_line: Callable[[str], None]) -> None:
    """按 \\r 与 \\n 双分隔读取。

    rsync 的进度输出用 \\r 原地刷新，没有换行；按行读取会一直阻塞到进程结束，
    进度就退化成完成时的一次性跳变。
    """
    buffer = b""
    while True:
        chunk = await stream.read(4096)
        if not chunk:
            break
        buffer += chunk
        parts = re.split(rb"[\r\n]", buffer)
        buffer = parts.pop()
        for part in parts:
            if part:
                on_line(part.decode("utf-8", errors="replace"))
    if buffer:
        on_line(buffer.decode("utf-8", errors="replace"))


def _classify(exit_code: int, stderr: str) -> ErrorCode:
    text = stderr.lower()
    if "no such file or directory" in text or "link_stat" in text:
        return ErrorCode.NOT_FOUND
    if "permission denied" in text and "publickey" in text:
        return ErrorCode.SSH_FAILED
    return _EXIT_CODES.get(exit_code, ErrorCode.EXTRACT_FAILED)


def _user_message(code: ErrorCode) -> str:
    return {
        ErrorCode.NOT_FOUND: "该工单在 0.2 上不存在",
        ErrorCode.SSH_FAILED: "无法连接 0.2",
        ErrorCode.QUOTA_EXCEEDED: "工单包超过大小上限",
        ErrorCode.TOO_LARGE: "工单包超过大小上限",
    }.get(code, "拉取归档失败")
