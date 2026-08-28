"""KBD LLM 调用的共享运行时治理。

基于 shared.governance.llm_governor 统一底座，为 KBD LLM 场景（VISION / CLASSIFY / EXTRACT_SIGNALS）
提供进程级并发舱壁和带抖动的瞬态错误重试。
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import TypeVar

from shared.governance.llm_governor import (
    call_with_llm_governance as _shared_call_with_llm_governance,
)
from shared.governance.llm_governor import (
    get_llm_semaphore,
    is_retryable_llm_error,
)

__all__ = [
    "call_with_llm_governance",
    "get_llm_semaphore",
    "is_retryable_llm_error",
]

T = TypeVar("T")


async def call_with_llm_governance[T](
    operation: str,
    call: Callable[[], Awaitable[T]],
    *,
    max_attempts: int | None = None,
) -> T:
    """在共享并发槽位内调用 LLM，并执行受限的 full-jitter 重试。"""
    return await _shared_call_with_llm_governance(
        operation=f"kb.{operation}",
        call=call,
        max_attempts=max_attempts,
    )
