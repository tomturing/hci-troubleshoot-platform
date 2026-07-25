"""
Embedding 服务。

向量检索的正确性依赖一个不可破坏的不变量：文档向量与查询向量必须来自同一模型空间。
因此本服务只接受配置的 OpenAI-compatible embedding 端点返回的真实向量；网络失败、
响应格式错误或维度不匹配时直接失败，由调用方选择词法检索或保存 NULL，不生成伪向量。
"""

from __future__ import annotations

import math
import time
from numbers import Real
from typing import TYPE_CHECKING, Any

import httpx
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger("kb-service-embedding")

_FAILURE_THRESHOLD = 3
_COOLDOWN_SECS = 300


class EmbeddingService:
    """通过单一 provider 生成可比较的真实 embedding。"""

    def __init__(self, settings: Settings):
        self._settings = settings
        self._consecutive_failures = 0
        self._circuit_open_until: float = 0.0

    @property
    def model_name(self) -> str:
        """返回当前向量模型标识，供入库 provenance 使用。"""
        return self._settings.LLM_EMBEDDING_MODEL

    async def embed_single(self, text: str) -> list[float]:
        """生成单条入库向量，失败时抛出异常。"""
        return (await self._embed_strict([text], operation="storage"))[0]

    async def embed_for_search(self, text: str) -> list[float]:
        """生成查询向量，失败时由调用方降级到词法检索。"""
        return (await self._embed_strict([text], operation="search"))[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量生成入库向量，失败时抛出异常。"""
        if not texts:
            return []
        return await self._embed_strict(texts, operation="storage")

    async def _embed_strict(self, texts: list[str], *, operation: str) -> list[list[float]]:
        trace_id = get_current_trace_id()
        started_at = time.monotonic()

        if self._is_circuit_open():
            raise RuntimeError(f"Embedding 服务处于熔断期（冷却至 {self._circuit_open_until:.0f}）")

        try:
            vectors = await self._embed_via_llm(texts)
            validated = self._validate_vectors(vectors, expected_count=len(texts))
        except (httpx.HTTPError, KeyError, TypeError, ValueError, IndexError) as exc:
            self._record_failure(trace_id=trace_id, error=exc, operation=operation)
            raise RuntimeError(f"Embedding 生成失败: {exc}") from exc

        self._consecutive_failures = 0
        logger.info(
            event="embedding_success",
            operation=operation,
            model=self._settings.LLM_EMBEDDING_MODEL,
            count=len(validated),
            dimension=self._settings.EMBEDDING_DIM,
            latency_ms=int((time.monotonic() - started_at) * 1000),
            trace_id=trace_id,
        )
        return validated

    def _is_circuit_open(self) -> bool:
        if self._circuit_open_until == 0:
            return False
        if time.monotonic() < self._circuit_open_until:
            return True
        self._circuit_open_until = 0.0
        self._consecutive_failures = 0
        logger.info(event="embedding_circuit_close", message="熔断冷却结束，重新尝试 embedding")
        return False

    def _record_failure(self, *, trace_id: str, error: Exception, operation: str) -> None:
        self._consecutive_failures += 1
        logger.warning(
            event="embedding_failed",
            operation=operation,
            model=self._settings.LLM_EMBEDDING_MODEL,
            error=str(error),
            consecutive_failures=self._consecutive_failures,
            threshold=_FAILURE_THRESHOLD,
            trace_id=trace_id,
        )
        if self._consecutive_failures >= _FAILURE_THRESHOLD:
            self._circuit_open_until = time.monotonic() + _COOLDOWN_SECS
            logger.warning(
                event="embedding_circuit_open",
                message=f"Embedding 连续失败 {_FAILURE_THRESHOLD} 次，熔断 {_COOLDOWN_SECS}s",
                trace_id=trace_id,
            )

    async def _embed_via_llm(self, texts: list[str]) -> list[list[Any]]:
        """调用 OpenAI-compatible embeddings API。"""
        base_url = self._settings.LLM_BASE_URL.rstrip("/")
        async with httpx.AsyncClient(timeout=self._settings.EMBEDDING_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{base_url}/embeddings",
                headers={"Authorization": f"Bearer {self._settings.LLM_API_KEY}"},
                json={"model": self._settings.LLM_EMBEDDING_MODEL, "input": texts},
            )
            response.raise_for_status()
            payload = response.json()

        data = payload["data"]
        if not isinstance(data, list):
            raise TypeError("embedding 响应 data 必须是数组")
        ordered = sorted(data, key=lambda item: item["index"])
        if [item["index"] for item in ordered] != list(range(len(texts))):
            raise ValueError("embedding 响应 index 不连续或重复")
        return [item["embedding"] for item in ordered]

    def _validate_vectors(self, vectors: list[list[Any]], *, expected_count: int) -> list[list[float]]:
        if len(vectors) != expected_count:
            raise ValueError(f"embedding 数量不匹配（期望 {expected_count}，实际 {len(vectors)}）")

        validated: list[list[float]] = []
        for index, vector in enumerate(vectors):
            if not isinstance(vector, list):
                raise TypeError(f"第 {index} 个 embedding 不是数组")
            if len(vector) != self._settings.EMBEDDING_DIM:
                raise ValueError(
                    f"第 {index} 个 embedding 维度不匹配（期望 {self._settings.EMBEDDING_DIM}，实际 {len(vector)}）"
                )
            if any(isinstance(value, bool) or not isinstance(value, Real) for value in vector):
                raise TypeError(f"第 {index} 个 embedding 包含非数值元素")
            normalized = [float(value) for value in vector]
            if not all(math.isfinite(value) for value in normalized):
                raise ValueError(f"第 {index} 个 embedding 包含 NaN 或 Infinity")
            if not any(value != 0.0 for value in normalized):
                raise ValueError(f"第 {index} 个 embedding 是零向量，无法计算余弦相似度")
            validated.append(normalized)
        return validated
