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
from typing import Any

import asyncpg
import httpx

from .config import settings

logger = logging.getLogger("kbd.image_proc")


# ─── API 客户端 ──────────────────────────────────────────────────────────────


async def _call_reanalyze_api(
    kbd_entry_id: int,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """
    调用 kb-service 重新识图 API。

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

    Raises:
        httpx.HTTPStatusError: API 返回非 2xx
        httpx.TimeoutException: 请求超时
    """
    url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/reanalyze-images"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
    }

    # 带重试的请求（识图耗时较长，超时设置充足）
    timeout = max(settings.API_TIMEOUT, 300.0)  # 至少 5 分钟
    for attempt in range(settings.API_MAX_RETRIES):
        try:
            response = await client.post(url, headers=headers, timeout=timeout)
            response.raise_for_status()
            return response.json()

        except httpx.TimeoutException:
            if attempt == settings.API_MAX_RETRIES - 1:
                raise
            wait = 2.0 * (2 ** attempt)
            logger.warning(
                "识图 API 超时 kbd_entry_id=%d 等待 %.1fs 后重试",
                kbd_entry_id, wait,
            )
            await asyncio.sleep(wait)

        except httpx.HTTPStatusError as exc:
            if 400 <= exc.response.status_code < 500:
                logger.error(
                    "识图 API 客户端错误 status=%d kbd_entry_id=%d",
                    exc.response.status_code, kbd_entry_id,
                )
                raise
            if attempt == settings.API_MAX_RETRIES - 1:
                raise
            wait = 2.0 * (2 ** attempt)
            logger.warning(
                "识图 API 服务端错误 status=%d 等待 %.1fs 后重试",
                exc.response.status_code, wait,
            )
            await asyncio.sleep(wait)

    raise RuntimeError("unreachable")


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
            dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1)
        )

    try:
        stats = {"done": 0, "failed": 0, "skipped": 0}

        async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
            for idx, support_id in enumerate(kbd_ids, 1):
                # 查询 kbd_entry.id
                kbd_entry_id = await pool.fetchval(
                    "SELECT id FROM kbd_entry WHERE support_id = $1", support_id
                )
                if kbd_entry_id is None:
                    logger.warning(
                        "[%d/%d] support_id=%s 在 kbd_entry 表中不存在，跳过",
                        idx, len(kbd_ids), support_id,
                    )
                    stats["skipped"] += 1
                    continue

                logger.info(
                    "[%d/%d] 重新识图 support_id=%s kbd_entry_id=%d",
                    idx, len(kbd_ids), support_id, kbd_entry_id,
                )

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
                    logger.error(
                        "识图失败 support_id=%s 原因=%s", support_id, exc
                    )
                    stats["failed"] += 1

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
        dsn=settings.DATABASE_URL.replace("postgres://", "postgresql://", 1)
    )
    try:
        return await process_images_batch([kbd_id], pool)
    finally:
        await pool.close()


def get_failed_vision_ids(kbd_ids: list[str]) -> list[str]:
    """筛选 Vision 处理失败的案例（兼容旧接口，返回空列表）。

    新架构下失败统计由 kb-service 返回，此函数仅用于兼容旧调用方。
    """
    return []


def _has_failed_vision(kbd_dir: Any) -> bool:
    """兼容旧接口，新架构不再使用本地文件标记。"""
    return False
