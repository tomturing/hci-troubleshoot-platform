"""
data-pipeline/kbd/run.py — KBD 知识生产管道 CLI 入口（API 调用版）

统一任务入口（在项目根目录下）：

  uv run python -m data-pipeline.kbd.run task --ids 34977 --stages all
  uv run python -m data-pipeline.kbd.run cli
  uv run python -m data-pipeline.kbd.run task --excel --stages vision --resume
  uv run python -m data-pipeline.kbd.run task --run-id 20260809_103000 --failed

无模式参数执行未完成和失败任务；`--resume` 仅未完成，`--failed` 仅失败，
`--rework[=draft,published]` 重做指定生命周期状态。三者严格互斥。

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
from .observability import (
    get_trace_id,
    install_trace_logging,
    new_trace_id,
    set_run_id,
    set_trace_id,
)
from .pipeline import EMPTY_SIGNALS_JSON_PREDICATE, Stage, run_from_excel
from .progress import load_progress
from .runtime import require_shared_contracts
from .task_manager import (
    ALL_STAGE_NAMES,
    REWORK_STATUS_NAMES,
    TaskMode,
    parse_requested_stage_names,
    parse_rework_statuses,
    parse_stage_names,
    parse_task_mode,
    select_task_plan,
    stage_cli_name,
)
from .task_state import (
    load_execution_manifest,
    load_state,
    merge_run_progress,
    save_execution_manifest,
    save_state,
)
from .terminal_layout import SUMMARY_COLUMN_WIDTHS, TERMINAL_LAYOUT_WIDTH

# ─── 日志配置（终端 + 文件双输出）────────────────────────────────────────────────

_ANSI_RESET = "\033[0m"
_ANSI = {
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[94m",
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
    _SUCCESS_WORDS = ("完成", "成功", "通过", "ready", "done", "全部完成")

    @staticmethod
    def _has_positive_counter(message: str, names: tuple[str, ...]) -> bool:
        pattern = r"(?:" + "|".join(re.escape(name) for name in names) + r")[\"']?\s*[:=]\s*[\"']?([1-9][0-9]*)"
        return re.search(pattern, message, flags=re.IGNORECASE) is not None

    def format(self, record: logging.LogRecord) -> str:
        message = record.getMessage()
        # 阶段 banner 不带长前缀，直接占一整行，便于快速定位阶段边界。
        if self._STAGE_BANNER_RE.match(message):
            # Stage Banner 与完成摘要共享同一亮蓝色视觉语言。
            return _paint(message, "blue")

        rendered = super().format(record)
        has_error_counter = self._has_positive_counter(
            message, ("failed", "error", "blocked", "blocked_by_dependency")
        )
        has_warning_counter = self._has_positive_counter(message, ("needs_review", "warning"))
        # 颜色必须由日志级别或结构化计数决定，不能扫描任意中文词。比如
        # “重做策略：前置阶段会阻断目标阶段”是正常的 INFO 计划说明，不能被
        # 其中的“阻断”误判成错误。
        if record.levelno >= logging.ERROR or has_error_counter:
            return _paint(rendered, "red")
        if record.levelno >= logging.WARNING or has_warning_counter:
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
            "run_id": getattr(record, "run_id", None),
        }
        for key in (
            "run_id", "support_id", "stage", "job_id", "error_code", "retryable",
            "error_detail",
        ):
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
        "%(asctime)s [%(levelname)s] %(name)s [run=%(run_id)s tid=%(trace_id)s] — %(message)s",
        datefmt="%H:%M:%S",
    )
    console_formatter = _ConsoleFormatter(
        "%(asctime)s [%(levelname)s] %(name)s [run=%(run_id)s tid=%(trace_id)s] — %(message)s",
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
    set_run_id(run_id)

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
        "extract-signals": Stage.EXTRACT_SIGNALS,
        "review-signals": Stage.REVIEW_SIGNALS,
        "1": Stage.FETCH,
        "2": Stage.IMPORT,
        "3": Stage.CLASSIFY,
        "4": Stage.VISION,
        "5": Stage.EXTRACT_SIGNALS,
        "6": Stage.REVIEW_SIGNALS,
    }
    result = []
    for s in stages_str.split(","):
        s = s.strip().lower()
        if s not in stage_map:
            print(
                f"未知 stage: {s}，合法值："
                "fetch,import,vision,classify,extract-signals,review-signals"
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


def _get_task_ids(args: argparse.Namespace) -> list[str]:
    """解析统一任务范围；``--run-id`` 从历史任务 manifest 读取 ID。"""

    limit = getattr(args, "limit", None)
    if limit is not None and limit <= 0:
        raise ValueError("--limit 必须是正整数")
    if getattr(args, "run_id", None):
        manifest = load_execution_manifest(args.run_id)
        if manifest is None:
            raise ValueError(f"任务 run_id 不存在或没有不可变 manifest: {args.run_id}")
        ids = [str(value) for value in manifest.get("requested_ids", [])]
        return ids[:limit] if limit is not None else ids
    if getattr(args, "excel", False):
        excel_file = getattr(args, "excel_file", None)
        ids = read_ids_from_excel(Path(excel_file)) if excel_file else read_ids_from_excel()
    elif getattr(args, "ids", None):
        raw_ids = [item.strip() for item in args.ids.split(",") if item.strip()]
        invalid = [item for item in raw_ids if not item.isdigit()]
        if invalid:
            raise ValueError(f"案例 ID 必须是数字: {', '.join(invalid)}")
        ids = raw_ids
    elif getattr(args, "id_file", None):
        path = Path(args.id_file)
        ids = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
        invalid = [item for item in ids if not item.isdigit()]
        if invalid:
            raise ValueError(f"ID 文件包含非法案例 ID: {', '.join(invalid)}")
    else:
        raise ValueError("需要提供 --ids、--excel、--id-file 或 --run-id 之一")
    # 保持输入顺序并去重，避免同一任务在一次运行中被重复调度。
    unique_ids = list(dict.fromkeys(ids))
    return unique_ids[:limit] if limit is not None else unique_ids


def _build_task_plan(args: argparse.Namespace) -> tuple[TaskMode, tuple[str, ...], list[str], dict[Stage, list[str]]]:
    """生成可审计的任务执行计划。"""

    rework_requested = getattr(args, "rework", None) is not None
    mode = parse_task_mode(
        resume=bool(getattr(args, "resume", False)),
        failed=bool(getattr(args, "failed", False)),
        rework=rework_requested,
    )
    # 先验证重做状态，即使本次不是 rework 也拒绝误传非法值。
    if rework_requested:
        parse_rework_statuses(args.rework)
    stage_spec = getattr(args, "stages", None) or getattr(args, "stage", None)
    requested_stages = parse_requested_stage_names(stage_spec)
    stages = parse_stage_names(stage_spec)
    ids = _get_task_ids(args)
    states = load_state()
    if getattr(args, "run_id", None):
        manifest = load_execution_manifest(args.run_id)
        if manifest is None:
            raise ValueError(f"任务 run_id 不存在或没有不可变 manifest: {args.run_id}")
    plan = select_task_plan(
        ids,
        requested_stages=requested_stages,
        resolved_stages=stages,
        states=states,
        mode=mode,
    )
    return mode, tuple(stage_cli_name(stage) for stage in stages), ids, plan


async def _cmd_task(args: argparse.Namespace, run_id: str) -> int:
    """执行统一任务命令；所有 Stage 共用同一生命周期模式。"""

    try:
        mode, stage_names, ids, task_plan = _build_task_plan(args)
        stage_spec = getattr(args, "stages", None) or getattr(args, "stage", None)
        stages = tuple(parse_stage_names(stage_spec))
        rework_statuses = parse_rework_statuses(getattr(args, "rework", None)) if mode is TaskMode.REWORK else ("draft",)
    except (OSError, ValueError) as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    selected_count = sum(len(values) for values in task_plan.values())
    requested_stage_spec = getattr(args, "stages", None) or getattr(args, "stage", None) or "all"
    requested_stage_set = set(parse_requested_stage_names(str(requested_stage_spec)))
    requested_stage_names = (
        [item.strip().lower() for item in str(requested_stage_spec).split(",") if item.strip()]
        if str(requested_stage_spec).strip().lower() not in {"", "all"}
        else list(stage_names)
    )
    rework_dependency_tasks = {
        stage_cli_name(stage): values
        for stage, values in task_plan.items()
        if mode is TaskMode.REWORK and stage not in requested_stage_set and values
    }
    plan_payload = {
        "execution_id": run_id,
        "source_run_id": getattr(args, "run_id", None),
        "mode": mode.value,
        "requested_ids": ids,
        "requested_stages": requested_stage_names,
        "resolved_stages": list(stage_names),
        "selected_tasks": {
            stage_cli_name(stage): values for stage, values in task_plan.items()
        },
    }
    if mode is TaskMode.REWORK:
        plan_payload["rework_policy"] = (
            "用户指定阶段全部重做；前置阶段仅在未成功并会阻断目标阶段时补做"
        )
        plan_payload["rework_dependency_tasks"] = rework_dependency_tasks
    logger.info("任务计划 %s", plan_payload)
    if mode is TaskMode.REWORK:
        if rework_dependency_tasks:
            logger.info(
                "Rework 依赖补做：用户指定阶段=%s；因前置依赖未成功，补做=%s",
                ",".join(requested_stage_names),
                {stage: len(values) for stage, values in rework_dependency_tasks.items()},
            )
        else:
            logger.info(
                "Rework 依赖检查：用户指定阶段=%s；前置阶段均已成功，不补做前置阶段",
                ",".join(requested_stage_names),
            )
    try:
        save_execution_manifest(plan_payload)
    except ValueError as exc:
        print(f"任务计划错误：{exc}", file=sys.stderr)
        return 2
    if selected_count == 0:
        print(json.dumps({"plan": plan_payload, "message": "没有符合当前模式的任务"}, ensure_ascii=False, indent=2))
        return 0

    from .pipeline import run_pipeline

    stats, actual_run_id = await run_pipeline(
        ids,
        stages=stages,
        run_id=run_id,
        task_ids_by_stage=task_plan,
        task_mode=mode.value,
        rework_statuses=list(rework_statuses),
    )

    progress = load_progress(actual_run_id)
    if progress is not None:
        states = merge_run_progress(progress, load_state(), stats)
        save_state(states)

    result = {
        "execution_id": actual_run_id,
        "plan": plan_payload,
        "stats": stats,
    }
    if getattr(args, "json", False) or getattr(args, "quiet", False):
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        _print_pipeline_summary(actual_run_id, stats)
    return 0 if stats.get("pipeline", {}).get("success", True) else 1


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
        ("classify", "案例分类"),
        ("vision", "截图识别"),
        ("extract", "关键信号抽取"),
        ("review_signals", "统一信号审查"),
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
        elif stage_name == "review_signals":
            done = int(item.get("case_count", 0) or 0)
            skipped = 0
            failed = int(item.get("case_status_counts", {}).get("BLOCKED_SIGNAL_REVIEW", 0) or 0)
        else:
            done = int(item.get("done", 0) or 0)
            failed = int(item.get("failed", 0) or 0)
            skipped = int(item.get("skipped", 0) or 0)
        review_issues = 0
        needs_review = int(item.get("needs_review", item.get("low_confidence", 0)) or 0)
        if stage_name == "review_signals":
            needs_review = int(item.get("case_status_counts", {}).get("NEEDS_SIGNAL_REVIEW", 0) or 0)
            review_issues = sum(int(value or 0) for value in item.get("issue_counts", {}).values())
        if stage_name == "vision":
            needs_review = int(item.get("case_status_counts", {}).get("needs_review", needs_review) or 0)
        blocked = int(item.get("blocked_by_dependency", 0) or 0)
        elapsed = item.get("elapsed_s")
        elapsed_text = f"{elapsed:.1f}s" if isinstance(elapsed, (int, float)) else "-"
        execution_status = item.get("execution_status")
        if execution_status == "not_scheduled":
            status = "未安排"
            status_color = "gray"
        elif execution_status == "no_work":
            status = "无需执行"
            status_color = "gray"
        elif failed or blocked:
            status = "失败/阻断"
            status_color = "red"
        elif needs_review or review_issues:
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

    # 摘要标题及其上下边框作为一个完整的视觉锚点使用明确亮蓝色，
    # 不依赖终端对 bold 的主题映射。
    summary_border = "=" * table_width
    print("\n" + _paint(summary_border, "blue", enabled=enabled))
    print(_paint(_center_display("KBD 流水线完成摘要", table_width), "blue", enabled=enabled))
    print(_paint(summary_border, "blue", enabled=enabled))
    print(f"运行编号   : {run_id}")
    print(f"关联 trace : {get_trace_id() or '-'}（用于串联 kb-service 服务端日志）")
    has_execution_scope = any(
        "selected_cases" in (stats.get(stage_name) or {})
        for stage_name, _label in stage_rows
    )
    ran_any_stage = any(
        int((stats.get(stage_name) or {}).get("selected_cases", 0) or 0) > 0
        for stage_name, _label in stage_rows
    )
    if success and has_execution_scope and not ran_any_stage:
        result_text = "无需执行（没有符合条件的任务）"
        result_color = "gray"
    elif success:
        result_text = "全部阶段完成"
        result_color = "green"
    else:
        result_text = "部分完成，请查看失败/阻断项"
        result_color = "red"
    print(f"总体结果   : {_paint(result_text, result_color, enabled=enabled)}")
    print(f"KBD 完成数 : {completed}/{total}")
    vision_counts = stats.get("vision", {}).get("case_status_counts")
    if vision_counts:
        # 这是 Vision 的 KBD 案例级聚合结果，必须靠近总体结果；它不是
        # “最后附加的一条日志”，也不代表执行顺序上的最后一个事件。
        print("Vision KBD 状态：" + " / ".join(f"{key}={value}" for key, value in vision_counts.items()))
    print(border("┌", "┬", "┐"))
    print(row_text(list(headers)))
    print(border("├", "┼", "┤"))
    for values, color in summary_rows:
        print(row_text(values, color))
    print(border("└", "┴", "┘"))

    unscheduled = [
        (label, int(item.get("candidate_cases", 0) or 0), int(item.get("selected_cases", 0) or 0))
        for stage_name, label in stage_rows
        if (item := stats.get(stage_name))
        and item.get("execution_status") == "not_scheduled"
    ]
    if unscheduled:
        print("未安排阶段：" + "；".join(
            f"{label}（候选 {candidate}，选中 {selected}）"
            for label, candidate, selected in unscheduled
        ))
    no_work = [
        (label, item.get("execution_reason", "本阶段没有待处理任务"))
        for stage_name, label in stage_rows
        if (item := stats.get(stage_name))
        and item.get("execution_status") == "no_work"
    ]
    if no_work:
        print("无需执行阶段：" + "；".join(f"{label}（{reason}）" for label, reason in no_work))

    if stats.get("review_signals", {}).get("issue_counts"):
        issues = stats["review_signals"]["issue_counts"]
        print("统一信号审查问题：" + " / ".join(f"{key}={value}" for key, value in issues.items()))
    if not success:
        print(_paint("建议：先按下方命令确认具体原因；前置阻断项必须先修复上游阶段。", "yellow", enabled=enabled))
        print(
            "失败重试   : uv run python -m data-pipeline.kbd.run task "
            f"--run-id {run_id} --failed"
        )
    jsonl_path = settings.KBD_LOGS_DIR / f"kbd_{run_id}.jsonl"
    text_path = settings.KBD_LOGS_DIR / f"kbd_{run_id}.log"
    print(f"排障日志   : {jsonl_path}")
    print(f"查看失败   : rg -n '\"level\": \"(ERROR|CRITICAL)\"' {jsonl_path}")
    print(f"按案例检索 : rg -n '\"support_id\": \"<案例ID>\"' {jsonl_path}")
    print(f"查看完整文本: less -N {text_path}")


async def _cmd_cli(args: argparse.Namespace, run_id: str) -> int:
    """交互式 CLI：只收集统一 task 参数，实际执行复用 ``_cmd_task``。"""
    if not sys.stdin.isatty():
        print("错误：cli 需要交互终端；自动化请使用 task --ids/--id-file --json。")
        return 3
    print("KBD 交互式 CLI")
    print("将执行：抓取 → 导入 →（截图识别与分类并行）→ 关键信号抽取 → 审计")
    print(f"目标 kb-service：{settings.KB_SERVICE_URL}")
    print(f"数据库：{'已配置' if settings.DATABASE_URL else '未配置'}")
    print(f"内部 Token：{'已配置' if settings.INTERNAL_API_TOKEN else '未配置'}")
    if not settings.INTERNAL_API_TOKEN:
        print("错误：内部 Token 未配置，已停止。请先设置 INTERNAL_API_TOKEN。")
        return 4
    try:
        task_args = _collect_interactive_task_args()
        if Stage.REVIEW_SIGNALS in parse_stage_names(task_args.stages):
            require_shared_contracts()
        mode, stage_names, ids, task_plan = _build_task_plan(task_args)
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"参数错误：{exc}", file=sys.stderr)
        return 2

    _print_interactive_plan(task_args, mode, stage_names, ids, task_plan)
    if not any(task_plan.values()):
        print("没有符合当前模式的任务，已结束。")
        return 0
    if not _prompt_yes_no("确认开始？", default=False):
        print("已取消，未执行任何处理。")
        return 130
    return await _cmd_task(task_args, run_id)


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


def _collect_interactive_task_args() -> argparse.Namespace:
    """按统一任务模型收集交互参数。"""

    source = _prompt_choice(
        "请选择任务范围：",
        (
            ("1", "ids", "手动输入案例 ID（最高频，推荐）"),
            ("2", "run-id", "选择历史 run-id 任务范围"),
            ("3", "id-file", "指定 ID 文件"),
            ("4", "excel", "指定 Excel 文件"),
        ),
        default="1",
    )
    values: dict[str, object] = {
        "command": "task", "excel": False, "ids": None,
        "id_file": None, "run_id": None,
    }
    if source == "ids":
        values["ids"] = input("请输入 KBD 案例 ID（逗号分隔）：").strip()
    elif source == "run-id":
        values["run_id"] = _choose_history_run_id()
    elif source == "id-file":
        values["id_file"] = input("请输入 ID 文件路径：").strip()
    else:
        values["excel"] = True
        values["excel_file"] = input(
            "请输入 Excel 文件路径（直接回车使用 EXCEL_FILE）："
        ).strip() or None

    stage_choice = _prompt_choice(
        "请选择执行阶段：",
        (("1", "all", "ALL：全部六个阶段"), ("2", "selected", "指定阶段")),
        default="1",
    )
    if stage_choice == "all":
        stages = "all"
    else:
        print(
            "阶段：1 fetch  2 import  3 classify  4 vision "
            "5 extract-signals  6 review-signals"
        )
        stages = input("请输入阶段编号或名称（逗号分隔）：").strip()

    mode = _prompt_choice(
        "请选择执行模式：",
        (
            ("1", "default", "默认：未执行 + 失败"),
            ("2", "resume", "断点续跑：仅未执行"),
            ("3", "failed", "失败重试：仅失败"),
            ("4", "rework", "重做：不论完成/失败"),
        ),
        default="1",
    )
    values.update({
        "stages": stages,
        "resume": mode == "resume",
        "failed": mode == "failed",
        "rework": None,
    })
    if mode == "rework":
        print("可选状态：1 draft  2 published  3 rejected  4 archived")
        status_input = input(
            "请输入状态编号或名称（逗号分隔，直接回车默认 draft）："
        ).strip()
        values["rework"] = _parse_interactive_rework_statuses(status_input)

    limit_input = input("最多处理多少个案例？直接回车表示不限制：").strip()
    if limit_input:
        if not limit_input.isdigit() or int(limit_input) <= 0:
            raise ValueError("--limit 必须是正整数")
        values["limit"] = int(limit_input)
    else:
        values["limit"] = None
    values.update({"json": False, "quiet": False, "verbose": False})
    return argparse.Namespace(**values)


def _parse_interactive_rework_statuses(value: str) -> str:
    if not value:
        return "draft"
    aliases = {str(index): name for index, name in enumerate(REWORK_STATUS_NAMES, 1)}
    selected = [aliases.get(item.strip(), item.strip().lower()) for item in value.split(",")]
    parsed = ",".join(dict.fromkeys(selected))
    parse_rework_statuses(parsed)
    return parsed


def _choose_history_run_id() -> str:
    manifests_dir = settings.KBD_LOGS_DIR / "task-manifests"
    manifests = sorted(
        manifests_dir.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not manifests:
        raise ValueError("当前没有可用的历史 run-id")
    choices: list[tuple[str, str, str]] = []
    for index, path in enumerate(manifests[:10], 1):
        manifest = load_execution_manifest(path.stem) or {}
        choices.append(
            (
                str(index),
                path.stem,
                f"{path.stem}：案例 {len(manifest.get('requested_ids', []))} 个，"
                f"模式 {manifest.get('mode', '-')}",
            )
        )
    choices.append(("m", "manual", "手动输入 run-id"))
    selected = _prompt_choice("最近的历史任务：", tuple(choices), default="1")
    if selected == "manual":
        return input("请输入历史 run-id：").strip()
    return selected


def _print_interactive_plan(
    args: argparse.Namespace,
    mode: TaskMode,
    stage_names: tuple[str, ...],
    ids: list[str],
    task_plan: dict[Stage, list[str]],
) -> None:
    requested = args.stages or getattr(args, "stage", None) or "all"
    source = args.run_id or ("Excel" if args.excel else args.id_file or "手动输入")
    print("\nKBD 任务执行计划")
    print(f"任务来源：{source}")
    print(f"案例数：{len(ids)}")
    print(f"执行模式：{mode.value}")
    print(f"用户请求阶段：{requested}")
    print(f"最终执行阶段：{', '.join(stage_names)}")
    if mode is TaskMode.REWORK:
        requested_stages = set(parse_requested_stage_names(str(requested)))
        dependency_tasks = {
            stage_cli_name(stage): selected
            for stage, selected in task_plan.items()
            if stage not in requested_stages and selected
        }
        print("重做规则：用户指定阶段全部重做；前置阶段仅在未成功且会阻断时补做")
        if dependency_tasks:
            details = "、".join(
                f"{stage}({len(selected)}个)"
                for stage, selected in dependency_tasks.items()
            )
            print(f"前置依赖补做：{details}")
        else:
            print("前置依赖补做：无（前置阶段已成功，不重做）")
    print("阶段任务数：")
    for stage, selected in task_plan.items():
        print(f"  {stage_cli_name(stage):<18} {len(selected)}")


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
    """Stage 4：图片语义化"""
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
    """Stage 3：AI 分类（通过 API）"""
    import asyncpg

    from .classifier import classify_batch

    kbd_ids = _get_kbd_ids(args)

    if not settings.INTERNAL_API_TOKEN:
        print("错误：INTERNAL_API_TOKEN 未配置")
        print("请在环境变量或 .env 文件中设置 INTERNAL_API_TOKEN")
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn=settings.asyncpg_database_url)
    try:
        # 读取全部 draft 案例；分类器会把已有分类按幂等 done 计数，
        # 只有未分类案例才实际调用 API。
        classify_ids = await pool.fetch(
            """SELECT support_id FROM kbd_entry
               WHERE support_id = ANY($1)
                 AND status = 'draft'""",
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
            f"""SELECT support_id
               FROM kbd_entry
               WHERE support_id = ANY($1)
                 AND status = 'draft'
                 AND (COALESCE(category_id, '') <> '' OR COALESCE(ai_category_id, '') <> '')
                 AND {EMPTY_SIGNALS_JSON_PREDICATE}
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


async def _cmd_review_signals(args: argparse.Namespace, run_id: str) -> int:
    """Stage 6：从文件、stdin 或数据库审查全部 Signal。"""

    from .signal_review import (
        dump_report,
        load_rows,
        load_rows_file,
        load_rows_from_db,
        review_rows,
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

    report = review_rows(rows)
    logger.info(
        "Review-signals 完成 source=%s cases=%d status=%s issues=%s run_id=%s",
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

    blocked = int(report["case_status_counts"].get("BLOCKED_SIGNAL_REVIEW", 0))
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

def _add_task_scope(p: argparse.ArgumentParser, *, required: bool = False) -> None:
    """所有任务 Stage 共用的输入范围和生命周期参数。"""

    source = p.add_mutually_exclusive_group(required=required)
    source.add_argument("--excel", action="store_true", help="从 EXCEL_FILE 读取案例 ID")
    source.add_argument("--ids", help="逗号分隔的案例 ID，如 29351,29352")
    source.add_argument("--id-file", help="每行一个 ID 的文本文件路径")
    source.add_argument("--run-id", help="使用某次历史任务运行记录中的案例范围")
    p.add_argument("--limit", type=int, default=None, help="最多处理 N 条任务（必须为正整数）")
    p.add_argument("--verbose", action="store_true", help="显示详细调试日志")

    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--resume", action="store_true", help="只执行未完成任务")
    mode.add_argument("--failed", action="store_true", help="只执行失败任务")
    mode.add_argument(
        "--rework",
        nargs="?",
        const="draft",
        metavar="STATUS_LIST",
        help=(
            "重做用户指定阶段；前置阶段仅在未成功且会阻断时补做；"
            "默认只处理 draft，可写 --rework=draft,published"
        ),
    )


def _add_task_command_options(p: argparse.ArgumentParser) -> None:
    _add_task_scope(p, required=True)
    p.add_argument(
        "--stages",
        default=None,
        help=(
            "目标阶段（逗号分隔），默认 all；合法值："
            + ",".join(ALL_STAGE_NAMES)
        ),
    )
    p.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    p.add_argument("--quiet", action="store_true", help="只保留日志，不输出终端摘要")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m data-pipeline.kbd.run",
        description="KBD 知识生产管道",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_task = sub.add_parser("task", help="统一 KBD 任务执行器（六个 Stage 共用同一套参数）")
    _add_task_command_options(p_task)

    sub.add_parser("cli", help="统一 task 任务的交互式前端")

    # 每个 Stage 都是同一种任务，使用完全相同的生命周期参数。
    for name, help_text in [
        ("fetch", "Stage 1：抓取 API + 下载图片"),
        ("import", "Stage 2：语义转换 + 原子入库"),
        ("classify", "Stage 3：AI 分类"),
        ("vision", "Stage 4：图片语义化"),
        ("extract-signals", "Stage 5：关键信号抽取"),
        ("review-signals", "Stage 6：Shared Resolution Runtime 全量审查"),
    ]:
        p_stage = sub.add_parser(name, help=help_text)
        _add_task_scope(p_stage, required=True)
        p_stage.set_defaults(stage=name)
        p_stage.add_argument("--json", action="store_true", help="输出机器可读 JSON")
        p_stage.add_argument("--quiet", action="store_true", help="只保留日志，不输出终端摘要")

    # 文件/stdin 是审查报告工具，不属于任务范围；避免破坏 Stage 的统一任务契约。
    p_input_review = sub.add_parser("review-input", help="审查外部 JSON 输入（不创建 KBD 任务）")
    input_source = p_input_review.add_mutually_exclusive_group(required=True)
    input_source.add_argument("--stdin", action="store_true", help="从标准输入读取 JSON 数组")
    input_source.add_argument("--file", help="从 UTF-8 JSON 文件读取数组")
    p_input_review.add_argument("--output", help="将完整 JSON 报告写入文件")
    p_input_review.add_argument("--fail-on-blocked", action="store_true", help="BLOCKED 时返回 1")

    # review-list 子命令
    p_review = sub.add_parser("review-list", help="列出待审核案例")
    p_review.add_argument("--limit", type=int, default=50)

    # config 子命令
    sub.add_parser("config", help="打印当前配置")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    # 包含 Stage 6 的任务在任何生产副作用前确认 Shared Runtime 可用。
    requires_shared_contracts = False
    if args.command == "task":
        try:
            requires_shared_contracts = Stage.REVIEW_SIGNALS in parse_stage_names(args.stages)
        except ValueError as exc:
            parser.error(str(exc))
    elif args.command in {"review-signals", "review-input"}:
        requires_shared_contracts = True
    if requires_shared_contracts:
        try:
            require_shared_contracts()
        except RuntimeError as exc:
            parser.error(str(exc))

    cmd_map = {
        "task": _cmd_task,
        "cli": _cmd_cli,
        "fetch": _cmd_task,
        "import": _cmd_task,
        "classify": _cmd_task,
        "vision": _cmd_task,
        "extract-signals": _cmd_task,
        "review-signals": _cmd_task,
        "review-input": _cmd_review_signals,
        "review-list": _cmd_review_list,
        "config": lambda a: (_cmd_config(a), None)[1],
    }

    cmd = cmd_map.get(args.command)
    if cmd is None:
        parser.print_help()
        sys.exit(1)

    # config 命令不需要日志初始化
    if args.command == "config":
        _cmd_config(args)
        return

    # 初始化本次执行日志；--run-id 只用于任务范围，不复用为本次 execution_id。
    run_id = _setup_logging(None, verbose=getattr(args, "verbose", False))

    # 执行异步命令，传递 run_id
    exit_code = asyncio.run(cmd(args, run_id))
    if isinstance(exit_code, int) and exit_code:
        raise SystemExit(exit_code)


if __name__ == "__main__":
    main()
