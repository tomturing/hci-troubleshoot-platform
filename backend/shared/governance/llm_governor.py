"""
共享 LLM 统一治理运行时 (Unified LLM Governor)

提供全平台通用的 LLM 访问治理能力：
1. 进程级共享并发舱壁（Semaphore），防止打爆下游服务/网关 QPM；
2. 智能瞬态错误分类器，精准识别网络超时、连接重置、429 与 5xx 错误；
3. 带抖动的 Full-Jitter 指数退避重试，遵循 Retry-After 响应头；
4. 全链路可观测性，绑定唯一 trace_id、Prometheus 细粒度指标与结构化审计日志。
"""

from __future__ import annotations

import asyncio
import os
import random
import time
from collections.abc import Awaitable, Callable
from typing import Any, TypeVar

import httpx

from shared.observability.logger import get_logger
from shared.observability.metrics import (
    AI_GOVERNANCE_CONCURRENCY_WAIT_SECONDS,
    AI_GOVERNANCE_EXHAUSTED_TOTAL,
    AI_GOVERNANCE_RETRIES_TOTAL,
)
from shared.observability.otel import get_current_trace_id
from shared.utils.exceptions import AIStreamError, ErrorCode

logger = get_logger("shared-llm-governor")

T = TypeVar("T")

_semaphore: asyncio.Semaphore | None = None
_semaphore_lock = asyncio.Lock()


def get_global_concurrency() -> int:
    """读取所有 LLM 场景共用的进程级并发上限。"""
    raw = os.environ.get(
        "LLM_GLOBAL_CONCURRENCY",
        os.environ.get("VISION_GLOBAL_CONCURRENCY", "3"),
    )
    try:
        return max(1, int(raw))
    except ValueError:
        logger.warning(event="llm_global_concurrency_invalid", value=raw, fallback=3)
        return 3


async def get_llm_semaphore(concurrency: int | None = None) -> asyncio.Semaphore:
    """延迟初始化进程级并发信号量，避免模块导入时绑定不存在的 event loop。"""
    global _semaphore
    if _semaphore is None:
        async with _semaphore_lock:
            if _semaphore is None:
                limit = concurrency or get_global_concurrency()
                _semaphore = asyncio.Semaphore(limit)
                logger.info(event="llm_governor_initialized", global_concurrency=limit)
    return _semaphore


def extract_retry_after(exc: Exception) -> float | None:
    """从异常中解析 HTTP Retry-After 头（如果存在）。"""
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
    """
    判定是否属于可通过重试自动恢复的瞬态错误。

    可重试错误包括：
    - 连接超时 (ConnectTimeout)、读取超时 (ReadTimeout)、写入超时 (WriteTimeout)
    - 连接被拒绝 (ConnectError)、网络断开、协议解析异常 (RemoteProtocolError)
    - 上游 RateLimit (HTTP 429)
    - 上游服务端故障 (HTTP 5xx)
    - 结构化 AIStreamError (code in AI_TIMEOUT, AI_RATE_LIMITED, AI_UNAVAILABLE, AI_UPSTREAM_ERROR)

    不可重试错误（直接 fail-fast 抛出）：
    - 鉴权认证错误 (HTTP 401, 403, AI_AUTH_FAILED)
    - 请求参数错误 (HTTP 400, 422)
    - 接口不存在 (HTTP 404)
    """
    # 1. 结构化业务异常
    if isinstance(exc, AIStreamError):
        return exc.code in {
            ErrorCode.AI_TIMEOUT,
            ErrorCode.AI_RATE_LIMITED,
            ErrorCode.AI_UNAVAILABLE,
            ErrorCode.AI_UPSTREAM_ERROR,
        }

    # 2. HTTPX 传输与网络层异常
    if isinstance(exc, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.TransportError)):
        return True

    # 3. HTTP 状态码判定（429 与 5xx）
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if isinstance(status_code, int):
        if status_code == 429 or 500 <= status_code < 600:
            return True
        if status_code in {400, 401, 403, 404, 422}:
            return False

    # 4. OpenAI 兼容 SDK 异常判定（如果已安装且捕获）
    exc_type_name = type(exc).__name__
    if exc_type_name in {
        "APITimeoutError",
        "APIConnectionError",
        "RateLimitError",
        "InternalServerError",
        "ServiceUnavailableError",
    }:
        return True

    # 5. 异常描述签名比对
    message = str(exc).lower()
    retriable_signatures = (
        "connecttimeout",
        "readtimeout",
        "timeout",
        "incomplete chunked read",
        "peer closed connection",
        "read timeout",
        "connection reset",
        "connection refused",
        "server disconnected",
        "remoteprotocolerror",
        "rate limit",
        "too many requests",
    )
    return any(sig in message for sig in retriable_signatures)


def classify_error_type(exc: Exception) -> str:
    """提取错误类型标签，用于指标与监控。"""
    if isinstance(exc, AIStreamError):
        return exc.code
    if isinstance(exc, httpx.TimeoutException):
        return type(exc).__name__.lower()
    if isinstance(exc, httpx.ConnectError):
        return "connect_error"
    status_code = getattr(getattr(exc, "response", None), "status_code", None)
    if status_code:
        return f"http_{status_code}"
    return type(exc).__name__.lower()


async def call_with_llm_governance[T](
    operation: str,
    call: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
    base_delay: float = 1.0,
    cap: float = 15.0,
    trace_id: str | None = None,
    conversation_id: str | None = None,
    case_id: str | None = None,
    extra_context: dict[str, Any] | None = None,
) -> T:
    """
    在共享并发槽位内执行 LLM 调用，并实施受控的 Full-Jitter 指数退避重试。

    Args:
        operation: 调用操作名（如 "signal.ai_processing", "kb.vision", "agent.triage"）
        call: 实际执行的异步函数
        max_attempts: 最大尝试次数（默认读取环境变量 LLM_MAX_ATTEMPTS 或 3）
        base_delay: 重试基础退避时间（秒）
        cap: 重试最大等待上限（秒）
        trace_id: 唯一调用链 ID
        conversation_id: 会话 ID
        case_id: 工单 ID
        extra_context: 附加日志元数据

    Returns:
        T: 调用结果
    """
    raw_attempts = os.environ.get("LLM_MAX_ATTEMPTS", "3")
    attempts = max_attempts or int(raw_attempts)
    attempts = max(1, attempts)
    resolved_trace_id = trace_id or get_current_trace_id() or "unknown"
    context = {
        "operation": operation,
        "trace_id": resolved_trace_id,
        "conversation_id": conversation_id,
        "case_id": case_id,
        **(extra_context or {}),
    }

    last_error: Exception | None = None

    for attempt in range(1, attempts + 1):
        wait_start = time.perf_counter()
        semaphore = await get_llm_semaphore()
        async with semaphore:
            wait_duration = time.perf_counter() - wait_start
            AI_GOVERNANCE_CONCURRENCY_WAIT_SECONDS.labels(operation=operation).observe(wait_duration)
            try:
                return await call()
            except Exception as exc:
                last_error = exc
                err_type = classify_error_type(exc)
                retryable = is_retryable_llm_error(exc)

                if not retryable or attempt >= attempts:
                    if attempt >= attempts and retryable:
                        AI_GOVERNANCE_EXHAUSTED_TOTAL.labels(operation=operation, error_type=err_type).inc()
                    logger.warning(
                        event="llm_call_failed",
                        attempt=attempt,
                        max_attempts=attempts,
                        retryable=retryable,
                        error_type=err_type,
                        error=str(exc),
                        **context,
                    )
                    raise

                AI_GOVERNANCE_RETRIES_TOTAL.labels(operation=operation, error_type=err_type).inc()
                retry_after = extract_retry_after(exc)
                if retry_after is not None:
                    delay = retry_after
                else:
                    backoff = min(cap, base_delay * (2 ** (attempt - 1)))
                    delay = random.uniform(0.1, backoff)

                logger.warning(
                    event="llm_call_retry",
                    attempt=attempt,
                    max_attempts=attempts,
                    delay_s=round(delay, 2),
                    error_type=err_type,
                    error=str(exc),
                    **context,
                )
                await asyncio.sleep(delay)

    assert last_error is not None
    raise last_error
