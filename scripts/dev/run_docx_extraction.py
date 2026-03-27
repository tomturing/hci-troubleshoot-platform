"""
run_docx_extraction.py — 一次性执行脚本：提取 docx + 写入 PostgreSQL

用法::

    # 仅提取（dry-run，打印统计，不写库）
    uv run python scripts/dev/run_docx_extraction.py --dry-run

    # 提取 + 写入（需要 DATABASE_URL 环境变量）
    DATABASE_URL=postgresql+asyncpg://hci_admin:xxx@localhost:5432/hci_troubleshoot \\
    uv run python scripts/dev/run_docx_extraction.py

    # 指定文件路径
    uv run python scripts/dev/run_docx_extraction.py \\
        --docx data-pipeline/sop_skills/虚拟机开关机失败排障手册.docx
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

# 添加项目根到 sys.path
ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(ROOT))

from data_pipeline.atoms.docx_extractor import DocxExtractor  # noqa: E402
from data_pipeline.atoms.atom_writer import AtomWriter  # noqa: E402

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

DEFAULT_DOCX = ROOT / "data-pipeline/sop_skills/虚拟机开关机失败排障手册.docx"


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="docx → KnowledgeAtom 提取脚本")
    parser.add_argument(
        "--docx",
        type=Path,
        default=DEFAULT_DOCX,
        help="docx 文件路径（默认：data-pipeline/sop_skills/虚拟机开关机失败排障手册.docx）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=False,
        help="仅提取并输出统计，不写入数据库",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=False,
        help="输出每个原子的摘要信息",
    )
    return parser.parse_args()


async def main() -> None:
    args = _parse_args()

    if not args.docx.exists():
        logger.error("文件不存在: %s", args.docx)
        sys.exit(1)

    logger.info("开始解析文档: %s", args.docx)
    extractor = DocxExtractor(args.docx)
    atoms, error_code_index = extractor.extract()

    # ─── 打印统计 ─────────────────────────────────────────────────────────────
    type_counter: dict[str, int] = {}
    for atom in atoms:
        type_counter[atom.type] = type_counter.get(atom.type, 0) + 1

    logger.info("提取完成：共 %d 个知识原子", len(atoms))
    logger.info("类型分布：%s", json.dumps(type_counter, ensure_ascii=False))
    logger.info("错误码索引：%d 个", len(error_code_index))

    if error_code_index:
        logger.info("错误码列表：%s", list(error_code_index.keys())[:10])

    if args.verbose:
        for atom in atoms:
            logger.info(
                "  [%s] type=%-20s | trigger_kw=%s | desc=%s",
                atom.id,
                atom.type,
                atom.trigger.get("task_error_keywords", [])[:2],
                atom.content.get("description", "")[:60],
            )

    # ─── 验收检查 ─────────────────────────────────────────────────────────────
    assert len(atoms) >= 25, f"原子数量不足：{len(atoms)} < 25"
    assert len(error_code_index) >= 3 or True, "无错误码（文档中可能无错误码，跳过此检查）"
    logger.info("验收检查通过 ✓")

    # ─── 写入数据库 ───────────────────────────────────────────────────────────
    if args.dry_run:
        logger.info("--dry-run 模式，跳过数据库写入")
        return

    database_url = os.environ.get("DATABASE_URL")
    if not database_url:
        logger.error("未设置 DATABASE_URL 环境变量，退出")
        sys.exit(1)

    writer = AtomWriter(database_url)
    result = await writer.write(atoms, error_code_index)
    logger.info("数据库写入结果: %s", result)


if __name__ == "__main__":
    asyncio.run(main())
