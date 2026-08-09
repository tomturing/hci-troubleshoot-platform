"""
data-pipeline/kbd/extract_signals.py - 关键信号分级抽取（Stage 5）

镜像 image_proc.py 模式：离线脚本只做批量调度，LLM 工作下放到 kb-service。
调用 POST /api/admin/kbd/{kbd_entry_id}/extract-signals，由服务端抽取并写回 signals_json。

设计参考：docs/solution/agent/02-架构设计/关键信号字段级分别抽取.md §8
"""
from __future__ import annotations

import asyncio
import logging
import random
import time

import asyncpg
import httpx

from .config import settings
from .error_catalog import humanize_error
from .observability import get_trace_id, traceparent

logger = logging.getLogger("kbd.extract_signals")


async def _call_extract_api(kbd_entry_id: int, client: httpx.AsyncClient) -> dict:
    """调用 kb-service 关键信号抽取 API（自动适配同步/异步两种响应）。

    新行为：POST 返回 202 + job_id 时，自动轮询 status 端点直到完成（与 image_proc.py 对齐）。
    向后兼容：POST 返回 200（同步结果）时直接返回。
    """
    url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/extract-signals"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
        **traceparent(),
    }
    logger.info("提交关键信号抽取 kbd_entry_id=%d trace_id=%s", kbd_entry_id, get_trace_id())

    # 提交（超时短：仅需接收 202）
    timeout_submit = 30.0
    response = None
    max_attempts = max(1, settings.API_MAX_RETRIES)
    for attempt in range(max_attempts):
        try:
            response = await client.post(url, headers=headers, timeout=timeout_submit)
            response.raise_for_status()
            break
        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == max_attempts - 1:
                raise
            wait = random.uniform(0.0, min(30.0, 2.0 ** attempt))
            logger.warning("抽取 API 提交超时 kbd_entry_id=%d 等待 %.1fs 后重试", kbd_entry_id, wait)
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            if status_code == 429:
                retry_after = exc.response.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else random.uniform(0.0, 2.0 ** attempt)
                except (TypeError, ValueError):
                    wait = random.uniform(0.0, 2.0 ** attempt)
            elif 400 <= status_code < 500:
                logger.error("抽取 API 客户端错误 status=%d kbd_entry_id=%d",
                             status_code, kbd_entry_id)
                raise
            else:
                wait = random.uniform(0.0, min(30.0, 2.0 ** attempt))
            if attempt == max_attempts - 1:
                raise
            logger.warning("抽取 API 服务端错误 status=%d 等待 %.1fs 后重试",
                           exc.response.status_code, wait)
            await asyncio.sleep(wait)

    if response is None:
        raise RuntimeError("unreachable: submit exhausted retries")

    body = response.json()
    # 异步模式（202 + job_id）→ 轮询 status
    job_id = body.get("job_id")
    if job_id and response.status_code == 202:
        return await _poll_extract_status(kbd_entry_id, job_id, client)
    # 同步模式（向后兼容）
    return body


async def _poll_extract_status(
    kbd_entry_id: int,
    job_id: str,
    client: httpx.AsyncClient,
    *,
    poll_interval: float = 5.0,
    timeout_total: float = 900.0,  # 15 分钟上限
) -> dict:
    """轮询 Signal Job 状态直至完成（Asynchronous Request-Reply 模式客户端，与 image_proc 对齐）。"""
    status_url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/extract-signals/status"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        **traceparent(),
    }
    logger.info(
        "开始轮询信号抽取状态 kbd_entry_id=%d job_id=%s trace_id=%s",
        kbd_entry_id, job_id, get_trace_id(),
    )
    started_at = time.monotonic()
    poll_count = 0
    consecutive_transport_errors = 0

    while True:
        elapsed = time.monotonic() - started_at
        if elapsed > timeout_total:
            raise TimeoutError(f"Signal Job {job_id} 轮询超时（{timeout_total}s）")
        await asyncio.sleep(poll_interval)
        poll_count += 1
        try:
            resp = await client.get(
                status_url, headers=headers, params={"job_id": job_id}, timeout=30.0,
            )
            resp.raise_for_status()
            data = resp.json()
            consecutive_transport_errors = 0
            job_status = data.get("status")
            logger.info(
                "信号抽取轮询 job_id=%s kbd_entry_id=%d poll=%d status=%s",
                job_id, kbd_entry_id, poll_count, job_status,
            )
            if job_status == "done":
                result = data.get("result") or {}
                return {
                    "success": True,
                    "kbd_id": kbd_entry_id,
                    "signals_count": result.get("signals_count", 0),
                    "rejected_count": result.get("rejected_count", 0),
                    "message": "异步信号抽取完成",
                }
            if job_status == "failed":
                err = data.get("error") or "Job 执行失败"
                raise RuntimeError(f"Signal Job {job_id} 失败：{err}")
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 404:
                raise RuntimeError(f"Signal Job {job_id} 不存在（kb-service 可能已重启）") from exc
            logger.warning("轮询 status 瞬态错误 %s，继续", exc)
            continue
        except httpx.TransportError as exc:
            # Signal Job 已提交后复用同一 job_id 恢复轮询，禁止因瞬时断连重复提交 LLM。
            consecutive_transport_errors += 1
            if consecutive_transport_errors > settings.API_MAX_RETRIES:
                raise RuntimeError(
                    f"Signal Job {job_id} 状态轮询连续 {consecutive_transport_errors} 次传输失败"
                ) from exc
            retry_wait = min(poll_interval, 2.0 ** (consecutive_transport_errors - 1))
            logger.warning(
                "轮询 Signal status 传输错误 job_id=%s attempt=%d/%d，%.1fs 后继续：%s",
                job_id,
                consecutive_transport_errors,
                settings.API_MAX_RETRIES,
                retry_wait,
                exc,
            )
            await asyncio.sleep(retry_wait)


async def extract_signals_batch(
    kbd_ids: list[str], pool: asyncpg.Pool | None = None, *, rework: bool = False
) -> dict[str, int]:
    """批量关键信号抽取。

    Args:
        kbd_ids: support_id 列表
        pool: asyncpg 连接池（用于查 kbd_entry.id）
        rework: 允许对已有 Signal Proposal 重新提交抽取；实际候选筛选由编排层完成。

    Returns:
        {"done": N, "failed": N, "skipped": N, "needs_review": N}
    """
    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    owns_pool = pool is None
    if pool is None:
        pool = await asyncpg.create_pool(
            dsn=settings.asyncpg_database_url
        )

    stats = {"done": 0, "failed": 0, "skipped": 0, "needs_review": 0}

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
            if result.get("success") and int(result.get("signals_count", 0)) > 0:
                stats["done"] += 1
                logger.info("关键信号抽取完成 support_id=%s signals=%d rejected=%d",
                            support_id, result.get("signals_count", 0),
                            result.get("rejected_count", 0))
            elif result.get("success"):
                # HTTP/Job 成功只说明 LLM 流程结束；0 条信号意味着没有可执行 Proposal，
                # 必须进入人工审核，不能用一个非空 signals_json 外壳冒充工程完成。
                stats["needs_review"] += 1
                logger.warning(
                    "关键信号抽取无可执行 Proposal support_id=%s rejected=%d，转人工复核",
                    support_id,
                    result.get("rejected_count", 0),
                )
            else:
                stats["failed"] += 1
        except Exception as exc:
            error = humanize_error(exc)
            logger.error(
                "关键信号抽取失败 support_id=%s code=%s retryable=%s 原因=%s",
                support_id, error.code, error.retryable, error.message,
                extra={
                    "support_id": support_id,
                    "stage": "extract_signals",
                    "error_code": error.code,
                    "retryable": error.retryable,
                },
            )
            stats["failed"] += 1

    max_concurrent = getattr(settings, "EXTRACT_CONCURRENCY", 3)
    sem = asyncio.Semaphore(max_concurrent)

    async def _bounded(support_id: str) -> None:
        async with sem:
            await _run_one(support_id)

    try:
        async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
            await asyncio.gather(*[_bounded(sid) for sid in kbd_ids])
        logger.info(
            "批量关键信号抽取完成 done=%d failed=%d skipped=%d needs_review=%d",
            stats["done"],
            stats["failed"],
            stats["skipped"],
            stats["needs_review"],
        )
        return stats
    finally:
        if owns_pool and pool:
            await pool.close()
