"""
scripts/kbd/import_sop.py — SOP 文档导入工具

功能：
  1. 读取本地 .docx 文件
  2. 解析章节生成 Markdown
  3. 调用 kb-service API `/api/sop/ingest` 写入 sop_document + sop_chunk

前置操作（必须）：
  ── Docker Compose 环境 ───────────────────────────────
    1. 启动 kb-service：docker compose -f deploy/docker/docker-compose.yml up -d kb-service
    2. 创建配置文件：cp scripts/kbd/.env.example scripts/kbd/.env
    3. （可选）修改 .env 中的 INTERNAL_API_TOKEN

  ── K3s 环境 ───────────────────────────────────────────
    1. 端口转发 kb-service 到本地：
       kubectl port-forward svc/kb-service -n hci-dev 8004:8004 --address 127.0.0.1 &
    2. 获取正确的 INTERNAL_API_TOKEN：
       kubectl exec -n hci-dev deploy/kb-service -- env | grep INTERNAL_API_TOKEN
    3. 创建配置文件（使用正确 token）：
       cat > scripts/kbd/.env << 'EOF'
       KB_SERVICE_URL=http://localhost:8004
       INTERNAL_API_TOKEN=<从步骤2获取的值>
       EOF

用法：
  python -m scripts.kbd.import_sop --file /path/to/sop.docx --category-id "虚拟机-001"
  python -m scripts.kbd.import_sop --dir /path/to/sop_docs/ --category-id "虚拟机-001"

设计特点：
  - 使用 python-docx 解析 Word 文档
  - 按章节（Heading 1/2/3）分块转换为 Markdown
  - 支持 docx_hash 幂等去重
  - 使用 httpx 异步客户端调用 API

调用方：
  - CLI 手动导入
  - scripts/kbd CLI 手动导入
"""
from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import logging
import sys
from pathlib import Path
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("kbd.import_sop")

# 尝试导入 python-docx（可选依赖）
try:
    from docx import Document

    HAS_DOCX = True
except ImportError:
    HAS_DOCX = False
    logger.warning("python-docx 未安装，无法解析 .docx 文件。请执行: uv add python-docx")


# ─── docx 解析 ────────────────────────────────────────────────────────────────


def parse_docx_to_markdown(docx_path: Path) -> tuple[str, str, list[tuple[str, str]]]:
    """
    解析 .docx 文件，生成 Markdown 内容和章节分块。

    Args:
        docx_path: .docx 文件路径

    Returns:
        (title, full_markdown, chapters)
        - title: 文档标题（第一个 Heading 1 或文件名）
        - full_markdown: 完整 Markdown 文本
        - chapters: list of (chapter_title, chapter_content) tuples

    Raises:
        ImportError: python-docx 未安装
        FileNotFoundError: 文件不存在
    """
    if not HAS_DOCX:
        raise ImportError("python-docx 未安装，请执行: uv add python-docx")

    if not docx_path.exists():
        raise FileNotFoundError(f"文件不存在: {docx_path}")

    doc = Document(str(docx_path))

    title = ""
    md_lines: list[str] = []
    chapters: list[tuple[str, str]] = []

    current_chapter_title = "概述"
    current_chapter_lines: list[str] = []

    for para in doc.paragraphs:
        style_name = para.style.name if para.style else ""
        text = para.text.strip()

        if not text:
            continue

        # 判断标题级别
        if style_name.startswith("Heading"):
            try:
                level = int(style_name.split()[-1])
            except ValueError:
                level = 1

            # 保存前一个章节
            if current_chapter_lines:
                content = "\n".join(current_chapter_lines).strip()
                if content:
                    chapters.append((current_chapter_title, content))

            # 开始新章节
            heading_prefix = "#" * min(level, 3)  # 最多 ### 级别
            heading_line = f"{heading_prefix} {text}"
            md_lines.append(heading_line)
            current_chapter_title = text
            current_chapter_lines = [heading_line]

            # 第一个 Heading 1 作为文档标题
            if level == 1 and not title:
                title = text

        else:
            # 普通段落
            md_lines.append(text)
            current_chapter_lines.append(text)

    # 保存最后一个章节
    if current_chapter_lines:
        content = "\n".join(current_chapter_lines).strip()
        if content:
            chapters.append((current_chapter_title, content))

    # 如果没有找到标题，用文件名作为标题
    if not title:
        title = docx_path.stem

    full_markdown = "\n\n".join(md_lines)

    return title, full_markdown, chapters


def compute_docx_hash(docx_path: Path) -> str:
    """计算 docx 文件的 SHA256 哈希（用于幂等去重）"""
    sha256 = hashlib.sha256()
    with docx_path.open("rb") as f:
        while chunk := f.read(8192):
            sha256.update(chunk)
    return sha256.hexdigest()


# ─── API 调用 ──────────────────────────────────────────────────────────────────


async def _call_sop_ingest_api(
    source_id: str | None,
    title: str,
    content_md: str,
    category_id: str | None,
    docx_hash: str | None,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """
    调用 kb-service SOP 入库 API。

    Args:
        source_id: 来源标识（如 "sop-vm-start-failure")
        title: SOP 标题
        content_md: 完整 Markdown 文档
        category_id: 分类编码（可选）
        docx_hash: 文件哈希（用于幂等）
        client: httpx 异步客户端

    Returns:
        {"success": true, "document_id": 1, "chunks_created": 5, "status": "draft"}

    Raises:
        httpx.HTTPStatusError: API 返回非 2xx 状态码
    """
    url = f"{settings.KB_SERVICE_URL}/api/sop/ingest"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "source_id": source_id,
        "title": title,
        "content_md": content_md,
        "category_id": category_id,
        "docx_hash": docx_hash,
    }

    response = await client.post(
        url,
        headers=headers,
        json=payload,
        timeout=settings.API_TIMEOUT,
    )
    response.raise_for_status()
    return response.json()


async def import_sop_docx(
    docx_path: Path,
    category_id: str | None = None,
    source_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """
    导入单个 SOP .docx 文件。

    Args:
        docx_path: .docx 文件路径
        category_id: 分类编码（可选）
        source_id: 来源标识（可选，不传则用文件名）
        client: httpx 异步客户端（可选）

    Returns:
        {
            "success": true/false,
            "document_id": int,
            "chunks_created": int,
            "status": str,
            "message": str,
            "source_file": str,
        }
    """
    if not HAS_DOCX:
        return {
            "success": False,
            "message": "python-docx 未安装，请执行: uv add python-docx",
            "source_file": str(docx_path),
        }

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    try:
        # 解析 docx
        title, content_md, chapters = parse_docx_to_markdown(docx_path)
        docx_hash = compute_docx_hash(docx_path)

        # 默认 source_id 用文件名
        if not source_id:
            source_id = f"sop-{docx_path.stem}"

        logger.info(
            "解析完成 file=%s title=%s chapters=%d hash=%s",
            docx_path.name, title[:30], len(chapters), docx_hash[:16]
        )

        # 使用传入的 client 或创建临时客户端
        should_close = False
        if client is None:
            client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
            should_close = True

        try:
            api_result = await _call_sop_ingest_api(
                source_id=source_id,
                title=title,
                content_md=content_md,
                category_id=category_id,
                docx_hash=docx_hash,
                client=client,
            )

            return {
                "success": api_result.get("success", False),
                "document_id": api_result.get("document_id"),
                "chunks_created": api_result.get("chunks_created", 0),
                "status": api_result.get("status", "draft"),
                "message": api_result.get("message", ""),
                "source_file": str(docx_path),
                "title": title,
            }

        finally:
            if should_close:
                await client.aclose()

    except Exception as exc:
        logger.error("导入失败 file=%s 原因=%s", docx_path, exc)
        return {
            "success": False,
            "message": str(exc),
            "source_file": str(docx_path),
        }


async def import_sop_dir(
    dir_path: Path,
    category_id: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> list[dict[str, Any]]:
    """
    批量导入目录下所有 .docx 文件。

    Args:
        dir_path: 包含 .docx 文件的目录
        category_id: 分类编码（可选，所有文件共用）
        client: httpx 异步客户端（可选）

    Returns:
        list of import results for each file
    """
    docx_files = list(dir_path.glob("*.docx"))
    if not docx_files:
        logger.warning("目录 %s 下没有 .docx 文件", dir_path)
        return []

    results: list[dict[str, Any]] = []

    # 共享客户端
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        should_close = True

    try:
        for idx, docx_path in enumerate(docx_files, 1):
            logger.info("[%d/%d] 导入 %s", idx, len(docx_files), docx_path.name)
            result = await import_sop_docx(
                docx_path,
                category_id=category_id,
                client=client,
            )
            results.append(result)

    finally:
        if should_close:
            await client.aclose()

    # 统计结果
    success_count = sum(1 for r in results if r.get("success"))
    logger.info(
        "批量导入完成 total=%d success=%d failed=%d",
        len(results), success_count, len(results) - success_count
    )

    return results


# ─── CLI 入口 ───────────────────────────────────────────────────────────────────


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m scripts.kbd.import_sop",
        description="SOP 文档导入工具",
    )

    # 文件来源（互斥）
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--file", type=Path, help="单个 .docx 文件路径")
    group.add_argument("--dir", type=Path, help="包含 .docx 文件的目录")

    # 分类
    parser.add_argument(
        "--category-id",
        help="分类编码（如 虚拟机-001），可选",
    )

    # 来源标识（仅单文件模式）
    parser.add_argument(
        "--source-id",
        help="来源标识（如 sop-vm-start-failure），可选",
    )

    return parser


async def main_async(args: argparse.Namespace) -> None:
    """异步主入口"""
    if not HAS_DOCX:
        print("错误: python-docx 未安装")
        print("请执行: uv add python-docx")
        sys.exit(1)

    if not settings.INTERNAL_API_TOKEN:
        print("错误: INTERNAL_API_TOKEN 未配置")
        print("请在环境变量或 .env 文件中设置 INTERNAL_API_TOKEN")
        sys.exit(1)

    client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)

    try:
        if args.file:
            result = await import_sop_docx(
                args.file,
                category_id=args.category_id,
                source_id=args.source_id,
                client=client,
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))

        elif args.dir:
            results = await import_sop_dir(
                args.dir,
                category_id=args.category_id,
                client=client,
            )
            print(json.dumps(results, ensure_ascii=False, indent=2))

    finally:
        await client.aclose()


def main() -> None:
    """CLI 入口"""
    parser = build_parser()
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
