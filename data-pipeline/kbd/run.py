"""
data-pipeline/kbd/run.py — KBD 知识生产管道 CLI 入口（API 调用版）

使用方式（在项目根目录下）：

  # 完整流水线（从 Excel 读取所有 ID）
  uv run python -m data-pipeline.kbd.run pipeline --excel

  # 完整流水线（指定 ID 列表）
  uv run python -m data-pipeline.kbd.run pipeline --ids 34977,36179,36166

  # 只跑特定 Stage
  uv run python -m data-pipeline.kbd.run fetch --excel --limit 100
  uv run python -m data-pipeline.kbd.run import --excel
  uv run python -m data-pipeline.kbd.run vision --ids 34977,36179
  uv run python -m data-pipeline.kbd.run classify --excel
  uv run python -m data-pipeline.kbd.run extract-signals --excel
  uv run python -m data-pipeline.kbd.run audit-log-signals --all

  # 从上次中断处继续（断点续传）
  uv run python -m data-pipeline.kbd.run pipeline --excel --resume

  # 仅处理失败的案例
  uv run python -m data-pipeline.kbd.run vision --excel --failed-only

  # 强制重新处理（覆盖已完成的记录）
  uv run python -m data-pipeline.kbd.run pipeline --excel --force-fetch --override

  # SOP 文档导入
  uv run python -m data-pipeline.kbd.import_sop --file /path/to/sop.docx --category-id "虚拟机-001"

  # 查看配置
  uv run python -m data-pipeline.kbd.run config
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import re
import sys
import unicodedata
from datetime import datetime
from pathlib import Path

import httpx

from .config import settings
from .fetcher import read_ids_from_excel
from .observability import install_trace_logging, new_trace_id, set_trace_id
from .pipeline import Stage, run_from_excel
from .runtime import require_shared_contracts
from .terminal_layout import SUMMARY_COLUMN_WIDTHS, TERMINAL_LAYOUT_WIDTH

# ─── 日志配置（终端 + 文件双输出）────────────────────────────────────────────────

_ANSI_RESET = "\033[0m"
_ANSI = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "cyan": "\033[36m",
    "gray": "\033[90m",
    "bold": "\033[1m",
}


def _terminal_color_enabled() -> bool:
    """遵循 NO_COLOR，并允许 KBD_COLOR=always 强制开启终端颜色。"""

    if os.environ.get("NO_COLOR") is not None:
        return False
    if os.environ.get("KBD_COLOR", "auto").lower() == "always":
        return True
    if os.environ.get("KBD_COLOR", "auto").lower() == "never":
        return False
    return bool(getattr(sys.stderr, "isatty", lambda: False)())


def _paint(text: str, color: str, *, enabled: bool | None = None) -> str:
    if enabled is None:
        enabled = _terminal_color_enabled()
    if not enabled:
        return text
    return f"{_ANSI[color]}{text}{_ANSI_RESET}"


def _display_width(text: str) -> int:
    """计算中英文混排的终端显示宽度，避免中文表格列错位。"""

    return sum(2 if unicodedata.east_asian_width(char) in {"W", "F"} else 1 for char in text)


def _pad_display(text: str, width: int, *, align: str = "left") -> str:
    padding = max(0, width - _display_width(text))
    if align == "right":
        return " " * padding + text
    return text + " " * padding


def _center_display(text: str, width: int) -> str:
    """按终端显示列宽居中文本，而不是按 Python 字符数居中。"""

    padding = max(0, width - _display_width(text))
    left = padding // 2
    return " " * left + text + " " * (padding - left)


class _ConsoleFormatter(logging.Formatter):
    """人类可读终端格式：阶段标题整行突出，状态按严重性着色。"""

    _STAGE_BANNER_RE = re.compile(r"^=+\s+Stage\s+\d+:\s+.+\s+=+$")
    _ERROR_WORDS = ("失败", "错误", "异常", "超时", "阻断", "PIPELINE_UNEXPECTED", "STATE_INCONSISTENT")
    _WARNING_WORDS = ("需复核", "warning", "重试", "跳过")
    _SUCCESS_WORDS = ("完成", "成功", "通过", "ready", "done", "全部完成")

    @staticmethod
    def _has_positive_counter(message: str, names: tuple[str, ...]) -> bool:
        pattern = r"(?:" + "|".join(re.escape(name) for name in names) + r")[\"']?\s*[:=]\s*[\"']?([1-9][0-9]*)"
        return re.search(pattern, message, flags=re.IGNORECASE) is not None

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        # 阶段 banner 不带长前缀，直接占一整行，便于快速定位阶段边界。
        if self._STAGE_BANNER_RE.match(message):
            return _paint(message, "cyan")

        rendered = super().format(record)
        has_error_counter = self._has_positive_counter(
            message, ("failed", "error", "blocked", "blocked_by_dependency")
        )
        has_warning_counter = self._has_positive_counter(message, ("needs_review", "warning"))
        if record.levelno >= logging.ERROR or has_error_counter or any(word in message for word in self._ERROR_WORDS):
            return _paint(rendered, "red")
        if record.levelno >= logging.WARNING or has_warning_counter or any(word in message for word in self._WARNING_WORDS):
            return _paint(rendered, "yellow")
        if any(word in message for word in self._SUCCESS_WORDS):
            return _paint(rendered, "green")
        return rendered

class _JsonLineFormatter(logging.Formatter):
    """排障用 JSONL：终端保持中文可读，详细日志可被脚本稳定检索。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S"),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
            "trace_id": getattr(record, "trace_id", None),
        }
        for key in ("run_id", "support_id", "stage", "job_id", "error_code", "retryable"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        return json.dumps(payload, ensure_ascii=False, default=str)


def _setup_logging(run_id: str | None = None, *, verbose: bool = False) -> str:
    """
    配置日志：终端输出 + 文件持久化。

    Args:
        run_id: 可选的 run_id，不传则自动生成（YYYYMMDD_HHMMSS）

    Returns:
        实际使用的 run_id
    """
    if run_id is None:
        run_id = datetime.now().strftime("%Y%m%d_%H%M%S")

    # 安全校验：run_id必须符合 YYYYMMDD_HHMMSS 格式，防止路径穿越
    import re
    if not re.match(r"^\d{8}_\d{6}$", run_id):
        raise ValueError(f"run_id 格式非法: {run_id}，必须为 YYYYMMDD_HHMMSS 格式")

    # 确保 logs 目录存在
    settings.KBD_LOGS_DIR.mkdir(parents=True, exist_ok=True)

    log_path = settings.KBD_LOGS_DIR / f"kbd_{run_id}.log"
    jsonl_path = settings.KBD_LOGS_DIR / f"kbd_{run_id}.jsonl"

    # 配置 root logger，DEBUG 级别以允许 DEBUG 日志通过（Handler 会进一步过滤）
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 清除已有 handlers（避免重复）
    root_logger.handlers.clear()

    # 日志格式（注入 trace_id，便于按 trace 串联 data-pipeline 与 kb-service 日志）
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s [tid=%(trace_id)s] — %(message)s",
        datefmt="%H:%M:%S",
    )
    console_formatter = _ConsoleFormatter(
        "%(asctime)s [%(levelname)s] %(name)s [tid=%(trace_id)s] — %(message)s",
        datefmt="%H:%M:%S",
    )

    # StreamHandler（终端输出，INFO 级别）
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
    stream_handler.setFormatter(console_formatter)
    root_logger.addHandler(stream_handler)

    # FileHandler（文件持久化，DEBUG 级别更详细）
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    jsonl_handler = logging.FileHandler(jsonl_path, encoding="utf-8")
    jsonl_handler.setLevel(logging.INFO)
    jsonl_handler.setFormatter(_JsonLineFormatter())
    root_logger.addHandler(jsonl_handler)

    # httpcore 的连接字节级日志对操作者没有帮助，且会淹没真实失败原因。
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("httpx").setLevel(logging.WARNING)

    # 可观测性：先安装 trace_id 注入过滤器并生成根 trace_id，确保首行日志即带 trace_id
    install_trace_logging()
    trace_id = new_trace_id()
    set_trace_id(trace_id)

    logger = logging.getLogger("kbd.run")
    logger.info(
        "日志初始化完成 run_id=%s trace_id=%s text_log=%s jsonl_log=%s",
        run_id, trace_id, log_path, jsonl_path,
    )

    return run_id


# 模块级别的 logger（用于初始化前的日志）
logger = logging.getLogger("kbd.run")


def _parse_ids(ids_str: str) -> list[str]:
    """解析逗号分隔的 ID 字符串"""
    return [i.strip() for i in ids_str.split(",") if i.strip()]


def _parse_stages(stages_str: str | None) -> list[Stage]:
    """解析 stage 参数，None 时返回全部"""
    if not stages_str:
        return list(Stage)
    stage_map = {
        "fetch": Stage.FETCH,
        "import": Stage.IMPORT,
        "vision": Stage.VISION,
        "classify": Stage.CLASSIFY,
        "extract": Stage.EXTRACT_SIGNALS,
        "extract-signals": Stage.EXTRACT_SIGNALS,
        "audit": Stage.AUDIT_LOG_SIGNALS,
        "audit-signals": Stage.AUDIT_LOG_SIGNALS,
        "audit-log-signals": Stage.AUDIT_LOG_SIGNALS,
        "1": Stage.FETCH,
        "2": Stage.IMPORT,
        "3": Stage.VISION,
        "4": Stage.CLASSIFY,
        "5": Stage.EXTRACT_SIGNALS,
        "6": Stage.AUDIT_LOG_SIGNALS,
    }
    result = []
    for s in stages_str.split(","):
        s = s.strip().lower()
        if s not in stage_map:
            print(
                f"未知 stage: {s}，合法值："
                "fetch,import,vision,classify,extract-signals,audit-log-signals"
            )
            sys.exit(1)
        result.append(stage_map[s])
    return result


def _get_kbd_ids(args: argparse.Namespace) -> list[str]:
    """从参数中解析案例 ID 列表"""
    if args.excel:
        ids = read_ids_from_excel()
        if args.limit:
            ids = ids[: args.limit]
        print(f"从 Excel 读取 {len(ids)} 个案例 ID")
        return ids
    elif args.ids:
        return _parse_ids(args.ids)
    elif args.id_file:
        p = Path(args.id_file)
        ids = [line.strip() for line in p.read_text().splitlines() if line.strip().isdigit()]
        print(f"从文件 {p} 读取 {len(ids)} 个案例 ID")
        if args.limit:
            ids = ids[: args.limit]
        return ids
    else:
        print("错误：需要提供 --excel、--ids 或 --id-file 之一")
        sys.exit(1)


async def _cmd_pipeline(args: argparse.Namespace, run_id: str) -> int:
    """执行完整流水线（或指定 stages）"""
    stages = _parse_stages(getattr(args, "stages", None))

    # 解析 override_status 参数（逗号分隔的字符串 → list）
    override_status = None
    if args.override_status:
        override_status = [s.strip() for s in args.override_status.split(",")]

    # 处理 resume 参数
    resume = getattr(args, "resume", False)
    resume_run_id = getattr(args, "resume_run_id", None)
    failed_only = getattr(args, "failed_only", False)

    if args.excel and not args.ids and not args.id_file:
        stats, actual_run_id = await run_from_excel(
            stages=stages,
            force_fetch=args.force_fetch,
            override=args.override,
            override_status=override_status,
            limit=args.limit,
            resume=resume,
            resume_run_id=resume_run_id,
            failed_only=failed_only,
            run_id=run_id,
        )
    else:
        kbd_ids = _get_kbd_ids(args)
        from .pipeline import run_pipeline
        stats, actual_run_id = await run_pipeline(
            kbd_ids,
            stages=stages,
            force_fetch=args.force_fetch,
            override=args.override,
            override_status=override_status,
            resume=resume,
            resume_run_id=resume_run_id,
            failed_only=failed_only,
            run_id=run_id,
        )
    if getattr(args, "json", False):
        print(json.dumps({"run_id": actual_run_id, "stats": stats}, ensure_ascii=False, indent=2))
    elif not getattr(args, "quiet", False):
        _print_pipeline_summary(actual_run_id, stats)
    return 0 if stats.get("pipeline", {}).get("success", True) else 1


def _print_pipeline_summary(run_id: str, stats: dict) -> None:
    """面向操作者的对齐摘要；完整细节在 JSONL 与 progress 文件中。"""

    pipeline = stats.get("pipeline", {})
    success = bool(pipeline.get("success"))
    total = pipeline.get("total_ids", 0)
    completed = pipeline.get("completed_ids", 0)
    enabled = _terminal_color_enabled()

    stage_rows = (
        ("fetch", "数据抓取"),
        ("import", "语义导入"),
        ("vision", "截图识别"),
        ("classify", "案例分类"),
        ("extract", "关键信号抽取"),
        ("audit_log_signals", "日志信号审计"),
    )
    headers = ("阶段", "状态", "完成", "失败", "跳过", "需复核", "前置阻断", "耗时")
    aligns = ("left", "left", "right", "right", "right", "right", "right", "right")
    summary_rows: list[tuple[list[str], str | None]] = []
    for stage_name, label in stage_rows:
        item = stats.get(stage_name)
        if not item:
            continue
        if stage_name == "import":
            done = sum(int(item.get(key, 0) or 0) for key in ("created", "overridden"))
            skipped = int(item.get("skipped", 0) or 0)
            failed = int(item.get("error", item.get("failed", 0)) or 0)
        elif stage_name == "audit_log_signals":
            done = int(item.get("case_count", 0) or 0)
            skipped = 0
            failed = int(item.get("issue_counts", {}).get("BLOCKED_ACTIVE_SIGNAL", 0) or 0)
        else:
            done = int(item.get("done", 0) or 0)
            failed = int(item.get("failed", 0) or 0)
            skipped = int(item.get("skipped", 0) or 0)
        audit_issues = 0
        needs_review = int(item.get("needs_review", item.get("low_confidence", 0)) or 0)
        if stage_name == "audit_log_signals":
            needs_review = int(item.get("case_status_counts", {}).get("NEEDS_EXPERT_REVIEW", 0) or 0)
            audit_issues = sum(int(value or 0) for value in item.get("issue_counts", {}).values())
        if stage_name == "vision":
            needs_review = int(item.get("case_status_counts", {}).get("needs_review", needs_review) or 0)
        blocked = int(item.get("blocked_by_dependency", 0) or 0)
        elapsed = item.get("elapsed_s")
        elapsed_text = f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "-"
        if failed or blocked:
            status = "失败/阻断"
            status_color = "red"
        elif needs_review or audit_issues:
            status = "需复核"
            status_color = "yellow"
        else:
            status = "完成"
            status_color = "green"
        summary_rows.append(
            (
                [
                    label,
                    status,
                    str(done),
                    str(failed),
                    str(skipped),
                    str(needs_review),
                    str(blocked),
                    elapsed_text,
                ],
                status_color,
            )
        )

    minimum_widths = SUMMARY_COLUMN_WIDTHS
    columns = [list(headers)] + [row for row, _color in summary_rows]
    widths = [
        max(minimum_widths[index], max(_display_width(row[index]) for row in columns) + 2)
        for index in range(len(headers))
    ]
    table_width = sum(widths) + len(widths) + 1
    if table_width != TERMINAL_LAYOUT_WIDTH:
        raise RuntimeError(
            "终端布局宽度契约不一致：摘要宽度 "
            f"{table_width} != Stage Banner 宽度 {TERMINAL_LAYOUT_WIDTH}"
        )

    def border(left: str, middle: str, right: str, fill: str = "─") -> str:
        return left + middle.join(fill * width for width in widths) + right

    def row_text(values: list[str], color: str | None = None) -> str:
        cells = []
        for index, value in enumerate(values):
            cell = f" {_pad_display(value, widths[index] - 2, align=aligns[index])} "
            cells.append(_paint(cell, color, enabled=enabled) if index == 1 and color else cell)
        return "│" + "│".join(cells) + "│"

    print("\n" + "=" * table_width)
    print(_paint(_center_display("KBD 流水线完成摘要", table_width), "bold", enabled=enabled))
    print("=" * table_width)
    print(f"运行编号   : {run_id}")
    result_text = "全部阶段完成" if success else "部分完成，请查看失败/阻断项"
    result_color = "green" if success else "red"
    print(f"总体结果   : {_paint(result_text, result_color, enabled=enabled)}")
    print(f"KBD 完成数 : {completed}/{total}")
    print(border("┌", "┬", "┐"))
    print(row_text(list(headers)))
    print(border("├", "┼", "┤"))
    for values, color in summary_rows:
        print(row_text(values, color))
    print(border("└", "┴", "┘"))

    if stats.get("vision", {}).get("case_status_counts"):
        counts = stats["vision"]["case_status_counts"]
        print("\nVision KBD 状态：" + " / ".join(f"{key}={value}" for key, value in counts.items()))
    if stats.get("audit_log_signals", {}).get("issue_counts"):
        issues = stats["audit_log_signals"]["issue_counts"]
        print("日志审计问题：" + " / ".join(f"{key}={value}" for key, value in issues.items()))
    if not success:
        print(_paint("建议：技术失败项使用 --resume 或 --failed-only 重试；前置阻断项必须先修复上游阶段。", "yellow", enabled=enabled))
    print(f"排障日志   : {settings.KBD_LOGS_DIR / f'kbd_{run_id}.jsonl'}")


async def _cmd_cli(args: argparse.Namespace, run_id: str) -> int:
    """交互式 CLI；Typical 保守运行，Custom 显式确认高风险参数。"""
    if not sys.stdin.isatty():
        print("错误：cli 需要交互终端；自动化请使用 pipeline --ids/--id-file --json。")
        return 3
    print("KBD 交互式 CLI")
    print("将执行：抓取 → 导入 →（截图识别与分类并行）→ 关键信号抽取 → 审计")
    print(f"目标 kb-service：{settings.KB_SERVICE_URL}")
    print(f"数据库：{'已配置' if settings.DATABASE_URL else '未配置'}")
    print(f"内部 Token：{'已配置' if settings.INTERNAL_API_TOKEN else '未配置'}")
    if not settings.INTERNAL_API_TOKEN:
        print("错误：内部 Token 未配置，已停止。请先设置 INTERNAL_API_TOKEN。")
        return 4
    ids_text = input("请输入 KBD 案例 ID（逗号分隔）：").strip()
    ids = _parse_ids(ids_text)
    if not ids:
        print("未输入有效案例 ID，已取消。")
        return 3

    options = _cli_options()
    print(f"本次将处理 {len(ids)} 个 KBD。截图识别成功与分类成功均为信号抽取的硬前置条件。")
    print("\n本次实际运行参数（后端 run_pipeline）：")
    print("  参数             当前值       中文含义")
    print("  " + "-" * 96)
    for key, value in options.items():
        print(f"  {key:<16}= {str(value):<11}  {_CLI_OPTION_DESCRIPTIONS[key]}")
    print("确认的意义：最后一次显式确认，防止在参数选定后误触发抓取、写库和 LLM 调用。")
    if not _prompt_yes_no("确认开始？", default=False):
        print("已取消，未执行任何处理。")
        return 130
    from .pipeline import run_pipeline
    stats, actual_run_id = await run_pipeline(ids, run_id=run_id, **options)
    _print_pipeline_summary(actual_run_id, stats)
    return 0 if stats.get("pipeline", {}).get("success", True) else 1


def _prompt_yes_no(prompt: str, *, default: bool) -> bool:
    """标准 yes/no 提示：支持 y/yes/n/no（大小写不敏感），空输入取默认值。"""

    yes = {"y", "yes"}
    no = {"n", "no"}
    suffix = "[Y/n]" if default else "[y/N]"
    while True:
        value = input(f"{prompt} {suffix}: ").strip().lower()
        if not value:
            return default
        if value in yes:
            return True
        if value in no:
            return False
        print("请输入 y/yes 或 n/no；直接回车采用默认值。")


def _prompt_choice(prompt: str, choices: tuple[tuple[str, str, str], ...], *, default: str) -> str:
    """标准编号选择：只接受列出的序号，空输入采用默认序号。"""

    choice_map = {number: value for number, value, _label in choices}
    while True:
        print(prompt)
        for number, _value, label in choices:
            print(f"  {number}) {label}")
        raw = input(f"请选择 [{default}]: ").strip()
        selected = raw or default
        if selected in choice_map:
            return choice_map[selected]
        valid = "/".join(choice_map)
        print(f"请输入选项序号（{valid}）。")


def _cli_options() -> dict[str, object]:
    """收集交互 CLI 参数，返回可直接传入 run_pipeline 的参数字典。"""

    mode = _prompt_choice(
        "请选择运行模式：",
        (
            ("1", "typical", "默认 Typical（推荐）：保守运行，不强制抓取、不覆盖已有记录"),
            ("2", "custom", "自定义 Custom：逐项确认运行参数"),
        ),
        default="1",
    )
    if mode == "typical":
        options: dict[str, object] = {
            "force_fetch": False,
            "override": False,
            "override_status": None,
            "resume": False,
            "failed_only": False,
        }
        print("已选择默认 Typical：不会强制重新抓取，也不会覆盖已有 KBD。")
        return options
    force_fetch = _prompt_yes_no("强制重新抓取已有缓存？", default=False)
    override = _prompt_yes_no("覆盖已存在的 KBD？", default=False)
    override_status: list[str] | None = None
    if override:
        scope = _prompt_choice(
            "请选择覆盖范围：",
            (
                ("1", "draft", "仅 draft（推荐）"),
                ("2", "all", "所有状态（包含 published，高风险）"),
            ),
            default="1",
        )
        if scope == "draft":
            override_status = ["draft"]
        else:
            print("警告：所有状态包含 published，可能覆盖专家已审核内容。")
            if not _prompt_yes_no("确认允许覆盖所有状态？", default=False):
                override = False
                override_status = None

    resume = _prompt_yes_no("从数据库现状续跑并跳过已完成项？", default=False)
    failed_only = _prompt_yes_no("仅处理抓取/Vision 失败项？", default=False)
    if resume and failed_only:
        print("提示：同时启用续跑和失败筛选时，将先筛选失败项，再按数据库状态跳过已完成项。")
    return {
        "force_fetch": force_fetch,
        "override": override,
        "override_status": override_status,
        "resume": resume,
        "failed_only": failed_only,
    }


_CLI_OPTION_DESCRIPTIONS = {
    "force_fetch": (
        "是否忽略本地 cache，重新从 Support Portal 抓取原文和图片；"
        "False=复用有效缓存，True=只重抓 Stage 1，不会自动覆盖数据库。"
    ),
    "override": (
        "是否覆盖已经存在的 KBD 导入记录；False=保护性跳过已有记录，"
        "True=允许 Stage 2 写入新的 Proposal。"
    ),
    "override_status": (
        "允许覆盖的状态范围；None=后端默认仅 draft，['draft']=仅草稿，"
        "['all']=包含 published，属于高风险覆盖。"
    ),
    "resume": (
        "是否按数据库现状续跑；False=按本次输入执行各阶段，"
        "True=跳过数据库已经完成的阶段，progress 文件只用于观察。"
    ),
    "failed_only": (
        "是否只筛选 Fetch/Vision 自动识别出的失败或可重试案例；"
        "False=处理全部输入 ID，True=用于故障重试，不会把普通成功案例重复送入 LLM。"
    ),
}


async def _cmd_fetch(args: argparse.Namespace, run_id: str) -> None:
    """Stage 1：抓取（文件存储，不依赖数据库）"""
    from .fetcher import fetch_batch

    kbd_ids = _get_kbd_ids(args)

    # --failed-only 参数：仅处理抓取失败的案例
    failed_only = getattr(args, "failed_only", False)
    if failed_only:
        from .fetcher import get_failed_fetch_ids
        logger.info("--failed-only 模式：筛选 Fetch 失败案例")
        kbd_ids = get_failed_fetch_ids(kbd_ids)
        if not kbd_ids:
            print("没有抓取失败的案例需要处理")
            return

    logger.info("Fetch 处理开始 kbds=%d run_id=%s", len(kbd_ids), run_id)
    stats = await fetch_batch(kbd_ids, force=args.force)
    print(f"run_id: {run_id}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


async def _cmd_vision(args: argparse.Namespace, run_id: str) -> None:
    """Stage 3：图片语义化"""
    import asyncpg

    from .image_proc import process_images_batch
    from .pipeline import _db_failed_vision_ids

    kbd_ids = _get_kbd_ids(args)

    # --failed-only 参数：仅处理失败的案例
    failed_only = getattr(args, "failed_only", False)

    pool = await asyncpg.create_pool(dsn=settings.asyncpg_database_url)
    try:
        if failed_only:
            logger.info("--failed-only 模式：筛选 Vision 失败案例")
            kbd_ids = await _db_failed_vision_ids(kbd_ids, pool)
            if not kbd_ids:
                print("没有 Vision 失败的案例需要处理")
                return

        # 检查已抓取的案例
        from .fetcher import _is_fetched
        ready_ids = [cid for cid in kbd_ids if _is_fetched(cid)]

        if not ready_ids:
            print("没有已抓取的案例需要处理")
            return

        logger.info("Vision 处理开始 kbds=%d run_id=%s", len(ready_ids), run_id)

        stats = await process_images_batch(ready_ids, pool)
        print(f"run_id: {run_id}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_import(args: argparse.Namespace, run_id: str) -> int:
    """Stage 2：语义提取 + 原子入库（kbd_entry + kbd_image）

    新架构：仅检查 FETCH 完成即可入库；图片随 IMPORT 原子写入 kbd_image，
    content_md 交由后端 rebuild_content_md 统一渲染（样式高一致）。
    解除旧架构下 "IMPORT 需要 .desc.txt" 的循环依赖（.desc.txt 机制已彻底移除）。
    """
    from .importer import import_batch

    kbd_ids = _get_kbd_ids(args)

    if not settings.INTERNAL_API_TOKEN:
        print("错误：INTERNAL_API_TOKEN 未配置")
        print("请在环境变量或 .env 文件中设置 INTERNAL_API_TOKEN")
        sys.exit(1)

    from .fetcher import _is_fetched

    # 仅检查 FETCH 完成即可入库（图片原子写入 kbd_image，无需 .desc.txt 前置；该机制已移除）
    ready_ids: list[str] = []
    for support_id in kbd_ids:
        if not _is_fetched(support_id):
            continue
        ready_ids.append(support_id)

    if not ready_ids:
        print("没有已准备好可导入的案例")
        return 1

    # 解析 override_status 参数（逗号分隔的字符串 → list）
    override_status = None
    if args.override_status:
        override_status = [s.strip() for s in args.override_status.split(",")]

    logger.info("Import 处理开始 kbds=%d run_id=%s", len(ready_ids), run_id)
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
        stats = await import_batch(
            ready_ids,
            None,
            override=args.override,
            override_status=override_status,
            client=client,
        )
        print(f"run_id: {run_id}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
        return 1 if stats.get("error", 0) else 0


async def _cmd_classify(args: argparse.Namespace, run_id: str) -> None:
    """Stage 4：AI 分类（通过 API）"""
    import asyncpg

    from .classifier import classify_batch

    kbd_ids = _get_kbd_ids(args)

    if not settings.INTERNAL_API_TOKEN:
        print("错误：INTERNAL_API_TOKEN 未配置")
        print("请在环境变量或 .env 文件中设置 INTERNAL_API_TOKEN")
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn=settings.asyncpg_database_url)
    try:
        # 只处理已入库且未分类的
        classify_ids = await pool.fetch(
            """SELECT support_id FROM kbd_entry
               WHERE support_id = ANY($1)
                 AND status = 'draft'
                 AND (ai_category_id IS NULL OR ai_category_id = '')""",
            kbd_ids,
        )
        classify_kbd_ids = [r["support_id"] for r in classify_ids]

        if not classify_kbd_ids:
            print("没有需要分类的案例")
            return

        logger.info("Classify 处理开始 kbds=%d run_id=%s", len(classify_kbd_ids), run_id)
        stats = await classify_batch(classify_kbd_ids, pool)
        print(f"run_id: {run_id}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_extract_signals(args: argparse.Namespace, run_id: str) -> None:
    """Stage 5：对已分类、尚无 Proposal 的 draft KBD 抽取关键信号。"""

    import asyncpg

    from .extract_signals import extract_signals_batch

    if not settings.INTERNAL_API_TOKEN:
        print("错误：INTERNAL_API_TOKEN 未配置")
        sys.exit(1)

    kbd_ids = _get_kbd_ids(args)
    pool = await asyncpg.create_pool(dsn=settings.asyncpg_database_url)
    try:
        rows = await pool.fetch(
            """SELECT support_id
               FROM kbd_entry
               WHERE support_id = ANY($1)
                 AND status = 'draft'
                 AND (COALESCE(category_id, '') <> '' OR COALESCE(ai_category_id, '') <> '')
                 AND (signals_json IS NULL OR signals_json = '[]'::jsonb)
               ORDER BY support_id""",
            kbd_ids,
        )
        extract_ids = [str(row["support_id"]) for row in rows]
        skipped = len(kbd_ids) - len(extract_ids)
        if not extract_ids:
            print("没有可抽取的案例：仅处理已分类、signals_json 为空的 draft KBD")
            return

        logger.info(
            "Extract-signals 处理开始 kbds=%d skipped=%d run_id=%s",
            len(extract_ids),
            skipped,
            run_id,
        )
        stats = await extract_signals_batch(extract_ids, pool)
        stats["skipped_by_precondition"] = skipped
        print(f"run_id: {run_id}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_audit_log_signals(args: argparse.Namespace, run_id: str) -> int:
    """Stage 6：从文件、stdin 或数据库只读审计 qfk_log Proposal。"""

    from .log_signal_audit import (
        audit_rows,
        dump_report,
        load_rows,
        load_rows_file,
        load_rows_from_db,
    )

    if args.stdin:
        rows = load_rows(sys.stdin)
        source = "stdin"
    elif args.file:
        source_path = Path(args.file)
        rows = load_rows_file(source_path)
        source = str(source_path)
    else:
        import asyncpg

        support_ids = None if args.all else _get_kbd_ids(args)
        pool = await asyncpg.create_pool(dsn=settings.asyncpg_database_url)
        try:
            rows = await load_rows_from_db(pool, support_ids)
        finally:
            await pool.close()
        source = "database:all" if args.all else "database:selected"

    report = audit_rows(rows)
    logger.info(
        "Audit-log-signals 完成 source=%s cases=%d status=%s issues=%s run_id=%s",
        source,
        report["case_count"],
        report["case_status_counts"],
        report["issue_counts"],
        run_id,
    )

    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as output:
            dump_report(report, output)
        print(
            json.dumps(
                {
                    "run_id": run_id,
                    "source": source,
                    "report": str(output_path),
                    "case_count": report["case_count"],
                    "case_status_counts": report["case_status_counts"],
                    "issue_counts": report["issue_counts"],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        dump_report(report, sys.stdout)

    blocked = int(report["case_status_counts"].get("BLOCKED_ACTIVE_SIGNAL", 0))
    return 1 if args.fail_on_blocked and blocked else 0


async def _cmd_review_list(args: argparse.Namespace, run_id: str) -> None:
    """列出待审核案例（调用 admin-service API）"""
    if not settings.INTERNAL_API_TOKEN:
        print("错误：INTERNAL_API_TOKEN 未配置")
        sys.exit(1)

    logger.info("Review-list 处理开始 run_id=%s", run_id)
    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
        url = f"{settings.KB_SERVICE_URL}/api/admin/kb/pending"
        headers = {
            "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        }

        try:
            response = await client.get(url, headers=headers, params={"limit": args.limit or 50})
            response.raise_for_status()
            data = response.json()

            items = data.get("items", [])
            print(f"待审核案例（共 {len(items)} 条）：")
            for item in items:
                conf = item.get("ai_category_conf")
                cat = item.get("ai_category_label") or item.get("ai_category_id") or "未分类"
                conf_str = f"{conf:.2f}" if conf is not None else "N/A"
                print(f"  {item.get('kbd_id')} | {item.get('title', '')[:40]} | {cat} (置信度: {conf_str})")

        except Exception as exc:
            print(f"获取待审核列表失败: {exc}")
            print("提示：确保 admin-service API 可用且 INTERNAL_API_TOKEN 已配置")


def _cmd_config(_args: argparse.Namespace) -> None:
    """打印当前配置（隐藏敏感信息）"""
    cfg = settings.model_dump()
    # 隐藏敏感字段
    for key in ("SANGFOR_COOKIE", "DATABASE_URL", "INTERNAL_API_TOKEN"):
        if key in cfg and cfg[key]:
            cfg[key] = cfg[key][:8] + "****"
    print(json.dumps({k: str(v) for k, v in cfg.items()}, ensure_ascii=False, indent=2))


# ─── 参数解析 ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data-pipeline.kbd.run",
        description="KBD 知识生产管道",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    # 公共参数
    def _add_common(p: argparse.ArgumentParser) -> None:
        group = p.add_mutually_exclusive_group()
        group.add_argument("--excel", action="store_true", help="从 Excel 读取全量 ID")
        group.add_argument("--ids", help="逗号分隔的案例 ID，如 34977,36179")
        group.add_argument("--id-file", help="每行一个 ID 的文本文件路径")
        p.add_argument("--limit", type=int, default=None, help="最多处理 N 条（测试用）")
        p.add_argument("--verbose", action="store_true", help="终端显示详细调试日志（默认仅显示阶段事件）")

    # pipeline 子命令
    p_pipeline = sub.add_parser("pipeline", help="运行完整流水线（或指定 stages）")
    _add_common(p_pipeline)
    p_pipeline.add_argument(
        "--stages",
        help=(
            "指定 stages（逗号分隔）：fetch,import,vision,classify,"
            "extract-signals,audit-log-signals"
        ),
    )
    # 抓取阶段参数
    p_pipeline.add_argument(
        "--force-fetch",
        action="store_true",
        help="强制重新抓取已完成的案例（仅影响 Stage 1）",
    )
    # 导入阶段参数
    p_pipeline.add_argument(
        "--override",
        action="store_true",
        help="强制覆盖已存在的记录（仅影响 Stage 2 导入阶段）",
    )
    p_pipeline.add_argument(
        "--override-status",
        type=str,
        default=None,
        help=(
            "仅覆盖指定状态的记录（逗号分隔）。"
            "不传=默认仅draft；'all'=所有状态；'draft,published'=仅指定状态"
        ),
    )
    # 进度追踪参数
    p_pipeline.add_argument(
        "--resume",
        action="store_true",
        help="从上次中断处继续，自动跳过已完成的案例",
    )
    p_pipeline.add_argument(
        "--resume-run-id",
        type=str,
        default=None,
        help="指定要恢复的 run_id（不传则自动查找最新的 progress 文件）",
    )
    p_pipeline.add_argument(
        "--failed-only",
        action="store_true",
        help="仅处理失败的案例（有 .failed 标记或识别为无文字）",
    )
    p_pipeline.add_argument("--json", action="store_true", help="输出机器可读 JSON（供 CI/自动化使用）")
    p_pipeline.add_argument("--quiet", action="store_true", help="不输出中文终端摘要，仅保留日志文件")

    # 交互 CLI：不隐式执行危险覆盖操作，输入与确认都必须来自 TTY。
    sub.add_parser("cli", help="交互式 KBD 生产 CLI（适合人工操作）")

    # 单独 stage 子命令
    for name, help_text in [
        ("fetch",    "Stage 1：抓取 API + 下载图片"),
        ("import",   "Stage 2：语义转换 + 原子入库"),
        ("vision",   "Stage 3：图片语义化（Vision LLM）"),
        ("classify", "Stage 4：AI 分类（调用 kb-service API）"),
    ]:
        p_sub = sub.add_parser(name, help=help_text)
        _add_common(p_sub)
        # fetch 子命令的 force 参数
        if name == "fetch":
            p_sub.add_argument(
                "--force",
                action="store_true",
                help="强制重新抓取已完成的案例",
            )
            p_sub.add_argument(
                "--resume",
                action="store_true",
                help="从上次中断处继续",
            )
            p_sub.add_argument(
                "--resume-run-id",
                type=str,
                default=None,
                help="指定要恢复的 run_id",
            )
            p_sub.add_argument(
                "--failed-only",
                action="store_true",
                help="仅处理抓取失败的案例",
            )
        # vision 子命令的 failed-only 参数
        if name == "vision":
            p_sub.add_argument(
                "--failed-only",
                action="store_true",
                help="仅处理 Vision 失败的案例（.desc.failed 或识别为无文字）",
            )
        # import 子命令的 override 参数
        if name == "import":
            p_sub.add_argument(
                "--override",
                action="store_true",
                help="强制覆盖已存在的记录",
            )
            p_sub.add_argument(
                "--override-status",
                type=str,
                default=None,
                help=(
                    "仅覆盖指定状态的记录（逗号分隔）。"
                    "不传=默认仅draft；'all'=所有状态；'draft,published'=仅指定状态"
                ),
            )

    # Stage 5：独立关键信号抽取。别名 extract 兼容既有 stage 口径。
    p_extract = sub.add_parser(
        "extract-signals",
        aliases=["extract"],
        help="Stage 5：抽取关键信号 Proposal",
    )
    _add_common(p_extract)

    # Stage 6：日志 Proposal 契约审计。输入源互斥，默认不因 Proposal 问题返回非零。
    p_audit = sub.add_parser(
        "audit-log-signals",
        aliases=["audit-signals", "audit"],
        help="Stage 6：只读审计 qfk_log Proposal 与运行时契约",
    )
    audit_source = p_audit.add_mutually_exclusive_group(required=True)
    audit_source.add_argument("--stdin", action="store_true", help="从标准输入读取 JSON 数组")
    audit_source.add_argument("--file", help="从 UTF-8 JSON 文件读取数组")
    audit_source.add_argument("--all", action="store_true", help="只读审计数据库全部 KBD")
    audit_source.add_argument("--excel", action="store_true", help="按 Excel 中的案例 ID 查询数据库")
    audit_source.add_argument("--ids", help="按逗号分隔的 support_id 查询数据库")
    audit_source.add_argument("--id-file", help="按每行一个 support_id 的文件查询数据库")
    p_audit.add_argument("--limit", type=int, default=None, help="限制 Excel/ID 文件输入数量")
    p_audit.add_argument("--output", help="将完整 JSON 报告写入文件；终端仅打印摘要")
    p_audit.add_argument(
        "--fail-on-blocked",
        action="store_true",
        help="存在 BLOCKED_ACTIVE_SIGNAL 时返回 1，供 CI 门禁使用",
    )

    # review-list 子命令
    p_review = sub.add_parser("review-list", help="列出待审核案例")
    p_review.add_argument("--limit", type=int, default=50)

    # config 子命令
    sub.add_parser("config", help="打印当前配置")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # 完整 pipeline 默认包含 Stage 6；在任何 fetch/import/LLM 调用前确认其
    # 唯一外部源码依赖可用，避免跑完前五阶段后才因 shared 导入失败。
    requires_shared_contracts = args.command in {"audit-log-signals", "audit-signals", "audit", "cli"}
    if args.command == "pipeline":
        requires_shared_contracts = Stage.AUDIT_LOG_SIGNALS in _parse_stages(args.stages)
    if requires_shared_contracts:
        try:
            require_shared_contracts()
        except RuntimeError as exc:
            parser.error(str(exc))

    cmd_map = {
        "pipeline":    _cmd_pipeline,
        "cli":          _cmd_cli,
        "fetch":       _cmd_fetch,
        "vision":      _cmd_vision,
        "import":      _cmd_import,
        "classify":    _cmd_classify,
        "extract-signals": _cmd_extract_signals,
        "extract":     _cmd_extract_signals,
        "audit-log-signals": _cmd_audit_log_signals,
        "audit-signals": _cmd_audit_log_signals,
        "audit":       _cmd_audit_log_signals,
        "review-list": _cmd_review_list,
        "config":      lambda a: (_cmd_config(a), None)[1],
    }

    cmd = cmd_map.get(args.command)
    if cmd is None:
        parser.print_help()
        sys.exit(1)

    # config 命令不需要日志初始化
    if args.command == "config":
        _cmd_config(args)
        return

    # 初始化日志（终端 + 文本文件 + JSONL 排障文件）
    # 如果有 --resume-run-id 参数，使用它；否则自动生成
    resume_run_id = getattr(args, "resume_run_id", None)
    run_id = _setup_logging(resume_run_id, verbose=getattr(args, "verbose", False))

    # 执行异步命令，传递 run_id
    exit_code = asyncio.run(cmd(args, run_id))
    if isinstance(exit_code, int) and exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
