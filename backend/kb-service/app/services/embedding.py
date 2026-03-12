"""
Embedding 服务 — 双模式切换（z.ai API 主力 + bge-small-zh 本地降级）

设计说明：
- 主力：z.ai API（与对话服务复用同一 AI 端点，通过 OpenAI-compatible API 调用）
- 降级：本地 bge-small-zh-v1.5（网络超时/故障时自动切换）
- 连续降级计数：连续 3 次 z.ai 失败后，自动切换到本地模式，5 分钟后重试 z.ai
- 所有 embedding 调用都通过 OTel 追踪（embedding_latency, fallback_count）

注意事项：
- z.ai API 与 OpenClaw 使用同一 base_url（18790），embedding 模型为 embedding-3
- 本地 BGE 模型路径由环境变量 BGE_MODEL_PATH 配置，不存在时跳过降级
- 向量维度固定为 384（bge-small-zh 与部分 z.ai embedding 模型一致）
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
from shared.utils.logger import get_logger
from shared.utils.otel import get_current_trace_id

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger("kb-service-embedding")

# 连续失败阈值：超过此次数后切换到本地模式
_FALLBACK_THRESHOLD = 3
# 熔断冷却时间（秒）：切换到本地模式后，等待此时间后重试 z.ai
_COOLDOWN_SECS = 300


class EmbeddingService:
    """双模式 Embedding 服务

    Usage:
        service = EmbeddingService(settings)
        vectors = await service.embed_batch(["文本1", "文本2"])
        vector = await service.embed_single("文本")
    """

    def __init__(self, settings: "Settings"):
        self._settings = settings
        self._consecutive_failures = 0
        self._local_mode_until: float = 0.0           # Unix timestamp：本地模式截止时间
        self._local_model = None                       # 懒加载本地模型

    async def embed_single(self, text: str) -> list[float]:
        """获取单条文本的 embedding 向量"""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量获取 embedding 向量

        Args:
            texts: 待 embed 的文本列表

        Returns:
            与 texts 等长的向量列表，每个向量长度为 384

        Raises:
            RuntimeError: 两路 embedding 均失败时抛出
        """
        trace_id = get_current_trace_id()
        t_start = time.monotonic()

        # 判断是否处于本地模式
        use_local = self._should_use_local()

        if not use_local:
            try:
                result = await self._embed_via_zai(texts)
                self._consecutive_failures = 0   # 成功后重置计数
                logger.info(
                    event="embedding_zai_success",
                    count=len(texts),
                    latency_ms=int((time.monotonic() - t_start) * 1000),
                    trace_id=trace_id,
                )
                return result
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
                self._consecutive_failures += 1
                logger.warning(
                    event="embedding_zai_failed",
                    error=str(exc),
                    consecutive_failures=self._consecutive_failures,
                    threshold=_FALLBACK_THRESHOLD,
                    trace_id=trace_id,
                )
                if self._consecutive_failures >= _FALLBACK_THRESHOLD:
                    self._local_mode_until = time.monotonic() + _COOLDOWN_SECS
                    logger.warning(
                        event="embedding_circuit_open",
                        message=f"z.ai embedding 连续失败 {_FALLBACK_THRESHOLD} 次，切换到本地模式 {_COOLDOWN_SECS}s",
                        trace_id=trace_id,
                    )

        # 降级：本地 bge-small-zh
        try:
            result = await self._embed_via_local(texts)
            logger.info(
                event="embedding_local_success",
                count=len(texts),
                latency_ms=int((time.monotonic() - t_start) * 1000),
                trace_id=trace_id,
            )
            return result
        except Exception as local_exc:
            logger.error(
                event="embedding_all_failed",
                error=str(local_exc),
                trace_id=trace_id,
            )
            raise RuntimeError(f"两路 Embedding 均失败: {local_exc}") from local_exc

    def _should_use_local(self) -> bool:
        """判断是否应使用本地模式（熔断期内）"""
        if self._local_mode_until == 0:
            return False
        if time.monotonic() < self._local_mode_until:
            return True
        # 冷却结束，重置状态，重新尝试 z.ai
        self._local_mode_until = 0.0
        self._consecutive_failures = 0
        logger.info(event="embedding_circuit_close", message="熔断冷却结束，重新尝试 z.ai embedding")
        return False

    async def _embed_via_zai(self, texts: list[str]) -> list[list[float]]:
        """通过 z.ai API 获取 embedding（OpenAI-compatible 格式）"""
        async with httpx.AsyncClient(timeout=self._settings.EMBEDDING_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{self._settings.ZAI_BASE_URL}/v1/embeddings",
                headers={"Authorization": f"Bearer {self._settings.ZAI_API_KEY}"},
                json={
                    "model": self._settings.ZAI_EMBEDDING_MODEL,
                    "input": texts,
                },
            )
            response.raise_for_status()
            data = response.json()
            # OpenAI 格式：data[].embedding
            return [item["embedding"] for item in sorted(data["data"], key=lambda x: x["index"])]

    async def _embed_via_local(self, texts: list[str]) -> list[list[float]]:
        """通过本地 bge-small-zh-v1.5 获取 embedding（在线程池中运行，避免阻塞事件循环）"""
        model = await self._get_local_model()
        loop = asyncio.get_event_loop()
        if callable(getattr(model, "encode", None)):
            # sentence_transformers 模型
            return await loop.run_in_executor(None, lambda: model.encode(texts).tolist())
        else:
            # 纯 numpy hash-based 降级模型
            return await loop.run_in_executor(None, lambda: model(texts))

    async def _get_local_model(self):
        """懒加载本地模型（首次调用时加载，后续复用）

        优先级：
        1. sentence_transformers（需要已安装）
        2. numpy hash-based embedding（纯 Python 降级，确定性向量化）
        """
        if self._local_model is None:
            import os

            model_path = self._settings.BGE_MODEL_PATH

            # 尝试 sentence_transformers
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                if os.path.exists(model_path):
                    loop = asyncio.get_event_loop()
                    self._local_model = await loop.run_in_executor(
                        None, lambda: SentenceTransformer(model_path)
                    )
                    logger.info(event="local_model_loaded", model_path=model_path, backend="sentence_transformers")
                    return self._local_model
            except ImportError:
                logger.warning(
                    event="sentence_transformers_unavailable",
                    message="sentence_transformers 未安装，降级到 numpy hash embedding",
                )

            # 降级：numpy hash-based embedding（确定性，不依赖外部包）
            # 使用 settings.EMBEDDING_DIM，与数据库 vector(512) 保持一致
            self._local_model = _make_numpy_hash_embedder(self._settings.EMBEDDING_DIM)
            logger.warning(
                event="using_hash_embedding",
                message="使用 numpy hash embedding 降级方案，向量搜索精度有限，BM25 搜索仍有效",
            )

        return self._local_model


def _make_numpy_hash_embedder(dim: int = 384):
    """创建一个基于 numpy 的确定性 hash-based embedding 函数

    原理：将文本每个字符的 Unicode codepoint 散列到 dim 维向量，并 L2 归一化。
    确定性（相同输入→相同向量），不依赖任何 ML 包，适合数据入库但搜索精度有限。
    """
    import hashlib
    import math

    import numpy as np

    def embed_texts(texts: list[str]) -> list[list[float]]:
        results = []
        for text in texts:
            # 用 SHA-256 生成确定性种子
            seed_bytes = hashlib.sha256(text.encode("utf-8")).digest()
            seed = int.from_bytes(seed_bytes[:4], "big")
            rng = np.random.default_rng(seed)

            # 基础随机向量（由 seed 确定）
            base = rng.standard_normal(dim).astype(np.float32)

            # 叠加文本统计特征（增强区分度）
            char_codes = np.array([ord(c) % 256 for c in text[:256]], dtype=np.float32)
            if len(char_codes) > 0:
                feat = np.zeros(dim, dtype=np.float32)
                for i, code in enumerate(char_codes):
                    feat[(i * 7 + int(code)) % dim] += math.sin(code / 128.0)
                feat_norm = np.linalg.norm(feat)
                if feat_norm > 0:
                    base += feat / feat_norm * 0.3

            # L2 归一化
            norm = np.linalg.norm(base)
            if norm > 0:
                base = base / norm
            results.append(base.tolist())
        return results

    return embed_texts
