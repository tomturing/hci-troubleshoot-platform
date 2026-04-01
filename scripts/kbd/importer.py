"""
scripts/kbd/importer.py — KBD 条目入库（API 调用版）

功能：
  从文件缓存（cache/{support_id}/raw.json）通过 converter 生成 content_md，
  然后调用 kb-service API `/api/kbd/ingest` 写入 kbd_entry 表。

变更（T2-03）：
  - 不再直接写数据库（废弃 asyncpg 直接写入）
  - 改为调用 kb-service API `/api/kbd/ingest`
  - API 端负责写入 kbd_entry 表，状态默认 draft
  - 幂等性由 API 端 support_id 唯一性校验保证

幂等规则：
  - support_id UNIQUE：API 端已有 draft 记录 → 返回已存在提示
  - 已有非 draft 状态（published/archived/rejected）→ API 返回已存在信息

调用方：
  - pipeline.py Stage 3（import）
  - CLI: python -m scripts.kbd.run import --ids xxx
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import httpx

from .config import settings

logger = logging.getLogger("kbd.importer")


# ─── API 客户端 ──────────────────────────────────────────────────────────────


async def _call_kbd_ingest_api(
    support_id: str,
    title: str,
    support_url: str | None,
    content_md: str,
    metadata: dict[str, Any],
    ai_category_id: str | None = None,
    ai_category_conf: float | None = None,
    ai_category_reason: str | None = None,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """
    调用 kb-service KBD 入库 API。

    Args:
        support_id: 案例 ID（幂等键）
        title: 案例标题
        support_url: 原始案例 URL
        content_md: 结构化 Markdown 内容
        metadata: 补充元数据
        ai_category_id: AI 分类建议 ID（可选）
        ai_category_conf: 分类置信度（可选）
        ai_category_reason: 分类理由（可选）
        client: httpx 异步客户端（可选，不传则创建临时客户端）

    Returns:
        {"success": true, "kbd_id": 123, "status": "draft", "message": "..."}

    Raises:
        httpx.HTTPStatusError: API 返回非 2xx 状态码
        httpx.TimeoutException: 请求超时
    """
    url = f"{settings.KB_SERVICE_URL}/api/kb/kbd/ingest"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "support_id": support_id,
        "support_url": support_url,
        "title": title,
        "content_md": content_md,
        "metadata": metadata,
        "ai_category_id": ai_category_id,
        "ai_category_conf": ai_category_conf,
        "ai_category_reason": ai_category_reason,
    }

    # 使用传入的 client 或创建临时客户端
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        should_close = True

    try:
        # 带重试的请求
        for attempt in range(settings.API_MAX_RETRIES):
            try:
                response = await client.post(
                    url,
                    headers=headers,
                    json=payload,
                    timeout=settings.API_TIMEOUT,
                )
                response.raise_for_status()
                return response.json()

            except httpx.TimeoutException:
                if attempt == settings.API_MAX_RETRIES - 1:
                    raise
                wait = 1.0 * (2 ** attempt)
                logger.warning(
                    "入库 API 超时 support_id=%s 等待 %.1fs 后重试",
                    support_id, wait
                )
                await asyncio.sleep(wait)

            except httpx.HTTPStatusError as exc:
                # 4xx 客户端错误不重试
                if 400 <= exc.response.status_code < 500:
                    logger.error(
                        "入库 API 客户端错误 status=%d support_id=%s",
                        exc.response.status_code, support_id
                    )
                    raise
                # 5xx 服务端错误重试
                if attempt == settings.API_MAX_RETRIES - 1:
                    raise
                wait = 1.0 * (2 ** attempt)
                logger.warning(
                    "入库 API 服务端错误 status=%d 等待 %.1fs 后重试",
                    exc.response.status_code, wait
                )
                await asyncio.sleep(wait)

        raise RuntimeError("unreachable")

    finally:
        if should_close:
            await client.aclose()


# ─── 入库逻辑 ────────────────────────────────────────────────────────────────


async def import_entry(
    support_id: str,
    client: httpx.AsyncClient,
    *,
    force_draft: bool = False,
) -> str:
    """
    将单个案例的处理结果通过 API 写入 kbd_entry。

    Args:
        support_id:  案例 ID（与 raw.json 目录名一致）
        client:      httpx 异步客户端（共享连接）
        force_draft: 已废弃，由 API 端处理幂等逻辑

    Returns:
        "created" | "updated" | "skipped" | "error" | "idempotent"
    """
    from .converter import convert_case_with_meta

    # 转换：从文件缓存生成 content_md + metadata
    result = convert_case_with_meta(support_id)
    if not result:
        # 转换失败或缺少必填 section（已写 abnormal.json）
        logger.warning("案例 %s 转换结果为空，跳过（详见 abnormal.json）", support_id)
        return "error"

    title: str = result["title"]
    support_url: str = result["support_url"]
    content_md: str = result["content_md"]
    metadata: dict[str, Any] = result["metadata"]

    if not content_md.strip():
        logger.warning("案例 %s content_md 为空，跳过", support_id)
        return "error"

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    try:
        api_result = await _call_kbd_ingest_api(
            support_id=support_id,
            title=title,
            support_url=support_url,
            content_md=content_md,
            metadata=metadata,
            client=client,
        )

        success = api_result.get("success", False)
        message = api_result.get("message", "")

        if success:
            kbd_id = api_result.get("kbd_id")
            status = api_result.get("status", "draft")

            # 判断是新建还是已存在（幂等）
            if message and "已存在" in message:
                logger.info("案例 %s 已存在（kbd_id=%d status=%s）", support_id, kbd_id, status)
                return "idempotent"
            else:
                logger.info("案例 %s 已入库（kbd_id=%d status=%s）", support_id, kbd_id, status)
                return "created"
        else:
            logger.error("案例 %s 入库失败: %s", support_id, message)
            return "error"

    except httpx.HTTPStatusError as exc:
        logger.error("案例 %s API 调用失败 status=%d", support_id, exc.response.status_code)
        return "error"
    except Exception as exc:
        logger.error("案例 %s 入库异常: %s", support_id, exc)
        return "error"


async def import_batch(
    support_ids: list[str],
    _pool: Any = None,  # 废弃参数，保留兼容性
    *,
    force_draft: bool = False,
    client: httpx.AsyncClient | None = None,
) -> dict[str, int]:
    """
    批量导入 kbd_entry（通过 API）。

    Args:
        support_ids: 要导入的案例 ID 列表
        _pool: 废弃参数（原 asyncpg 连接池），保留向后兼容
        force_draft: 已废弃
        client: 可选的 httpx 客户端（不传则创建临时客户端）

    Returns:
        {"created": N, "idempotent": N, "skipped": N, "error": N}
    """
    stats: dict[str, int] = {"created": 0, "idempotent": 0, "skipped": 0, "error": 0}
    total = len(support_ids)

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    # 使用传入的 client 或创建临时客户端
    should_close = False
    if client is None:
        client = httpx.AsyncClient(timeout=settings.API_TIMEOUT)
        should_close = True

    try:
        for idx, support_id in enumerate(support_ids, 1):
            logger.info("[%d/%d] 导入案例 %s", idx, total, support_id)
            status = await import_entry(support_id, client, force_draft=force_draft)
            stats[status] = stats.get(status, 0) + 1

    finally:
        if should_close:
            await client.aclose()

    logger.info(
        "批量导入完成 created=%d idempotent=%d skipped=%d error=%d",
        stats["created"], stats["idempotent"], stats["skipped"], stats["error"],
    )
    return stats


# ─── 旧版兼容接口 ────────────────────────────────────────────────────────────────


async def get_pending_review_cases(
    _pool: Any,
    limit: int = 50,
) -> list[dict]:
    """
    查询待审核案例列表（已废弃，应调用 admin-service API）。

    注意：此函数保留向后兼容，但实际应通过 admin-service API 获取。
    如需使用，请调用 GET /api/admin/kb/pending 接口。
    """
    logger.warning(
        "get_pending_review_cases 已废弃，请改用 admin-service API: "
        "GET /api/admin/kb/pending"
    )
    return []
