"""
data-pipeline/kbd/extract_signals.py - 关键信号分级抽取（Stage 5）

镜像 image_proc.py 模式：离线脚本只做批量调度，LLM 工作下放到 kb-service。
调用 POST /api/admin/kbd/{kbd_entry_id}/extract-signals，由服务端抽取并写回 signals_json。

设计参考：docs/solution/agent/02-架构设计/关键信号字段级分别抽取.md §8
"""
from __future__ import annotations

import asyncio
import logging

import asyncpg
import httpx

from .config import settings
from .observability import get_trace_id, traceparent

logger = logging.getLogger("kbd.extract_signals")


async def _call_extract_api(kbd_entry_id: int, client: httpx.AsyncClient) -> dict:
    """调用 kb-service 关键信号抽取 API（同步返回）。"""
    url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/extract-signals"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
        **traceparent(),
    }
    logger.info("提交关键信号抽取 kbd_entry_id=%d trace_id=%s", kbd_entry_id, get_trace_id())

    for attempt in range(settings.API_MAX_RETRIES):
        try:
            response = await client.post(url, headers=headers, timeout=settings.API_TIMEOUT)
            response.raise_for_status()
            return response.json()
        except httpx.TimeoutException:
            if attempt == settings.API_MAX_RETRIES - 1:
                raise
            wait = 2.0 * (2 ** attempt)
            logger.warning("抽取 API 超时 kbd_entry_id=%d 等待 %.1fs 后重试", kbd_entry_id, wait)
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                logger.error("抽取 API 客户端错误 status=%d kbd_entry_id=%d",
                             exc.response.status_code, kbd_entry_id)
                raise
            if attempt == settings.API_MAX_RETRIES - 1:
                raise
            wait = 2.0 * (2 ** attempt)
            logger.warning("抽取 API 服务端错误 status=%d 等待 %.1fs 后重试",
                           exc.response.status_code, wait)
            await asyncio.sleep(wait)

    raise RuntimeError("unreachable: extract exhausted retries")


async def extract_signals_batch(kbd_ids: list[str], pool: asyncpg.Pool | None = None) -> dict[str, int]:
    """批量关键信号抽取。

    Args:
        kbd_ids: support_id 列表
        pool: asyncpg 连接池（用于查 kbd_entry.id）

    Returns:
        {"done": N, "failed": N, "skipped": N}
    """
    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    owns_pool = pool is None
    if pool is None:
        pool = await asyncpg.create_pool(
            dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1)
        )

    stats = {"done": 0, "failed": 0, "skipped": 0}

    async def _run_one(support_id: str) -> None:
        kbd_entry_id = await pool.fetchval(
            "SELECT id FROM kbd_entry WHERE support_id = $1", support_id
        )
        if kbd_entry_id is None:
            logger.warning("support_id=%s 不存在，跳过", support_id)
            stats["skipped"] += 1
            return
        try:
            result = await _call_extract_api(kbd_entry_id, client)
            if result.get("success"):
                stats["done"] += 1
                logger.info("关键信号抽取完成 support_id=%s signals=%d rejected=%d",
                            support_id, result.get("signals_count", 0),
                            result.get("rejected_count", 0))
            else:
                stats["failed"] += 1
        except Exception as exc:
            logger.error("关键信号抽取失败 support_id=%s 原因=%s", support_id, exc)
            stats["failed"] += 1

    max_concurrent = getattr(settings, "EXTRACT_CONCURRENCY", 3)
    sem = asyncio.Semaphore(max_concurrent)

    async def _bounded(support_id: str) -> None:
        async with sem:
            await _run_one(support_id)

    try:
        async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
            await asyncio.gather(*[_bounded(sid) for sid in kbd_ids])
        logger.info("批量关键信号抽取完成 done=%d failed=%d skipped=%d",
                    stats["done"], stats["failed"], stats["skipped"])
        return stats
    finally:
        if owns_pool and pool:
            await pool.close()
