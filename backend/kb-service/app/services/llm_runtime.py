"""KBD LLM 调用的共享运行时治理。

三个 KBD LLM 场景（VISION / CLASSIFY / EXTRACT_SIGNALS）通常共用同一
Provider、endpoint 与 API Key。阶段各自增加并发会把流量相乘，因此这里提供
进程级并发舱壁和一致的、带抖动的瞬态错误重试。

该模块只负责单进程内的资源边界；多副本的全局配额仍应由 Provider 配额或
Redis 限流器承担，调用方必须通过指标观察 429 后再提升并发。
"""
from __future__ import annotations

import asyncio
import os
import random
from collections.abc import Awaitable, Callable

import httpx
from openai import APIConnectionError, APIStatusError, APITimeoutError, RateLimitError
from shared.observability.logger import get_logger

logger = get_logger("kb-service-llm-runtime")

_semaphore: asyncio.Semaphore | None = None


def _concurrency() -> int:
    """读取所有 KBD LLM 场景共用的进程级并发上限。"""
    raw = os.environ.get(
        "LLM_GLOBAL_CONCURRENCY",
        os.environ.get("VISION_GLOBAL_CONCURRENCY", "3"),
    )
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(event="llm_global_concurrency_invalid", value=raw, fallback=3)
        return 3


async def get_llm_semaphore() -> asyncio.Semaphore:
    """延迟初始化，避免模块导入时绑定不存在的 event loop。"""
    global _semaphore
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(_concurrency())
        logger.info(event="llm_runtime_initialized", global_concurrency=_concurrency())
    return _semaphore


def _retry_after(exc: Exception) -> float | None:
    response = getattr(exc, "response", None)
    headers = getattr(response, "headers", None)
    value = headers.get("Retry-After") if headers else None
    if value is None:
        return None
    try:
        return max(0.0, float(value))
    except (TypeError, ValueError):
        return None


def is_retryable_llm_error(exc: Exception) -> bool:
    """仅对确实可能自行恢复的错误重试，避免把鉴权/参数错误放大。"""
    if isinstance(exc, (APITimeoutError, APIConnectionError, httpx.TimeoutException, httpx.TransportError)):
        return True
    if isinstance(exc, RateLimitError):
        return True
    if isinstance(exc, APIStatusError):
        return exc.status_code == 429 or 500 <= exc.status_code < 600
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    return status_code == 429 or (isinstance(status_code, int) and 500 <= status_code < 600)


async def call_with_llm_governance[T](
    operation: str,
    call: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
) -> T:
    """在共享并发槽位内调用 LLM，并执行受限的 full-jitter 重试。"""
    attempts = max_attempts or int(os.environ.get("LLM_MAX_ATTEMPTS", "3"))
    attempts = max(1, attempts)
    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        try:
            semaphore = await get_llm_semaphore()
            async with semaphore:
                return await call()
        except Exception as exc:
            last_error = exc
            if not is_retryable_llm_error(exc) or attempt >= attempts:
                logger.exception(
                    event="llm_call_failed",
                    operation=operation,
                    attempt=attempt,
                    max_attempts=attempts,
                    retryable=is_retryable_llm_error(exc),
                    error=str(exc),
                )
                raise
            retry_after = _retry_after(exc)
            cap = min(30.0, 2.0 ** (attempt - 1))
            delay = retry_after if retry_after is not None else random.uniform(0.0, cap)
            logger.warning(
                event="llm_call_retry",
                operation=operation,
                attempt=attempt,
                max_attempts=attempts,
                delay_s=round(delay, 2),
                error=str(exc),
            )
            await asyncio.sleep(delay)

    assert last_error is not None  # pragma: no cover - 防御：循环一定会赋值或 return
    raise last_error
