"""
ingestor.py — 批量入库（调用 KB Service /api/kb/ingest）

功能：
  - 读取 enriched/<id>.json（优先）或 converted/<id>.json（降级）
  - 调用 POST /api/kb/ingest 将文档写入知识库
  - SHA256(source_url) 作为 source_id，天然幂等
  - 断点续传：记录已成功入库的 ID 到 ingested.json
  - 失败自动重试（指数退避），超出重试则写入 failed.json
  - 显示入库进度和实时统计

使用：
  export KB_URL="http://10.42.0.144:8004"
  export KB_TOKEN="hci-dev-internal-token"
  uv run data-pipeline/ingestor.py --limit 50    # 小批量测试
  uv run data-pipeline/ingestor.py               # 全量入库
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import time
from pathlib import Path

import httpx
from tqdm import tqdm

ENRICHED_DIR = Path(__file__).parent / "enriched"
CONVERTED_DIR = Path(__file__).parent / "converted"
INGESTED_FILE = Path(__file__).parent / "ingested.json"
FAILED_FILE = Path(__file__).parent / "failed.jsonl"

KB_URL = os.getenv("KB_URL", "http://localhost:8004")
KB_TOKEN = os.getenv("KB_TOKEN", "hci-dev-internal-token")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
logger = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────
# 断点续传
# ─────────────────────────────────────────────────────────────
def _load_ingested() -> set[str]:
    """加载已入库 ID 集合"""
    if INGESTED_FILE.exists():
        data = json.loads(INGESTED_FILE.read_text(encoding="utf-8"))
        return set(data) if isinstance(data, list) else set()
    return set()


def _save_ingested(ids: set[str]) -> None:
    INGESTED_FILE.write_text(json.dumps(sorted(ids), ensure_ascii=False, indent=2), encoding="utf-8")


def _record_failed(doc_id: str, reason: str) -> None:
    with FAILED_FILE.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"id": doc_id, "reason": reason}, ensure_ascii=False) + "\n")


# ─────────────────────────────────────────────────────────────
# 入库单篇
# ─────────────────────────────────────────────────────────────
def _ingest_one(
    client: httpx.Client,
    doc: dict,
    max_retries: int = 3,
) -> bool:
    """
    调用 KB Service 入库单篇文档。
    返回 True=成功，False=失败。
    """
    source_url = doc.get("source_url", "")
    # 使用 SHA256(source_url) 作为幂等 source_id
    source_id = hashlib.sha256(source_url.encode()).hexdigest() if source_url else doc.get("id", "")

    payload = {
        "source_id": source_id,
        "title": doc.get("title", ""),
        "content": doc.get("content_md", ""),
        "doc_type": "product_case",
        "metadata": {
            "source_url": source_url,
            "tags": doc.get("tags", []),
            "fault_category": doc.get("fault_category", "unknown"),
            "keywords": doc.get("keywords", []),
            "created_at": doc.get("created_at", ""),
            "summary": doc.get("summary", ""),
        },
    }

    for attempt in range(max_retries):
        try:
            resp = client.post(
                f"{KB_URL}/api/kb/ingest",
                json=payload,
                timeout=60.0,  # embedding 可能较慢
            )
            resp.raise_for_status()
            result = resp.json()
            logger.debug(
                "入库成功 id=%s doc_id=%s chunks=%d",
                doc.get("id"),
                result.get("document_id"),
                result.get("chunks_created", 0),
            )
            return True
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 409:
                # 幂等：已存在，视为成功
                logger.debug("已存在（幂等跳过） id=%s", doc.get("id"))
                return True
            if attempt == max_retries - 1:
                logger.error("入库失败 id=%s status=%d", doc.get("id"), exc.response.status_code)
                return False
        except Exception as exc:
            if attempt == max_retries - 1:
                logger.error("入库异常 id=%s error=%s", doc.get("id"), exc)
                return False
        wait = 2.0 * (2**attempt)
        logger.warning("入库重试 id=%s attempt=%d wait=%.1fs", doc.get("id"), attempt + 1, wait)
        time.sleep(wait)

    return False


# ─────────────────────────────────────────────────────────────
# 主流程
# ─────────────────────────────────────────────────────────────
def _collect_docs(limit: int | None) -> list[Path]:
    """优先读 enriched/，降级 converted/，过滤已入库"""
    ingested_ids = _load_ingested()

    # 优先 enriched
    files: list[Path] = []
    seen: set[str] = set()

    for d in (ENRICHED_DIR, CONVERTED_DIR):
        for p in sorted(d.glob("*.json")):
            if p.stem not in seen and p.stem not in ingested_ids:
                files.append(p)
                seen.add(p.stem)

    logger.info("待入库 %d 篇（已入库 %d 篇跳过）", len(files), len(ingested_ids))

    if limit is not None:
        files = files[:limit]
    return files


def run(limit: int | None = None) -> dict:
    """
    执行入库流程。

    Returns:
        {"success": int, "failed": int, "skipped": int}
    """
    files = _collect_docs(limit)
    if not files:
        logger.info("没有待入库文档")
        return {"success": 0, "failed": 0, "skipped": 0}

    ingested_ids = _load_ingested()
    success_count = 0
    failed_count = 0

    headers = {
        "Authorization": f"Bearer {KB_TOKEN}",
        "Content-Type": "application/json",
    }

    with httpx.Client(headers=headers) as client:
        for path in tqdm(files, desc="入库进度", unit="篇"):
            doc = json.loads(path.read_text(encoding="utf-8"))
            ok = _ingest_one(client, doc)
            if ok:
                ingested_ids.add(path.stem)
                success_count += 1
                # 每 10 篇持久化一次断点
                if success_count % 10 == 0:
                    _save_ingested(ingested_ids)
            else:
                failed_count += 1
                _record_failed(path.stem, "ingest_failed")

    _save_ingested(ingested_ids)
    logger.info("入库完成：成功 %d 篇，失败 %d 篇", success_count, failed_count)

    # 打印 KB 统计
    try:
        with httpx.Client() as client:
            resp = client.get(
                f"{KB_URL}/api/kb/stats",
                headers={"Authorization": f"Bearer {KB_TOKEN}"},
                timeout=10.0,
            )
            if resp.status_code == 200:
                stats = resp.json()
                logger.info("KB 当前统计: %s", json.dumps(stats, ensure_ascii=False))
    except Exception:
        pass

    return {"success": success_count, "failed": failed_count}


def main() -> None:
    parser = argparse.ArgumentParser(description="批量入库工具（调用 KB Service）")
    parser.add_argument("--limit", type=int, default=None, help="最多入库 N 篇")
    parser.add_argument("--kb-url", default=None, help="覆盖 KB_URL 环境变量")
    parser.add_argument("--kb-token", default=None, help="覆盖 KB_TOKEN 环境变量")
    args = parser.parse_args()

    # 支持命令行覆盖
    if args.kb_url:
        os.environ["KB_URL"] = args.kb_url
    if args.kb_token:
        os.environ["KB_TOKEN"] = args.kb_token

    result = run(limit=args.limit)
    print(f"入库完成：成功 {result['success']} 篇，失败 {result['failed']} 篇")
    print(f"KB 服务地址：{KB_URL}")


if __name__ == "__main__":
    main()
