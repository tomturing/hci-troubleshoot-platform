"""
scripts/kbd/run.py — KBD 知识生产管道 CLI 入口（API 调用版）

使用方式（在项目根目录下）：

  # 完整流水线（从 Excel 读取所有 ID）
  python -m scripts.kbd.run pipeline --excel

  # 完整流水线（指定 ID 列表）
  python -m scripts.kbd.run pipeline --ids 34977,36179,36166

  # 只跑特定 Stage
  python -m scripts.kbd.run fetch --excel --limit 100
  python -m scripts.kbd.run vision --ids 34977,36179
  python -m scripts.kbd.run import --excel
  python -m scripts.kbd.run classify --excel

  # 从上次中断处继续（断点续传）
  python -m scripts.kbd.run pipeline --excel --resume

  # 仅处理失败的案例
  python -m scripts.kbd.run vision --excel --failed-only

  # 强制重新处理（覆盖已完成的记录）
  python -m scripts.kbd.run pipeline --excel --force

  # SOP 文档导入
  python -m scripts.kbd.import_sop --file /path/to/sop.docx --category-id "虚拟机-001"

  # 查看配置
  python -m scripts.kbd.run config
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from datetime import datetime
from pathlib import Path

import httpx

from .config import settings
from .fetcher import read_ids_from_excel
from .pipeline import Stage, run_from_excel


# ─── 日志配置（终端 + 文件双输出）────────────────────────────────────────────────

def _setup_logging(run_id: str | None = None) -> str:
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

    # 配置 root logger，DEBUG 级别以允许 DEBUG 日志通过（Handler 会进一步过滤）
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.DEBUG)

    # 清除已有 handlers（避免重复）
    root_logger.handlers.clear()

    # 日志格式
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    # StreamHandler（终端输出，INFO 级别）
    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(logging.INFO)
    stream_handler.setFormatter(formatter)
    root_logger.addHandler(stream_handler)

    # FileHandler（文件持久化，DEBUG 级别更详细）
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

    logger = logging.getLogger("kbd.run")
    logger.info("日志初始化完成 run_id=%s log_path=%s", run_id, log_path)

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
        "vision": Stage.VISION,
        "import": Stage.IMPORT,
        "classify": Stage.CLASSIFY,
        "1": Stage.FETCH,
        "2": Stage.VISION,
        "3": Stage.IMPORT,
        "4": Stage.CLASSIFY,
    }
    result = []
    for s in stages_str.split(","):
        s = s.strip().lower()
        if s not in stage_map:
            print(f"未知 stage: {s}，合法值：fetch,vision,import,classify")
            sys.exit(1)
        result.append(stage_map[s])
    return result


def _get_case_ids(args: argparse.Namespace) -> list[str]:
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


async def _cmd_pipeline(args: argparse.Namespace, run_id: str) -> None:
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
        case_ids = _get_case_ids(args)
        from .pipeline import run_pipeline
        stats, actual_run_id = await run_pipeline(
            case_ids,
            stages=stages,
            force_fetch=args.force_fetch,
            override=args.override,
            override_status=override_status,
            resume=resume,
            resume_run_id=resume_run_id,
            failed_only=failed_only,
            run_id=run_id,
        )
    print("\n─── 流水线完成 ───")
    print(f"run_id: {actual_run_id}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


async def _cmd_fetch(args: argparse.Namespace, run_id: str) -> None:
    """Stage 1：抓取（文件存储，不依赖数据库）"""
    from .fetcher import fetch_batch

    case_ids = _get_case_ids(args)

    # --failed-only 参数：仅处理抓取失败的案例
    failed_only = getattr(args, "failed_only", False)
    if failed_only:
        from .fetcher import get_failed_fetch_ids
        logger.info("--failed-only 模式：筛选 Fetch 失败案例")
        case_ids = get_failed_fetch_ids(case_ids)
        if not case_ids:
            print("没有抓取失败的案例需要处理")
            return

    logger.info("Fetch 处理开始 cases=%d run_id=%s", len(case_ids), run_id)
    stats = await fetch_batch(case_ids, force=args.force)
    print(f"run_id: {run_id}")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


async def _cmd_vision(args: argparse.Namespace, run_id: str) -> None:
    """Stage 2：图片语义化"""
    import asyncpg

    from .image_proc import process_images_batch, get_failed_vision_ids

    case_ids = _get_case_ids(args)

    # --failed-only 参数：仅处理失败的案例
    failed_only = getattr(args, "failed_only", False)
    if failed_only:
        logger.info("--failed-only 模式：筛选 Vision 失败案例")
        case_ids = get_failed_vision_ids(case_ids)
        if not case_ids:
            print("没有 Vision 失败的案例需要处理")
            return

    # 检查已抓取的案例
    from .fetcher import _is_fetched
    ready_ids = [cid for cid in case_ids if _is_fetched(cid)]

    if not ready_ids:
        print("没有已抓取的案例需要处理")
        return

    logger.info("Vision 处理开始 cases=%d run_id=%s", len(ready_ids), run_id)

    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        stats = await process_images_batch(ready_ids, pool)
        print(f"run_id: {run_id}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_import(args: argparse.Namespace, run_id: str) -> None:
    """Stage 3：MD 转换 + 入库（通过 API）"""
    from .importer import import_batch

    case_ids = _get_case_ids(args)

    if not settings.INTERNAL_API_TOKEN:
        print("错误：INTERNAL_API_TOKEN 未配置")
        print("请在环境变量或 .env 文件中设置 INTERNAL_API_TOKEN")
        sys.exit(1)

    # 检查已抓取且 Vision 完成的案例
    from .fetcher import _case_dir, _is_fetched

    ready_ids: list[str] = []
    for support_id in case_ids:
        if not _is_fetched(support_id):
            continue

        case_dir = _case_dir(support_id)
        img_files = list(case_dir.glob("img_*.*"))
        actual_images = [f for f in img_files if f.suffix not in (".failed", ".txt")]

        if not actual_images:
            ready_ids.append(support_id)
            continue

        all_vision_done = all(
            (case_dir / f"{f.stem}.desc.txt").exists()
            for f in actual_images
        )
        if all_vision_done:
            ready_ids.append(support_id)

    if not ready_ids:
        print("没有已准备好可导入的案例")
        return

    # 解析 override_status 参数（逗号分隔的字符串 → list）
    override_status = None
    if args.override_status:
        override_status = [s.strip() for s in args.override_status.split(",")]

    logger.info("Import 处理开始 cases=%d run_id=%s", len(ready_ids), run_id)
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


async def _cmd_classify(args: argparse.Namespace, run_id: str) -> None:
    """Stage 4：AI 分类（通过 API）"""
    import asyncpg

    from .classifier import classify_batch

    case_ids = _get_case_ids(args)

    if not settings.INTERNAL_API_TOKEN:
        print("错误：INTERNAL_API_TOKEN 未配置")
        print("请在环境变量或 .env 文件中设置 INTERNAL_API_TOKEN")
        sys.exit(1)

    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        # 只处理已入库且未分类的
        classify_ids = await pool.fetch(
            """SELECT support_id FROM kbd_entry
               WHERE support_id = ANY($1)
                 AND status = 'draft'
                 AND (ai_category_id IS NULL OR ai_category_id = '')""",
            case_ids,
        )
        classify_case_ids = [r["support_id"] for r in classify_ids]

        if not classify_case_ids:
            print("没有需要分类的案例")
            return

        logger.info("Classify 处理开始 cases=%d run_id=%s", len(classify_case_ids), run_id)
        stats = await classify_batch(classify_case_ids, pool)
        print(f"run_id: {run_id}")
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


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
                print(f"  {item.get('case_id')} | {item.get('title', '')[:40]} | {cat} (置信度: {conf_str})")

        except Exception as exc:
            print(f"获取待审核列表失败: {exc}")
            print("提示：确保 admin-service API 可用且 INTERNAL_API_TOKEN 已配置")


def _cmd_config(_args: argparse.Namespace) -> None:
    """打印当前配置（隐藏敏感信息）"""
    cfg = settings.model_dump()
    # 隐藏敏感字段
    for key in ("SANGFOR_COOKIE", "ZAI_API_KEY", "DATABASE_URL", "INTERNAL_API_TOKEN"):
        if key in cfg and cfg[key]:
            cfg[key] = cfg[key][:8] + "****"
    print(json.dumps({k: str(v) for k, v in cfg.items()}, ensure_ascii=False, indent=2))


# ─── 参数解析 ────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.kbd.run",
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

    # pipeline 子命令
    p_pipeline = sub.add_parser("pipeline", help="运行完整流水线（或指定 stages）")
    _add_common(p_pipeline)
    p_pipeline.add_argument(
        "--stages",
        help="指定要运行的 stages（逗号分隔）：fetch,vision,import,classify",
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
        help="强制覆盖已存在的记录（仅影响 Stage 3 导入阶段）",
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

    # 单独 stage 子命令
    for name, help_text in [
        ("fetch",    "Stage 1：抓取 API + 下载图片"),
        ("vision",   "Stage 2：图片语义化（Vision LLM）"),
        ("import",   "Stage 3：HTML→MD 转换 + 调用 API 入库"),
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

    # review-list 子命令
    p_review = sub.add_parser("review-list", help="列出待审核案例")
    p_review.add_argument("--limit", type=int, default=50)

    # config 子命令
    sub.add_parser("config", help="打印当前配置")

    return parser


def main() -> None:
    parser = build_parser()
    args = parser.parse_args()

    cmd_map = {
        "pipeline":    _cmd_pipeline,
        "fetch":       _cmd_fetch,
        "vision":      _cmd_vision,
        "import":      _cmd_import,
        "classify":    _cmd_classify,
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

    # 初始化日志（双 Handler：终端 + 文件）
    # 如果有 --resume-run-id 参数，使用它；否则自动生成
    resume_run_id = getattr(args, "resume_run_id", None)
    run_id = _setup_logging(resume_run_id)

    # 执行异步命令，传递 run_id
    asyncio.run(cmd(args, run_id))


if __name__ == "__main__":
    main()
