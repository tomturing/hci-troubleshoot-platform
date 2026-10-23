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
from collections.abc import Callable
from pathlib import Path

from .config import Config
from .models import ErrorCode, TicketError

logger = logging.getLogger(__name__)

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

    await asyncio.gather(
        _pump(proc.stdout, handle_progress),
        _pump(proc.stderr, handle_stderr),
    )
    code = await proc.wait()

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
