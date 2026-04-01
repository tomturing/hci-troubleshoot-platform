"""
scripts/kbd/importer.py — 将 converter 结果写入 kbd_entry

功能：
  从文件缓存（cache/{support_id}/raw.json）通过 converter 生成 content_md，
  然后将结果写入 PostgreSQL kbd_entry 表（status 默认 'draft'，等待人工审核）。

幂等规则：
  - support_id UNIQUE：已有 draft 记录 → 更新内容
  - 已有非 draft 状态（published/archived/rejected）→ 跳过（force_draft 可覆盖）
"""
from __future__ import annotations

import logging
from typing import Any

import asyncpg

from .config import settings

logger = logging.getLogger("kbd.importer")


async def import_entry(
    support_id: str,
    pool: asyncpg.Pool,
    *,
    force_draft: bool = False,
) -> str:
    """
    将单个案例的处理结果写入 kbd_entry。

    Args:
        support_id:  案例 ID（与 raw.json 目录名一致）
        pool:        asyncpg 连接池
        force_draft: True 时即使已进入审核/发布，也强制更新内容（危险！）

    Returns:
        "created" | "updated" | "skipped" | "error"
    """
    from .converter import convert_case_with_meta

    # 检查是否已有记录
    existing = await pool.fetchrow(
        "SELECT id, status FROM kbd_entry WHERE support_id = $1",
        support_id,
    )
    if existing and existing["status"] not in ("draft",) and not force_draft:
        logger.info("案例 %s 已处于 %s 状态，跳过", support_id, existing["status"])
        return "skipped"

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

    import json
    metadata_json = json.dumps(metadata, ensure_ascii=False)

    if existing:
        # 更新已有 draft 记录
        await pool.execute(
            """UPDATE kbd_entry
               SET title       = $1,
                   support_url = $2,
                   content_md  = $3,
                   metadata    = $4::jsonb,
                   updated_at  = NOW()
               WHERE support_id = $5""",
            title, support_url, content_md, metadata_json, support_id,
        )
        logger.info("案例 %s 已更新（draft）", support_id)
        return "updated"
    else:
        # 创建新记录
        await pool.execute(
            """INSERT INTO kbd_entry
                   (support_id, support_url, title, content_md, metadata, status)
               VALUES ($1, $2, $3, $4, $5::jsonb, 'draft')""",
            support_id, support_url, title, content_md, metadata_json,
        )
        logger.info("案例 %s 已创建（draft）", support_id)
        return "created"


async def import_batch(
    support_ids: list[str],
    pool: asyncpg.Pool,
    *,
    force_draft: bool = False,
) -> dict[str, int]:
    """
    批量导入 kbd_entry。

    Returns:
        {"created": N, "updated": N, "skipped": N, "error": N}
    """
    stats: dict[str, int] = {"created": 0, "updated": 0, "skipped": 0, "error": 0}
    total = len(support_ids)

    for idx, support_id in enumerate(support_ids, 1):
        logger.info("[%d/%d] 导入案例 %s", idx, total, support_id)
        status = await import_entry(support_id, pool, force_draft=force_draft)
        stats[status] = stats.get(status, 0) + 1

    logger.info(
        "批量导入完成 created=%d updated=%d skipped=%d error=%d",
        stats["created"], stats["updated"], stats["skipped"], stats["error"],
    )
    return stats


async def get_pending_review_cases(pool: asyncpg.Pool, limit: int = 50) -> list[dict]:
    """查询待审核案例列表（status='draft'，已完成 AI 分类的优先）"""
    rows = await pool.fetch(
        """SELECT
               e.support_id,
               e.title,
               e.support_url,
               e.status,
               e.ai_category_id,
               e.ai_category_conf,
               e.ai_category_reason,
               e.created_at,
               e.updated_at,
               c.label AS ai_category_label
           FROM kbd_entry e
           LEFT JOIN kb_category c ON c.code = e.ai_category_id
           WHERE e.status = 'draft'
           ORDER BY e.ai_category_conf DESC NULLS LAST, e.created_at ASC
           LIMIT $1""",
        limit,
    )
    return [dict(r) for r in rows]
