"""
data-pipeline/kbd/classifier.py — AI 分类器（API 调用版）

功能：
  对 kbd_entry 中 status='draft' 且 ai_category_id 为空的条目，
  调用 kb-service API `/api/admin/kbd/{id}/reclassify` 进行分类并统一落 Proposal revision。

变更（T2-02）：
  - 废弃本地 LLM 调用和 category_baseline.yaml 直接读取
  - 改为调用 kb-service API，由服务端统一管理分类树和 LLM 调用
  - API 返回 category_id（分类编码如 "虚拟机-001"）、confidence、reason

设计特点：
  - 使用 httpx 异步客户端调用 API
  - 从环境变量读取 KB_SERVICE_URL 和 INTERNAL_API_TOKEN
  - 完善的错误处理和重试机制
  - 低置信度（< MIN_CLASSIFY_CONFIDENCE）时标记，提示人工重新分类
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any

import asyncpg
import httpx

from .config import settings
from .error_catalog import humanize_error
from .observability import traceparent

logger = logging.getLogger("kbd.classifier")


# ─── API 客户端 ──────────────────────────────────────────────────────────────


async def _call_classify_api(
    kbd_entry_id: int,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """
    调用 kb-service 分类 API。

    Args:
        kbd_entry_id: KBD 主键；服务端据此读取权威输入并原子落库
        client: httpx 异步客户端

    Returns:
        {
            "category_id": "虚拟机-001",
            "confidence": 0.85,
            "reason": "分类理由",
            "top3": [...],
            "needs_review": false
        }

    Raises:
        httpx.HTTPStatusError: API 返回非 2xx 状态码
        httpx.TimeoutException: 请求超时
    """
    url = f"{settings.KB_SERVICE_URL}/api/admin/kbd/{kbd_entry_id}/reclassify"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
        # 注入 W3C traceparent：与 kb-service 日志共享 trace_id（见 observability.py）。
        **traceparent(),
    }
    # 带重试的请求
    max_attempts = max(1, settings.API_MAX_RETRIES)
    for attempt in range(max_attempts):
        try:
            response = await client.post(
                url,
                headers=headers,
                timeout=settings.API_TIMEOUT,
            )
            response.raise_for_status()
            return response.json()

        except (httpx.TimeoutException, httpx.TransportError):
            if attempt == max_attempts - 1:
                raise
            wait = random.uniform(0.0, min(30.0, 2.0 ** attempt))
            logger.warning(
                "分类 API 超时 kbd_entry_id=%s 等待 %.1fs 后重试",
                kbd_entry_id, wait
            )
            await asyncio.sleep(wait)

        except httpx.HTTPStatusError as exc:
            status_code = exc.response.status_code
            # 429 是 Provider 暂态限流，必须遵守 Retry-After；其他 4xx 是调用错误。
            if status_code == 429:
                retry_after = exc.response.headers.get("Retry-After")
                try:
                    wait = max(0.0, float(retry_after)) if retry_after else random.uniform(0.0, 2.0 ** attempt)
                except (TypeError, ValueError):
                    wait = random.uniform(0.0, 2.0 ** attempt)
            elif 400 <= status_code < 500:
                logger.error(
                    "分类 API 客户端错误 status=%d kbd_entry_id=%s",
                    status_code, kbd_entry_id,
                )
                raise
            else:
                wait = random.uniform(0.0, min(30.0, 2.0 ** attempt))
            if attempt == max_attempts - 1:
                raise
            logger.warning(
                "分类 API 服务端错误 status=%d 等待 %.1fs 后重试",
                status_code, wait
            )
            await asyncio.sleep(wait)

    raise RuntimeError("unreachable")


# ─── 分类逻辑 ────────────────────────────────────────────────────────────────


async def classify_case(
    case_id: str,
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
    *,
    rework: bool = False,
) -> dict[str, object]:
    """
    对单个案例调用 kb-service API 分类。

    Returns:
        {"category_id": "...", "confidence": 0.85, "reason": "...", "status": "done"/"failed"}
    """
    # 只读取稳定主键；标题和问题描述由 kb-service 在生成与落库时读取同一权威记录。
    row = await pool.fetchrow(
        """SELECT id, ai_category_id FROM kbd_entry
           WHERE support_id = $1 AND status = 'draft'""",
        case_id,
    )
    if not row:
        logger.debug("案例 %s 不存在，跳过", case_id)
        return {"category_id": None, "confidence": 0.0, "reason": "已分类或不存在", "status": "skipped"}

    # 幂等运行时，已有分类的案例已经满足 Stage 3 的最终状态，计入 done。
    # 这样批量日志反映案例完成情况，而不是只反映本轮实际调用 API 的数量。
    try:
        existing_category_id = row["ai_category_id"]
    except (KeyError, IndexError):
        # 兼容只返回 id 的旧测试桩/连接层；此时按未分类处理。
        existing_category_id = None
    if existing_category_id and not rework:
        return {
            "category_id": existing_category_id,
            "confidence": 1.0,
            "reason": "已有分类，跳过重复调用",
            "status": "done",
            "already_classified": True,
        }

    try:
        result = await _call_classify_api(int(row["id"]), client)

        category_id = result.get("category_id")
        confidence = float(result.get("confidence", 0.0))
        reason = str(result.get("reason") or "")
        needs_review = result.get("needs_review", False)

        logger.debug(
            "分类完成 case_id=%s category=%s conf=%.2f needs_review=%s",
            case_id, category_id, confidence, needs_review
        )

        return {
            "category_id": category_id,
            "confidence": confidence,
            "reason": reason,
            "status": "done",
            "needs_review": needs_review,
        }

    except Exception as exc:
        error = humanize_error(exc)
        logger.error(
            "分类失败 case_id=%s code=%s retryable=%s 原因=%s",
            case_id, error.code, error.retryable, error.message,
            extra={
                "support_id": case_id,
                "stage": "classify",
                "error_code": error.code,
                "retryable": error.retryable,
            },
        )
        return {"category_id": None, "confidence": 0.0, "reason": f"API调用失败: {exc}", "status": "failed"}
async def classify_batch(
    case_ids: list[str],
    pool: asyncpg.Pool,
    *,
    rework: bool = False,
) -> dict[str, int]:
    """
    批量对未分类的 kbd_entry 进行 AI 分类。

    Returns:
        {"done": N, "failed": N, "low_confidence": N, "skipped": N}
    """
    stats = {"done": 0, "failed": 0, "low_confidence": 0, "skipped": 0}
    total = len(case_ids)

    if not settings.INTERNAL_API_TOKEN:
        raise RuntimeError("INTERNAL_API_TOKEN 未配置，无法调用 kb-service API")

    async with httpx.AsyncClient(timeout=settings.API_TIMEOUT) as client:
        sem = asyncio.Semaphore(getattr(settings, "CLASSIFY_CONCURRENCY", 2))

        async def _run_one(idx: int, case_id: str) -> dict[str, object]:
            async with sem:
                logger.info("[%d/%d] 分类案例 %s", idx, total, case_id)
                return await classify_case(case_id, pool, client, rework=rework)

        results = await asyncio.gather(
            *[_run_one(idx, case_id) for idx, case_id in enumerate(case_ids, 1)]
        )
        for result in results:
            status = result.get("status", "failed")
            if status == "done":
                stats["done"] += 1
                if result.get("needs_review") or result.get("confidence", 0) < settings.MIN_CLASSIFY_CONFIDENCE:
                    stats["low_confidence"] += 1
            elif status == "skipped":
                stats["skipped"] += 1
            else:
                stats["failed"] += 1

    logger.info(
        "批量分类完成 done=%d failed=%d skipped=%d low_conf=%d",
        stats["done"], stats["failed"], stats["skipped"], stats["low_confidence"],
    )
    return stats


# ─── 旧版兼容接口（保留用于 pipeline.py）───────────────────────────────────────

# 旧版 _load_categories 和 _format_category_list 函数已废弃
# 分类逻辑现在由 kb-service API 统一提供
