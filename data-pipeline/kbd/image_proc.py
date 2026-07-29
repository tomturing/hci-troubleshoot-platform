"""
data-pipeline/kbd/image_proc.py - 图片语义化（调用 kb-service API 版）

本模块已重构为薄封装：核心 Vision LLM 调用逻辑已迁移到
backend/kb-service/app/services/vision_processor.py，
Prompt 统一由 system_prompt 表管理（admin-ui 在线编辑 + 热生效）。

本模块职责：
  - 提供批量处理入口（process_images_batch），内部调用 kb-service API
  - 与 classifier.py 的模式保持一致（httpx + 重试）
  - 单条重算由 admin-ui 直接调用 POST /api/admin/kbd/{id}/reanalyze-images

设计原则：
  - 离线脚本只负责批量调度，不直接调用 LLM
  - Prompt 管理与 TriageAgent 保持统一一致（数据库 system_prompt 表）
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

import asyncpg
import httpx

from .config import settings
from .observability import get_trace_id, traceparent

logger = logging.getLogger("kbd.image_proc")


# ─── API 客户端 ──────────────────────────────────────────────────────────────


async def _call_reanalyze_api(
    kbd_entry_id: int,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """
    调用 kb-service 重新识图 API（P1-1：自动适配同步/异步两种响应）。

    新行为：POST 返回 202 + job_id 时，自动轮询 status 端点直到完成。
    向后兼容：POST 返回 200（同步结果）时直接返回。

    Args:
        kbd_entry_id: kbd_entry 表的 ID（注意：不是 support_id）
        client: httpx 异步客户端

    Returns:
        {
            "success": True,
            "kbd_id": int,
            "total": int,
            "done": int,
            "failed": int,
            "message": str
        }
    """
    post_url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/reanalyze-images"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
        # 注入 W3C traceparent，与 kb-service 日志共享同一 trace_id（见 observability.py）
        **traceparent(),
    }

    logger.info("提交重新识图 kbd_entry_id=%d trace_id=%s", kbd_entry_id, get_trace_id())

    # 提交（超时短：仅需接收 202）
    timeout_submit = 30.0
    response = None
    for attempt in range(settings.API_MAX_RETRIES):
        try:
            response = await client.post(post_url, headers=headers, timeout=timeout_submit)
            response.raise_for_status()
            break
        except httpx.TimeoutException:
            if attempt == settings.API_MAX_RETRIES - 1:
                raise
            wait = 2.0 * (2 ** attempt)
            logger.warning("识图提交超时 kbd_entry_id=%d 等待 %.1fs 后重试", kbd_entry_id, wait)
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                logger.error("识图 API 客户端错误 status=%d kbd_entry_id=%d", exc.response.status_code, kbd_entry_id)
                raise
            if attempt == settings.API_MAX_RETRIES - 1:
                raise
            wait = 2.0 * (2 ** attempt)
            logger.warning("识图 API 服务端错误 status=%d 等待 %.1fs 后重试", exc.response.status_code, wait)
            await asyncio.sleep(wait)

    if response is None:
        raise RuntimeError("unreachable: submit exhausted retries")

    body = response.json()
    # 异步模式（202 + job_id）→ 轮询 status
    job_id = body.get("job_id")
    if job_id and response.status_code == 202:
        return await _poll_reanalyze_status(kbd_entry_id, job_id, client)
    # 同步模式（向后兼容）
    return body


async def _poll_reanalyze_status(
    kbd_entry_id: int,
    job_id: str,
    client: httpx.AsyncClient,
    *,
    poll_interval: float = 5.0,
    timeout_total: float = 900.0,  # 15 分钟上限
) -> dict[str, Any]:
    """轮询 Vision Job 状态直至完成（Asynchronous Request-Reply 模式客户端）。"""
    status_url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/reanalyze-images/status"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        **traceparent(),
    }
    logger.info(
        "开始轮询识图状态 kbd_entry_id=%d job_id=%s trace_id=%s",
        kbd_entry_id, job_id, get_trace_id(),
    )
    started_at = time.monotonic()
    poll_count = 0
    consecutive_transport_errors = 0

    while True:
        elapsed = time.monotonic() - started_at
        if elapsed > timeout_total:
            raise TimeoutError(f"Vision Job {job_id} 轮询超时（{timeout_total}s）")
        await asyncio.sleep(poll_interval)
        poll_count += 1
        try:
            resp = await client.get(
                status_url, headers=headers, params={"job_id": job_id}, timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            consecutive_transport_errors = 0
            status = data.get("status")
            logger.info(
                "识图轮询 job_id=%s kbd_entry_id=%d poll=%d status=%s done=%d/%d",
                job_id, kbd_entry_id, poll_count, status,
                data.get("done", 0), data.get("total", 0),
            )
            if status == "done":
                return {
                    "success": True,
                    "kbd_id": kbd_entry_id,
                    "total": data.get("total", 0),
                    "done": data.get("done", 0),
                    "failed": data.get("failed", 0),
                    "message": "异步识图完成",
                }
            if status == "failed":
                err = data.get("error") or "Job 执行失败"
                raise RuntimeError(f"Vision Job {job_id} 失败：{err}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(f"Vision Job {job_id} 不存在（kb-service 可能已重启）") from exc
            logger.warning("轮询 status 瞬态错误 %s，继续", exc)
            continue
        except httpx.TransportError as exc:
            # POST 已经成功返回 job_id 后，轮询连接断开不代表后台 Vision Job 失败。
            # 必须复用同一 job_id 有限重试，避免重复提交任务和重复消耗模型配额。
            consecutive_transport_errors += 1
            if consecutive_transport_errors > settings.API_MAX_RETRIES:
                raise RuntimeError(
                    f"Vision Job {job_id} 状态轮询连续 {consecutive_transport_errors} 次传输失败"
                ) from exc
            retry_wait = min(poll_interval, 2.0 ** (consecutive_transport_errors - 1))
            logger.warning(
                "轮询 status 传输错误 job_id=%s attempt=%d/%d，%.1fs 后继续：%s",
                job_id,
                consecutive_transport_errors,
                settings.API_MAX_RETRIES,
                retry_wait,
                exc,
            )
            await asyncio.sleep(retry_wait)


# ─── 批量处理 ────────────────────────────────────────────────────────────────


async def process_images_batch(kbd_ids: list[str], _pool: Any = None) -> dict[str, int]:
    """
    批量调用 kb-service API 重新识图。

    Args:
        kbd_ids: support_id 列表（如 ["26890", "26891", ...]）
        _pool: 兼容旧签名的 asyncpg 连接池（未使用，API 调用无需）

    Returns:
        {"done": N, "failed": N, "skipped": N}
    """
    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    # 查询每个 support_id 对应的 kbd_entry.id
    pool: asyncpg.Pool | None = _pool
    if pool is None:
        pool = await asyncpg.create_pool(
            dsn=settings.asyncpg_database_url
        )

    try:
        stats = {"done": 0, "failed": 0, "skipped": 0}

        async def _run_one(support_id: str) -> None:
            kbd_entry_id = await pool.fetchval(
                "SELECT id FROM kbd_entry WHERE support_id = $1", support_id
            )
            if kbd_entry_id is None:
                logger.warning("support_id=%s 在 kbd_entry 表中不存在，跳过", support_id)
                stats["skipped"] += 1
                return
            logger.info("重新识图 support_id=%s kbd_entry_id=%d", support_id, kbd_entry_id)
            try:
                result = await _call_reanalyze_api(kbd_entry_id, client)
                if result.get("success"):
                    stats["done"] += result.get("done", 0)
                    stats["failed"] += result.get("failed", 0)
                    logger.info(
                        "识图完成 support_id=%s done=%d failed=%d",
                        support_id, result.get("done", 0), result.get("failed", 0),
                    )
                else:
                    stats["failed"] += 1
            except Exception as exc:
                logger.error("识图失败 support_id=%s 原因=%s", support_id, exc)
                stats["failed"] += 1

        # P1-1 并发提交（Semaphore 控制，避免一次提交 100 个撑爆后端）
        max_concurrent = getattr(settings, "VISION_CONCURRENCY", 3)
        sem = asyncio.Semaphore(max_concurrent)

        async def _bounded(support_id: str) -> None:
            async with sem:
                await _run_one(support_id)

        async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
            await asyncio.gather(*[_bounded(sid) for sid in kbd_ids])

        logger.info(
            "批量识图完成 done=%d failed=%d skipped=%d",
            stats["done"], stats["failed"], stats["skipped"],
        )
        return stats
    finally:
        if _pool is None and pool:
            await pool.close()


# ─── 兼容旧接口 ────────────────────────────────────────────────────────────────


async def process_images_for_kbd(kbd_id: str, client: Any = None) -> dict[str, int]:
    """
    处理单个案例的所有图片（兼容旧接口）。

    已重构为调用 kb-service API。注意：kbd_id 参数是 support_id，
    内部会查询对应的 kbd_entry.id。

    Args:
        kbd_id: support_id（如 "26890"）
        client: 兼容旧签名（未使用）

    Returns:
        {"done": N, "failed": N, "skipped": N}
    """
    pool = await asyncpg.create_pool(
        dsn=settings.asyncpg_database_url
    )
    try:
        return await process_images_batch([kbd_id], pool)
    finally:
        await pool.close()
