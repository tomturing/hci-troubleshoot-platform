"""
fetcher.py — 产品案例页面抓取器

功能：
  - 抓取 support.sangfor.com.cn 产品案例列表页（翻页）
  - 下载每篇案例正文 HTML，保存到 raw/<id>.html
  - 指数退避重试，幂等（存在则跳过）
  - 输出抓取进度到 stdout

使用：
  uv run data-pipeline/fetcher.py --help
  uv run data-pipeline/fetcher.py --limit 50    # 只抓前 50 篇
  uv run data-pipeline/fetcher.py --page-size 20 --max-pages 100
"""

from __future__ import annotations

import argparse
import json
import logging
import time
from pathlib import Path

import httpx
from tqdm import tqdm

# ─────────────────────────────────────────────────────────────
# 配置
# ─────────────────────────────────────────────────────────────
BASE_URL = "https://support.sangfor.com.cn"
LIST_API = f"{BASE_URL}/api/cases"           # 实际路径按站点调整
DETAIL_URL_TPL = f"{BASE_URL}/cases/{{id}}"  # 详情页 URL 模板

RAW_DIR = Path(__file__).parent / "raw"
METADATA_FILE = Path(__file__).parent / "raw" / "_index.jsonl"

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "zh-CN,zh;q=0.9",
    "Referer": BASE_URL,
}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 工具函数
# ─────────────────────────────────────────────────────────────
def _retry_get(
    client: httpx.Client,
    url: str,
    max_retries: int = 4,
    base_delay: float = 1.0,
    **kwargs,
) -> httpx.Response:
    """带指数退避重试的 GET 请求"""
    for attempt in range(max_retries):
        try:
            resp = client.get(url, **kwargs)
            resp.raise_for_status()
            return resp
        except (httpx.HTTPError, httpx.TimeoutException) as exc:
            if attempt == max_retries - 1:
                raise
            wait = base_delay * (2**attempt)
            logger.warning("请求失败 url=%s 原因=%s 等待 %.1fs 后重试", url, exc, wait)
            time.sleep(wait)
    raise RuntimeError("unreachable")


def _load_fetched_ids() -> set[str]:
    """从 _index.jsonl 读取已抓取的 ID 集合（断点续传）"""
    if not METADATA_FILE.exists():
        return set()
    ids: set[str] = set()
    with METADATA_FILE.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    ids.add(json.loads(line)["id"])
                except (json.JSONDecodeError, KeyError):
                    pass
    return ids


def _save_metadata(meta: dict) -> None:
    """追加元数据记录到 _index.jsonl"""
    with METADATA_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps(meta, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# 列表页抓取
# ─────────────────────────────────────────────────────────────
def fetch_case_list(
    client: httpx.Client,
    page: int = 1,
    page_size: int = 20,
) -> list[dict]:
    """
    抓取案例列表页，返回案例元数据列表。

    实际 API 参数需根据目标网站调整，此处为示意占位。
    返回格式示例：
      [{"id": "12345", "title": "...", "tags": [...], "created_at": "..."}]
    """
    params = {
        "page": page,
        "pageSize": page_size,
        "type": "product",  # 筛选产品案例
    }
    try:
        resp = _retry_get(client, LIST_API, params=params, timeout=20.0)
        data = resp.json()
        # 不同站点结构不同，按实际情况适配 data["list"] / data["items"] 等
        return data.get("list", data.get("items", []))
    except Exception as exc:
        logger.error("列表页抓取失败 page=%d error=%s", page, exc)
        return []


# ─────────────────────────────────────────────────────────────
# 详情页抓取
# ─────────────────────────────────────────────────────────────
def fetch_case_detail(client: httpx.Client, case_id: str) -> str | None:
    """
    抓取单篇案例详情页 HTML。
    返回 HTML 字符串，失败返回 None。
    """
    url = DETAIL_URL_TPL.format(id=case_id)
    try:
        resp = _retry_get(client, url, timeout=30.0)
        return resp.text
    except Exception as exc:
        logger.warning("详情页抓取失败 id=%s error=%s", case_id, exc)
        return None


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def run(
    limit: int | None = None,
    page_size: int = 20,
    max_pages: int = 500,
) -> int:
    """
    执行抓取流程。

    Args:
        limit: 最多抓取 N 篇（None=不限制）
        page_size: 列表页每页条目数
        max_pages: 最大翻页数（防止死循环）

    Returns:
        本次新抓取的文章数量
    """
    RAW_DIR.mkdir(parents=True, exist_ok=True)

    fetched_ids = _load_fetched_ids()
    logger.info("已有 %d 篇案例（断点续传）", len(fetched_ids))

    new_count = 0
    pbar = tqdm(desc="抓取案例", unit="篇", dynamic_ncols=True)

    with httpx.Client(headers=DEFAULT_HEADERS, follow_redirects=True) as client:
        for page in range(1, max_pages + 1):
            cases = fetch_case_list(client, page=page, page_size=page_size)
            if not cases:
                logger.info("列表页为空，抓取完成（共 %d 页）", page - 1)
                break

            for case in cases:
                case_id = str(case.get("id", ""))
                if not case_id:
                    continue

                # 断点续传：已存在则跳过
                if case_id in fetched_ids:
                    pbar.update(1)
                    continue

                # 检查 limit
                if limit is not None and new_count >= limit:
                    pbar.close()
                    logger.info("已达 limit=%d，停止抓取", limit)
                    return new_count

                # 下载详情页
                html = fetch_case_detail(client, case_id)
                if not html:
                    continue

                # 保存 HTML
                out_path = RAW_DIR / f"{case_id}.html"
                out_path.write_text(html, encoding="utf-8")

                # 记录元数据
                meta = {
                    "id": case_id,
                    "title": case.get("title", ""),
                    "tags": case.get("tags", []),
                    "source_url": DETAIL_URL_TPL.format(id=case_id),
                    "created_at": case.get("created_at", ""),
                    "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
                }
                _save_metadata(meta)
                fetched_ids.add(case_id)
                new_count += 1
                pbar.update(1)

                # 礼貌延迟，避免给服务器造成压力
                time.sleep(0.3)

    pbar.close()
    logger.info("本次新抓取 %d 篇，累计 %d 篇", new_count, len(fetched_ids))
    return new_count


# ─────────────────────────────────────────────────────────────
# CLI 入口
# ─────────────────────────────────────────────────────────────
def main() -> None:
    parser = argparse.ArgumentParser(description="产品案例 HTML 抓取器")
    parser.add_argument("--limit", type=int, default=None, help="最多抓取 N 篇")
    parser.add_argument("--page-size", type=int, default=20, help="列表页每页条目数（默认 20）")
    parser.add_argument("--max-pages", type=int, default=500, help="最大翻页数（默认 500）")
    args = parser.parse_args()

    count = run(limit=args.limit, page_size=args.page_size, max_pages=args.max_pages)
    print(f"抓取完成，新增 {count} 篇")


if __name__ == "__main__":
    main()
