"""
data-pipeline/raw_to_sop/__main__.py — CLI 入口

用法（在 data-pipeline/ 目录下运行）：

    # Dry-run：仅生成 Markdown，输出到 ./out/ 目录，不调用 API
    python -m raw_to_sop --file raw/内存ECC故障.json --dry-run

    # 预览：打印 Markdown 到 stdout
    python -m raw_to_sop --file raw/内存ECC故障.json --dry-run --stdout

    # 实际入库（draft 状态，需人工 Approve）
    python -m raw_to_sop --file raw/内存ECC故障.json --category-id "硬件-内存"

    # 批量处理目录下所有 .json 文件
    python -m raw_to_sop --dir raw/ --category-id "硬件"

前置配置（在 data-pipeline/raw_to_sop/ 下创建 .env）：
    KB_SERVICE_URL=http://localhost:8004
    INTERNAL_API_TOKEN=your-token-here

或使用环境变量：
    KB_SERVICE_URL=... INTERNAL_API_TOKEN=... python -m raw_to_sop --file ...
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
import textwrap
from pathlib import Path

from .config import settings
from .converter import convert_raw_json_file
from .ingestor import ingest_sop_markdown

logger = logging.getLogger("raw_to_sop")


# ──────────────────────────────────────────────────────────────────────────────
# 核心处理函数
# ──────────────────────────────────────────────────────────────────────────────


def _dry_run_single(
    json_path: Path,
    output_dir: Path | None,
    to_stdout: bool,
) -> dict:
    """Dry-run 模式：转化单个文件，输出到文件/stdout，不调用 API。"""
    try:
        title, source_id, markdown = convert_raw_json_file(json_path)
    except Exception as exc:
        logger.error("转化失败 file=%s 原因=%s", json_path, exc)
        return {"success": False, "file": str(json_path), "error": str(exc)}

    if to_stdout:
        print(f"\n{'='*60}")
        print(f"文件: {json_path.name}")
        print(f"标题: {title}")
        print(f"source_id: {source_id}")
        print(f"{'='*60}\n")
        print(markdown)
    else:
        out_dir = output_dir or (json_path.parent.parent / "out")
        out_dir.mkdir(parents=True, exist_ok=True)
        out_file = out_dir / f"{json_path.stem}.md"
        out_file.write_text(markdown, encoding="utf-8")
        logger.info("Markdown 已写入 %s", out_file)
        print(f"[dry-run] 已生成: {out_file}  (title={title!r}, source_id={source_id!r})")

    return {
        "success": True,
        "file": str(json_path),
        "title": title,
        "source_id": source_id,
        "markdown_lines": len(markdown.splitlines()),
    }


async def _ingest_single(
    json_path: Path,
    category_id: str | None,
) -> dict:
    """入库模式：转化单个文件并调用 API 入库。"""
    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError(
            "INTERNAL_API_TOKEN 未配置。\n"
            "请在 data-pipeline/raw_to_sop/.env 中设置，或使用 --dry-run 模式预览。"
        )

    try:
        title, source_id, markdown = convert_raw_json_file(json_path)
    except Exception as exc:
        logger.error("转化失败 file=%s 原因=%s", json_path, exc)
        return {"success": False, "file": str(json_path), "error": str(exc)}

    try:
        result = await ingest_sop_markdown(
            kb_service_url=settings.KB_SERVICE_URL,
            token=settings.INTERNAL_API_TOKEN,
            source_id=source_id,
            title=title,
            content_md=markdown,
            category_id=category_id,
            timeout=settings.API_TIMEOUT,
        )
        return {
            **result,
            "file": str(json_path),
            "title": title,
            "source_id": source_id,
        }
    except Exception as exc:
        logger.error("API 入库失败 file=%s 原因=%s", json_path, exc)
        return {"success": False, "file": str(json_path), "error": str(exc)}


# ──────────────────────────────────────────────────────────────────────────────
# CLI 参数解析
# ──────────────────────────────────────────────────────────────────────────────


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m raw_to_sop",
        description=(
            "将外部 Raw Graph JSON 转化为 SOPNode Markdown 并入库。\n"
            "默认以 draft 状态入库，需在 Admin UI 中人工审核后 Approve 发布。"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent("""\
            示例：
              # Dry-run（输出到 ./out/ 目录）
              python -m raw_to_sop --file raw/内存ECC故障.json --dry-run

              # Dry-run（打印到终端）
              python -m raw_to_sop --file raw/内存ECC故障.json --dry-run --stdout

              # 实际入库
              python -m raw_to_sop --file raw/内存ECC故障.json --category-id "硬件-内存"

              # 批量处理目录
              python -m raw_to_sop --dir raw/ --category-id "硬件"
        """),
    )

    # 输入来源（互斥）
    source_group = parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument(
        "--file",
        type=Path,
        metavar="FILE",
        help="单个 Raw Graph JSON 文件路径",
    )
    source_group.add_argument(
        "--dir",
        type=Path,
        metavar="DIR",
        help="包含多个 .json 文件的目录（批量处理）",
    )

    # 分类
    parser.add_argument(
        "--category-id",
        metavar="CATEGORY",
        help="SOP 分类编码（可选，如 '硬件-内存'）",
    )

    # 模式控制
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="仅生成 Markdown，不调用 API（输出到 --output-dir 或 ./out/ 目录）",
    )
    parser.add_argument(
        "--stdout",
        action="store_true",
        help="与 --dry-run 配合，将 Markdown 打印到终端而不是写入文件",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        metavar="DIR",
        help="Dry-run 模式下的输出目录（默认：./out/）",
    )

    return parser


# ──────────────────────────────────────────────────────────────────────────────
# 主入口
# ──────────────────────────────────────────────────────────────────────────────


def _collect_json_files(args: argparse.Namespace) -> list[Path]:
    """从参数中收集要处理的 JSON 文件列表。"""
    if args.file:
        if not args.file.exists():
            print(f"错误：文件不存在 {args.file}", file=sys.stderr)
            sys.exit(1)
        return [args.file]
    else:  # args.dir
        if not args.dir.is_dir():
            print(f"错误：目录不存在 {args.dir}", file=sys.stderr)
            sys.exit(1)
        files = sorted(args.dir.glob("*.json"))
        if not files:
            print(f"错误：目录 {args.dir} 下没有 .json 文件", file=sys.stderr)
            sys.exit(1)
        return files


async def _async_main(args: argparse.Namespace) -> None:
    json_files = _collect_json_files(args)
    results: list[dict] = []

    for json_path in json_files:
        logger.info("处理 %s ...", json_path.name)
        if args.dry_run:
            result = _dry_run_single(
                json_path,
                output_dir=args.output_dir,
                to_stdout=args.stdout,
            )
        else:
            result = await _ingest_single(json_path, category_id=args.category_id)
        results.append(result)

    # 汇总报告
    success = sum(1 for r in results if r.get("success"))
    failed = len(results) - success
    print(f"\n─── 处理完成 total={len(results)} success={success} failed={failed} ───")
    if failed:
        print("\n失败详情：")
        for r in results:
            if not r.get("success"):
                print(f"  {r.get('file')}: {r.get('error')}")
    if not args.dry_run:
        print(json.dumps(results, ensure_ascii=False, indent=2))


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    if args.stdout and not args.dry_run:
        print("错误：--stdout 仅在 --dry-run 模式下有效", file=sys.stderr)
        sys.exit(1)

    asyncio.run(_async_main(args))


if __name__ == "__main__":
    main()
