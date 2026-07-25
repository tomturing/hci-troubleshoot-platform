"""Embedding 服务正确性测试。"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from app.services.embedding import EmbeddingService


def _settings(dimension: int = 3) -> SimpleNamespace:
    return SimpleNamespace(
        LLM_BASE_URL="http://embedding.test/v1",
        LLM_API_KEY="test-key",
        LLM_EMBEDDING_MODEL="test-embedding-model",
        EMBEDDING_DIM=dimension,
        EMBEDDING_TIMEOUT_SEC=3,
    )


@pytest.mark.asyncio
async def test_embed_single_accepts_valid_provider_vector():
    service = EmbeddingService(_settings())
    service._embed_via_llm = AsyncMock(return_value=[[0.1, 0.2, 0.3]])

    assert await service.embed_single("虚拟机启动失败") == [0.1, 0.2, 0.3]
    assert service.model_name == "test-embedding-model"


@pytest.mark.asyncio
async def test_embed_single_rejects_wrong_dimension_without_fallback():
    service = EmbeddingService(_settings())
    service._embed_via_llm = AsyncMock(return_value=[[0.1, 0.2]])

    with pytest.raises(RuntimeError, match="维度不匹配"):
        await service.embed_single("虚拟机启动失败")


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid_value", [float("nan"), float("inf"), float("-inf")])
async def test_embed_single_rejects_non_finite_values(invalid_value: float):
    service = EmbeddingService(_settings())
    service._embed_via_llm = AsyncMock(return_value=[[0.1, invalid_value, 0.3]])

    with pytest.raises(RuntimeError, match="NaN 或 Infinity"):
        await service.embed_single("虚拟机启动失败")


@pytest.mark.asyncio
async def test_embed_single_rejects_zero_vector():
    service = EmbeddingService(_settings())
    service._embed_via_llm = AsyncMock(return_value=[[0.0, 0.0, 0.0]])

    with pytest.raises(RuntimeError, match="零向量"):
        await service.embed_single("虚拟机启动失败")


@pytest.mark.asyncio
async def test_embed_batch_empty_input_skips_provider():
    service = EmbeddingService(_settings())
    service._embed_via_llm = AsyncMock()

    assert await service.embed_batch([]) == []
    service._embed_via_llm.assert_not_awaited()
