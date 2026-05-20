"""
data-pipeline/kbd/classifier.py — AI 分类器（API 调用版）

功能：
  对 kbd_entry 中 status='draft' 且 ai_category_id 为空的条目，
  调用 kb-service API `/api/kb/classify` 进行分类。

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
from typing import Any

import asyncpg
import httpx

from .config import settings

logger = logging.getLogger("kbd.classifier")


# ─── API 客户端 ──────────────────────────────────────────────────────────────


async def _call_classify_api(
    title: str,
    problem_desc: str,
    client: httpx.AsyncClient,
) -> dict[str, Any]:
    """
    调用 kb-service 分类 API。

    Args:
        title: 案例标题
        problem_desc: 问题描述
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
    url = f"{settings.KB_SERVICE_URL}/api/kb/classify"
    headers = {
        "Authorization": f"Bearer {settings.INTERNAL_API_TOKEN}",
        "Content-Type": "application/json",
    }
    payload = {
        "title": title,
        "problem_desc": problem_desc[:2000] if problem_desc else "",  # 截断防止超长
    }

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
                "分类 API 超时 title=%s 等待 %.1fs 后重试",
                title[:30], wait
            )
            await asyncio.sleep(wait)

        except httpx.HTTPStatusError as exc:
            # 4xx 客户端错误不重试
            if 400 <= exc.response.status_code < 500:
                logger.error(
                    "分类 API 客户端错误 status=%d title=%s",
                    exc.response.status_code, title[:30]
                )
                raise
            # 5xx 服务端错误重试
            if attempt == settings.API_MAX_RETRIES - 1:
                raise
            wait = 1.0 * (2 ** attempt)
            logger.warning(
                "分类 API 服务端错误 status=%d 等待 %.1fs 后重试",
                exc.response.status_code, wait
            )
            await asyncio.sleep(wait)

    raise RuntimeError("unreachable")


# ─── 分类逻辑 ────────────────────────────────────────────────────────────────


async def classify_case(
    case_id: str,
    pool: asyncpg.Pool,
    client: httpx.AsyncClient,
) -> dict[str, object]:
    """
    对单个案例调用 kb-service API 分类。

    Returns:
        {"category_id": "...", "confidence": 0.85, "reason": "...", "status": "done"/"failed"}
    """
    # 从 kbd_entry 读取标题和内容
    row = await pool.fetchrow(
        """SELECT title, content_md FROM kbd_entry
           WHERE support_id = $1 AND (ai_category_id IS NULL OR ai_category_id = '')""",
        case_id,
    )
    if not row:
        logger.debug("案例 %s 不存在或已分类，跳过", case_id)
        return {"category_id": None, "confidence": 0.0, "reason": "已分类或不存在", "status": "skipped"}

    title = row["title"] or ""

    # 从 content_md 提取问题描述（第一个 ## 问题描述 章节的内容）
    problem_desc = _extract_problem_desc(row["content_md"] or "")

    try:
        result = await _call_classify_api(title, problem_desc, client)

        category_id = result.get("category_id")
        confidence = float(result.get("confidence", 0.0))
        reason = str(result.get("reason") or "")
        needs_review = result.get("needs_review", False)

        # 更新 kbd_entry
        await pool.execute(
            """UPDATE kbd_entry
               SET ai_category_id=$1, ai_category_conf=$2, ai_category_reason=$3, updated_at=NOW()
               WHERE support_id=$4""",
            category_id,
            confidence,
            reason,
            case_id,
        )

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
        logger.error("分类失败 case_id=%s 原因=%s", case_id, exc)
        return {"category_id": None, "confidence": 0.0, "reason": f"API调用失败: {exc}", "status": "failed"}


def _extract_problem_desc(content_md: str) -> str:
    """从 content_md 提取问题描述章节内容"""
    if not content_md:
        return ""

    # 查找 "## 问题描述" 章节
    lines = content_md.split("\n")
    in_problem_section = False
    problem_lines: list[str] = []

    for line in lines:
        if line.strip().startswith("## 问题描述"):
            in_problem_section = True
            continue
        if in_problem_section:
            # 遇到下一个 ## 标题则停止
            if line.strip().startswith("## ") and not line.strip().startswith("## 问题描述"):
                break
            problem_lines.append(line)

    return "\n".join(problem_lines).strip()[:800]


async def classify_batch(
    case_ids: list[str],
    pool: asyncpg.Pool,
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
        for idx, case_id in enumerate(case_ids, 1):
            logger.info("[%d/%d] 分类案例 %s", idx, total, case_id)

            result = await classify_case(case_id, pool, client)

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
