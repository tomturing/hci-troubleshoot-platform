"""
Embedding 服务 — 双模式切换（LLM 网关主力 + bge-small-zh 本地降级）

设计说明：
- 主力：LLM_BASE_URL（复用 hci-common-config 已注入的 LLM 公共端点，OpenAI-compatible API）
- 降级：本地 bge-small-zh-v1.5（网络超时/故障时自动切换）
- 连续降级计数：连续 3 次 LLM 失败后，自动切换到本地模式，5 分钟后重试
- 所有 embedding 调用都通过 OTel 追踪（embedding_latency, fallback_count）

注意事项：
- LLM_BASE_URL 与 OPENCLAW/DashScope 使用同一端点，embedding 模型名由 LLM_EMBEDDING_MODEL 配置
- 本地 BGE 模型路径由环境变量 BGE_MODEL_PATH 配置，不存在时跳过降级
- 向量维度固定为 1536（与 DB vector(1536) 一致；LLM 端点返回 1536 维）

搜索路径 vs 入库路径的不同降级策略：
- embed_for_search(): 不允许 hash 降级。embedding 失败 → 直接抛异常 → 调用方使用 BM25 替代
  （hash 向量与存储的真实向量无语义关联，做向量搜索等同随机排序，危害大于无降级）
- embed_batch(): 允许 hash 降级，用于入库。KBD 发布时 embedding 失败不阻断流程，
  待服务恢复后可批量重新生成。
"""

from __future__ import annotations

import asyncio
import time
from typing import TYPE_CHECKING

import httpx
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id

if TYPE_CHECKING:
    from app.config import Settings

logger = get_logger("kb-service-embedding")

# 连续失败阈值：超过此次数后切换到本地模式
_FALLBACK_THRESHOLD = 3
# 熔断冷却时间（秒）：切换到本地模式后，等待此时间后重试
_COOLDOWN_SECS = 300


class EmbeddingService:
    """双模式 Embedding 服务

    Usage:
        service = EmbeddingService(settings)
        # 搜索专用（不允许 hash 降级）
        vector = await service.embed_for_search("用户症状描述")
        # 入库专用（允许 hash 降级兜底）
        vectors = await service.embed_batch(["文本1", "文本2"])
    """

    def __init__(self, settings: Settings):
        self._settings = settings
        self._consecutive_failures = 0
        self._local_mode_until: float = 0.0  # Unix timestamp：本地模式截止时间
        self._local_model = None  # 懒加载本地模型

    async def embed_single(self, text: str) -> list[float]:
        """获取单条文本的 embedding 向量（入库路径，允许 hash 降级）"""
        results = await self.embed_batch([text])
        return results[0]

    async def embed_for_search(self, text: str) -> list[float]:
        """搜索专用：仅返回真实 embedding，失败时抛异常（不降级 hash）

        Rationale（第一性原理）：
        - hash 向量是 SHA-256 种子的随机向量，与存储的真实 embedding 在语义上完全无关联。
        - 用 hash(query) 和 real_embedding(KBD) 做 cosine 相似度 = 随机排序，
          等价于不做向量搜索。此时应由 BM25 接管，而非用"假向量"污染结果。
        - 调用方（kbd_search.py）捕获此异常后回退到 BM25 全文检索。

        Raises:
            RuntimeError: LLM embedding 不可用（调用方应使用 BM25 替代）
        """
        trace_id = get_current_trace_id()
        t_start = time.monotonic()

        # 搜索路径：仅尝试 LLM 端点，不走 hash 降级
        if not self._should_use_local():
            try:
                result = await self._embed_via_llm([text])
                self._consecutive_failures = 0
                logger.info(
                    event="embedding_search_success",
                    latency_ms=int((time.monotonic() - t_start) * 1000),
                    trace_id=trace_id,
                )
                return result[0]
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
                self._consecutive_failures += 1
                if self._consecutive_failures >= _FALLBACK_THRESHOLD:
                    self._local_mode_until = time.monotonic() + _COOLDOWN_SECS
                    logger.warning(
                        event="embedding_circuit_open",
                        message=f"LLM embedding 连续失败 {_FALLBACK_THRESHOLD} 次，熔断 {_COOLDOWN_SECS}s",
                        trace_id=trace_id,
                    )
                raise RuntimeError(
                    f"搜索 embedding 不可用（LLM 端点失败，BM25 将接管）: {exc}"
                ) from exc

        # 熔断期间：直接抛出，不降级 hash
        raise RuntimeError(
            f"搜索 embedding 处于熔断期（冷却至 {self._local_mode_until:.0f}），BM25 将接管"
        )

    async def embed_batch(self, texts: list[str]) -> list[list[float]]:
        """批量获取 embedding 向量（入库路径，允许 hash 降级兜底）

        Args:
            texts: 待 embed 的文本列表

        Returns:
            与 texts 等长的向量列表，每个向量长度为 EMBEDDING_DIM

        Raises:
            RuntimeError: 两路 embedding 均失败时抛出
        """
        trace_id = get_current_trace_id()
        t_start = time.monotonic()

        # 判断是否处于本地模式
        use_local = self._should_use_local()

        if not use_local:
            try:
                result = await self._embed_via_llm(texts)
                self._consecutive_failures = 0  # 成功后重置计数
                logger.info(
                    event="embedding_llm_success",
                    count=len(texts),
                    latency_ms=int((time.monotonic() - t_start) * 1000),
                    trace_id=trace_id,
                )
                return result
            except (httpx.TimeoutException, httpx.HTTPStatusError, httpx.RequestError) as exc:
                self._consecutive_failures += 1
                logger.warning(
                    event="embedding_llm_failed",
                    error=str(exc),
                    consecutive_failures=self._consecutive_failures,
                    threshold=_FALLBACK_THRESHOLD,
                    trace_id=trace_id,
                )
                if self._consecutive_failures >= _FALLBACK_THRESHOLD:
                    self._local_mode_until = time.monotonic() + _COOLDOWN_SECS
                    logger.warning(
                        event="embedding_circuit_open",
                        message=f"LLM embedding 连续失败 {_FALLBACK_THRESHOLD} 次，切换到本地模式 {_COOLDOWN_SECS}s",
                        trace_id=trace_id,
                    )

        # 降级：本地 bge-small-zh（仅用于入库，不用于搜索）
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
        # 冷却结束，重置状态，重新尝试 LLM
        self._local_mode_until = 0.0
        self._consecutive_failures = 0
        logger.info(event="embedding_circuit_close", message="熔断冷却结束，重新尝试 LLM embedding")
        return False

    async def _embed_via_llm(self, texts: list[str]) -> list[list[float]]:
        """通过 LLM 网关获取 embedding（OpenAI-compatible 格式）

        使用 LLM_BASE_URL / LLM_API_KEY，与 hci-common-config 中的 LLM 公共配置对齐。
        """
        async with httpx.AsyncClient(timeout=self._settings.EMBEDDING_TIMEOUT_SEC) as client:
            response = await client.post(
                f"{self._settings.LLM_BASE_URL}/embeddings",
                headers={"Authorization": f"Bearer {self._settings.LLM_API_KEY}"},
                json={
                    "model": self._settings.LLM_EMBEDDING_MODEL,
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

        ⚠️  numpy hash 降级仅用于入库（embed_batch），搜索路径（embed_for_search）
            在 LLM 不可用时直接抛异常，不走到此处。
        """
        if self._local_model is None:
            import os

            model_path = self._settings.BGE_MODEL_PATH

            # 尝试 sentence_transformers
            try:
                from sentence_transformers import SentenceTransformer  # type: ignore

                if os.path.exists(model_path):
                    loop = asyncio.get_event_loop()
                    self._local_model = await loop.run_in_executor(None, lambda: SentenceTransformer(model_path))
                    logger.info(event="local_model_loaded", model_path=model_path, backend="sentence_transformers")
                    return self._local_model
            except ImportError:
                logger.warning(
                    event="sentence_transformers_unavailable",
                    message="sentence_transformers 未安装，降级到 numpy hash embedding",
                )

            # 降级：numpy hash-based embedding（仅用于入库，不用于搜索）
            self._local_model = _make_numpy_hash_embedder(self._settings.EMBEDDING_DIM)
            logger.warning(
                event="using_hash_embedding",
                message="使用 numpy hash embedding 降级（仅限入库路径）。此向量无语义，搜索路径已禁止使用。",
            )

        return self._local_model


def _make_numpy_hash_embedder(dim: int = 1536):
    """创建一个基于 numpy 的确定性 hash-based embedding 函数

    ⚠️  仅用于 embed_batch()（入库降级）。搜索路径（embed_for_search）禁止使用此函数。

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
