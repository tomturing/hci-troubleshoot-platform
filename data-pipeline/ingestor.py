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


# =============================================================================
# raw_cases 表写入（T16：HCI 历史工单数据管道）
# =============================================================================

import asyncio as _asyncio
import datetime as _datetime
import json as _json

DB_URL = os.getenv("DATABASE_URL", "postgresql+asyncpg://hci_admin:dev_password_123@postgres:5432/hci_troubleshoot")

MIN_QUALITY_SCORE = 20    # 低于此分的工单不入库（噪音过大）


async def _ingest_raw_cases_async(cases: list[dict]) -> dict:
    """
    异步批量写入工单数据到 raw_cases 表。

    幂等：使用 ON CONFLICT (case_id) DO NOTHING。
    质量评分低于 MIN_QUALITY_SCORE 的工单直接跳过。
    """
    try:
        import asyncpg  # type: ignore[import-untyped]
    except ImportError:
        logger.error("asyncpg 未安装，无法写入数据库")
        return {"inserted": 0, "skipped": 0, "total": len(cases)}

    from fetcher import DataAnonymizer, CaseQualityScorer

    anonymizer = DataAnonymizer()
    scorer = CaseQualityScorer()
    inserted = 0
    skipped = 0

    # 从 DB_URL 解析连接参数（asyncpg 不支持 SQLAlchemy 格式前缀）
    pg_url = DB_URL.replace("postgresql+asyncpg://", "postgresql://")

    conn = await asyncpg.connect(pg_url)
    try:
        for case in cases:
            case_id = str(case.get("id") or case.get("case_id", ""))
            if not case_id:
                skipped += 1
                continue

            content_raw = str(case.get("content_text") or case.get("content") or "")

            # 脱敏处理
            content_anonymized = anonymizer.anonymize(content_raw)

            # 质量评分
            case_with_content = {**case, "content_text": content_anonymized}
            quality_score = scorer.score(case_with_content)
            if quality_score < MIN_QUALITY_SCORE:
                logger.debug("工单 %s 质量评分 %d < %d，跳过", case_id, quality_score, MIN_QUALITY_SCORE)
                skipped += 1
                continue

            source_url = str(case.get("source_url") or case.get("url") or "")
            images_raw = case.get("images") or case.get("attachments") or []
            category = str(case.get("category") or case.get("fault_category") or "")

            try:
                await conn.execute(
                    """
                    INSERT INTO raw_cases
                      (case_id, source_url, content_text, images, classification,
                       quality_score, created_at)
                    VALUES ($1, $2, $3, $4, $5, $6, NOW())
                    ON CONFLICT (case_id) DO NOTHING
                    """,
                    case_id,
                    source_url,
                    content_anonymized,
                    _json.dumps(images_raw, ensure_ascii=False),
                    category,
                    quality_score,
                )
                inserted += 1
            except Exception as exc:
                logger.warning("工单 %s 写入失败: %s", case_id, exc)
                skipped += 1
    finally:
        await conn.close()

    logger.info(
        "raw_cases 入库完成：inserted=%d skipped=%d total=%d",
        inserted,
        skipped,
        len(cases),
    )
    return {"inserted": inserted, "skipped": skipped, "total": len(cases)}


def ingest_raw_cases(cases: list[dict]) -> dict:
    """同步包装器，供 pipeline.py 调用"""
    return _asyncio.run(_ingest_raw_cases_async(cases))


async def fetch_and_ingest_cases(limit: int = 500) -> dict:
    """
    完整工单数据管道：获取 → 脱敏 → 质量评分 → 入库

    需要环境变量：
      DATA_SOURCE_URL   内部支持系统 API 地址
      DATA_SOURCE_TOKEN 认证 Token
    """
    from fetcher import HCICaseFetcher

    fetcher = HCICaseFetcher.from_env()
    logger.info("开始获取最近 %d 条 HCI 工单...", limit)
    cases = await fetcher.fetch_500_cases()
    logger.info("获取到 %d 条工单，开始入库", len(cases))
    return await _ingest_raw_cases_async(cases)

