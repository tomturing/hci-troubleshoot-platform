"""命令行入口：S1 阶段用于单工单验证，S2 起作为服务内核被 FastAPI 复用。

用法：
    uv run python -m app.cli Q2026052801304
    uv run python -m app.cli Q2026052801304 --force
    uv run python -m app.cli Q2026052801304 --dry-run

退出码：
    0 成功；2 参数或配置问题；3 业务失败（TicketError）
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import shutil
import sys
import time
import uuid
from pathlib import Path

from .config import Config
from .extractor import extract
from .fetcher import fetch
from .models import ErrorCode, TicketError, validate_ticket_id
from .publisher import cleanup_workspace, prune_trash, publish

logger = logging.getLogger("ticket-sync")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ticket-sync",
        description="同步并解压指定工单的日志归档",
    )
    parser.add_argument("ticket_id", help="工单号，形如 Q2026052801304")
    parser.add_argument("--env-file", type=Path, default=None, help="配置文件的路径")
    parser.add_argument("--force", action="store_true", help="目标已存在时强制重建")
    parser.add_argument("--keep-archive", action="store_true", help="保留下载的 zip 以便复查")
    parser.add_argument("--dry-run", action="store_true", help="只校验配置与工单号，不执行")
    parser.add_argument("-v", "--verbose", action="store_true", help="输出调试日志")
    args = parser.parse_args(argv)

    _setup_logging(args.verbose)

    try:
        return asyncio.run(_run(args))
    except TicketError as error:
        logger.error("任务失败 %s detail=%s", error, error.detail)
        return 3


async def _run(args: argparse.Namespace) -> int:
    cfg = Config.from_env(args.env_file)
    ticket_id = validate_ticket_id(args.ticket_id)

    job_id = uuid.uuid4().hex[:8]
    final_dir = cfg.root / ticket_id
    work_dir = cfg.tmp_dir() / f"{ticket_id}-{job_id}"

    logger.info(
        "ticket=%s job=%s 受理 phase=queued root=%s force=%s",
        ticket_id,
        job_id,
        cfg.root,
        args.force,
    )

    if args.dry_run:
        _preflight(cfg, ticket_id, final_dir, args.force)
        print(f"[dry-run] 校验通过 ticket={ticket_id} 目标={final_dir}")
        return 0

    _preflight(cfg, ticket_id, final_dir, args.force)

    cfg.tmp_dir().mkdir(parents=True, exist_ok=True)
    work_dir.mkdir(parents=True, exist_ok=False)

    started = time.monotonic()
    try:
        archive = await fetch(
            ticket_id,
            cfg,
            work_dir,
            job_id,
            on_progress=_fetch_progress(ticket_id),
        )
        _clear_line()

        # 密码只在真正解压时必需，放在这里而不是配置加载处，
        # 是为了让 --dry-run 能独立用于连通性与权限检查。
        if not cfg.archive_password:
            raise TicketError(
                ErrorCode.BAD_PASSWORD,
                "未配置归档密码",
                "TICKET_ARCHIVE_PASSWORD 为空",
            )

        stats = await extract(
            ticket_id,
            archive,
            work_dir,
            cfg,
            job_id,
            on_progress=_extract_progress(ticket_id),
        )
        _clear_line()

        if args.keep_archive:
            keep_dir = cfg.root / ".archives" / ticket_id
            keep_dir.mkdir(parents=True, exist_ok=True)
            shutil.copy2(archive, keep_dir / archive.name)
            logger.info("归档已保留 path=%s", keep_dir / archive.name)

        replaced = publish(work_dir, final_dir, cfg.trash_dir())
        prune_trash(cfg.trash_dir())

        elapsed = time.monotonic() - started
        print(
            f"完成 ticket={ticket_id} 耗时={elapsed:.1f}s "
            f"层数={stats.depth} 文件={stats.files} "
            f"解压字节={_human(stats.bytes)} 清理链接={stats.symlinks_removed}"
        )
        print(f"产物目录: {final_dir}")
        if replaced is not None:
            print(f"旧版本: {replaced}")
        return 0

    except BaseException:
        # 任何异常都先清理临时目录，再向上抛：残留的 .tmp 会在下次启动时被清空，
        # 但在 S1 阶段人工排查时，残留比没有残留更容易误判。
        cleanup_workspace(work_dir)
        raise


def _preflight(cfg: Config, ticket_id: str, final_dir: Path, force: bool) -> None:
    """执行前的快速校验，避免拉到一半才发现不可写或空间不足。"""
    if not cfg.root.is_dir():
        raise TicketError(
            ErrorCode.NO_SPACE,
            f"产物根目录不存在: {cfg.root}",
            "请先创建并设置属主为服务运行用户",
        )

    wrote = _check_writable(cfg.root)
    if not wrote:
        raise TicketError(
            ErrorCode.NO_SPACE,
            f"产物根目录不可写: {cfg.root}",
            f"uid={os.getuid()}",
        )

    free = shutil.disk_usage(cfg.root).free
    if free < cfg.min_free_bytes:
        raise TicketError(
            ErrorCode.NO_SPACE,
            "服务器磁盘空间不足",
            f"free={free} threshold={cfg.min_free_bytes}",
        )

    if final_dir.exists() and not force:
        raise TicketError(
            ErrorCode.EXTRACT_FAILED,
            f"产物已存在: {final_dir}（加 --force 重建）",
            f"ticket={ticket_id}",
        )


def _check_writable(root: Path) -> bool:
    probe = root / f".write-probe-{uuid.uuid4().hex[:6]}"
    try:
        probe.write_text("", encoding="utf-8")
    except OSError:
        return False
    finally:
        probe.unlink(missing_ok=True)
    return True


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
        stream=sys.stderr,
    )


def _fetch_progress(ticket_id: str):
    state = {"next": 0.0, "total": 0}

    def callback(received: int, total: int) -> None:
        state["total"] = total or state["total"]
        now = time.monotonic()
        if now < state["next"]:
            return
        state["next"] = now + 1.0
        percent = received * 100 // total if total else 0
        _render(f"拉取 {ticket_id} {_human(received)}/{_human(state['total'])} {percent}%")

    return callback


def _extract_progress(ticket_id: str):
    state = {"next": 0.0}

    def callback(stats) -> None:
        now = time.monotonic()
        if now < state["next"]:
            return
        state["next"] = now + 1.0
        _render(
            f"解压 {ticket_id} 层={stats.depth} 文件={stats.files} {_human(stats.bytes)}"
        )

    return callback


def _render(text: str) -> None:
    # 只对终端做原地刷新；重定向到文件时保留完整行，便于事后查看。
    if sys.stderr.isatty():
        print(f"\r{text}", end="", file=sys.stderr, flush=True)
    else:
        logger.info("%s", text)


def _clear_line() -> None:
    if sys.stderr.isatty():
        print("\r\033[K", end="", file=sys.stderr, flush=True)


def _human(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB", "GiB", "TiB"):
        if value < 1024 or unit == "TiB":
            return f"{value:.1f}{unit}"
        value /= 1024
    return f"{value:.1f}TiB"


if __name__ == "__main__":
    sys.exit(main())
