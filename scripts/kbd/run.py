"""
scripts/kbd/run.py — KBD 知识生产管道 CLI 入口

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

  # 强制重新处理（覆盖已完成的记录）
  python -m scripts.kbd.run pipeline --excel --force

  # 查看待审核案例
  python -m scripts.kbd.run review-list

  # 查看配置
  python -m scripts.kbd.run config
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from pathlib import Path

from .config import settings
from .pipeline import Stage, run_pipeline, run_from_excel
from .fetcher import read_ids_from_excel

# 日志配置
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    datefmt="%H:%M:%S",
)
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


async def _cmd_pipeline(args: argparse.Namespace) -> None:
    """执行完整流水线（或指定 stages）"""
    stages = _parse_stages(getattr(args, "stages", None))
    if args.excel and not args.ids and not args.id_file:
        stats = await run_from_excel(stages=stages, force=args.force, limit=args.limit)
    else:
        case_ids = _get_case_ids(args)
        from .pipeline import run_pipeline
        stats = await run_pipeline(case_ids, stages=stages, force=args.force)
    print("\n─── 流水线完成 ───")
    print(json.dumps(stats, ensure_ascii=False, indent=2))


async def _cmd_fetch(args: argparse.Namespace) -> None:
    import asyncpg
    from .fetcher import fetch_batch

    case_ids = _get_case_ids(args)
    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        stats = await fetch_batch(case_ids, pool, force=args.force)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_vision(args: argparse.Namespace) -> None:
    import asyncpg
    from .image_proc import process_images_batch

    case_ids = _get_case_ids(args)
    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        stats = await process_images_batch(case_ids, pool)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_import(args: argparse.Namespace) -> None:
    import asyncpg
    from .importer import import_batch

    case_ids = _get_case_ids(args)
    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        stats = await import_batch(case_ids, pool, force_draft=args.force)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_classify(args: argparse.Namespace) -> None:
    import asyncpg
    from .classifier import classify_batch

    case_ids = _get_case_ids(args)
    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        stats = await classify_batch(case_ids, pool)
        print(json.dumps(stats, ensure_ascii=False, indent=2))
    finally:
        await pool.close()


async def _cmd_review_list(args: argparse.Namespace) -> None:
    import asyncpg
    from .importer import get_pending_review_cases

    pool = await asyncpg.create_pool(dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1))
    try:
        items = await get_pending_review_cases(pool, limit=args.limit or 50)
        print(f"待审核案例（共 {len(items)} 条）：")
        for item in items:
            conf = item.get("ai_category_conf")
            cat = item.get("ai_category_label") or item.get("ai_category_id") or "未分类"
            conf_str = f"{conf:.2f}" if conf is not None else "N/A"
            print(f"  {item['case_id']} | {item['title'][:40]} | {cat} (置信度: {conf_str})")
    finally:
        await pool.close()


def _cmd_config(_args: argparse.Namespace) -> None:
    """打印当前配置（隐藏敏感信息）"""
    cfg = settings.model_dump()
    # 隐藏敏感字段
    for key in ("SANGFOR_COOKIE", "ZAI_API_KEY", "DATABASE_URL"):
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
        p.add_argument("--force", action="store_true", help="强制重新处理已完成的记录")

    # pipeline 子命令
    p_pipeline = sub.add_parser("pipeline", help="运行完整流水线（或指定 stages）")
    _add_common(p_pipeline)
    p_pipeline.add_argument(
        "--stages",
        help="指定要运行的 stages（逗号分隔）：fetch,vision,import,classify",
    )

    # 单独 stage 子命令
    for name, help_text in [
        ("fetch",    "Stage 1：抓取 API + 下载图片"),
        ("vision",   "Stage 2：图片语义化（Vision LLM）"),
        ("import",   "Stage 3：HTML→MD 转换 + 写入 kbd_entry"),
        ("classify", "Stage 4：AI 分类（198 分类节点）"),
    ]:
        p_sub = sub.add_parser(name, help=help_text)
        _add_common(p_sub)

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

    if args.command == "config":
        _cmd_config(args)
        return

    asyncio.run(cmd(args))


if __name__ == "__main__":
    main()
