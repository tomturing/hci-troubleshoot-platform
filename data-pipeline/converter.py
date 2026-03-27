"""
converter.py — HTML → 标准 Markdown JSON 转换器

功能：
  - 读取 raw/<id>.html，提取案例正文（title / content / tags / source_url）
  - 使用 markdownify 或 BeautifulSoup 清理 HTML，转为 Markdown 格式
  - 输出 converted/<id>.json，格式：
    {
      "id": "12345",
      "title": "...",
      "content_md": "# 标题\\n\\n...",
      "source_url": "https://...",
      "tags": [...],
      "created_at": "..."
    }
  - 幂等：已存在 converted/<id>.json 则跳过

使用：
  uv run data-pipeline/converter.py
  uv run data-pipeline/converter.py --limit 50
"""

from __future__ import annotations

import argparse
import json
import logging
from pathlib import Path

from bs4 import BeautifulSoup
from markdownify import markdownify as md
from tqdm import tqdm

RAW_DIR = Path(__file__).parent / "raw"
CONVERTED_DIR = Path(__file__).parent / "converted"
METADATA_FILE = RAW_DIR / "_index.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# HTML 解析
# ─────────────────────────────────────────────────────────────
def _extract_content(html: str, source_url: str = "") -> dict:
    """
    从 HTML 中提取结构化内容。

    解析策略（按优先级）：
      1. 尝试 <article> / .article-content / #content 等常见选择器
      2. 降级到 <body> 全文
    返回 {"title": str, "content_html": str}
    """
    soup = BeautifulSoup(html, "lxml")

    # 去除无关元素
    for tag in soup.select("script, style, nav, header, footer, .sidebar, .ad, .advertisement"):
        tag.decompose()

    # 提取标题
    title = ""
    title_tag = soup.find("h1") or soup.find("title")
    if title_tag:
        title = title_tag.get_text(strip=True)

    # 提取正文 HTML（尝试多个选择器）
    content_el = (
        soup.select_one("article")
        or soup.select_one(".article-content")
        or soup.select_one(".post-content")
        or soup.select_one("#content")
        or soup.select_one("main")
        or soup.find("body")
    )
    content_html = str(content_el) if content_el else ""

    return {"title": title, "content_html": content_html}


def _html_to_markdown(content_html: str) -> str:
    """将 HTML 转换为 Markdown，清理多余空行"""
    markdown = md(
        content_html,
        heading_style="ATX",
        bullets="-",
        strip=["a", "img"],  # 去除链接和图片，保留纯文字
    )
    # 去除连续超过 2 行的空行
    lines = markdown.splitlines()
    cleaned: list[str] = []
    blank_count = 0
    for line in lines:
        if line.strip() == "":
            blank_count += 1
            if blank_count <= 1:
                cleaned.append(line)
        else:
            blank_count = 0
            cleaned.append(line)
    return "\n".join(cleaned).strip()


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def _load_metadata() -> dict[str, dict]:
    """从 _index.jsonl 加载元数据索引"""
    meta_map: dict[str, dict] = {}
    if not METADATA_FILE.exists():
        return meta_map
    with METADATA_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    item = json.loads(line)
                    meta_map[item["id"]] = item
                except (json.JSONDecodeError, KeyError):
                    pass
    return meta_map


def run(limit: int | None = None) -> int:
    """
    转换流程。

    Returns:
        本次新转换的文件数量
    """
    CONVERTED_DIR.mkdir(parents=True, exist_ok=True)
    meta_map = _load_metadata()

    html_files = sorted(RAW_DIR.glob("*.html"))
    if not html_files:
        logger.warning("raw/ 目录为空，请先运行 fetcher.py")
        return 0

    new_count = 0
    for html_path in tqdm(html_files, desc="转换 HTML→MD", unit="篇"):
        case_id = html_path.stem

        # 幂等：已存在则跳过
        out_path = CONVERTED_DIR / f"{case_id}.json"
        if out_path.exists():
            continue

        # limit 检查
        if limit is not None and new_count >= limit:
            break

        html = html_path.read_text(encoding="utf-8", errors="replace")
        meta = meta_map.get(case_id, {})

        # 提取并转换
        extracted = _extract_content(html, source_url=meta.get("source_url", ""))
        title = extracted["title"] or meta.get("title", "")
        content_md = _html_to_markdown(extracted["content_html"])

        if not content_md.strip():
            logger.warning("案例 %s 提取内容为空，跳过", case_id)
            continue

        # 写出 JSON
        doc = {
            "id": case_id,
            "title": title,
            "content_md": content_md,
            "source_url": meta.get("source_url", ""),
            "tags": meta.get("tags", []),
            "created_at": meta.get("created_at", ""),
            "fetched_at": meta.get("fetched_at", ""),
        }
        out_path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")
        new_count += 1

    logger.info("本次新转换 %d 篇", new_count)
    return new_count


def main() -> None:
    parser = argparse.ArgumentParser(description="HTML → Markdown JSON 转换器")
    parser.add_argument("--limit", type=int, default=None, help="最多转换 N 篇")
    args = parser.parse_args()

    count = run(limit=args.limit)
    print(f"转换完成，新增 {count} 篇")


if __name__ == "__main__":
    main()
