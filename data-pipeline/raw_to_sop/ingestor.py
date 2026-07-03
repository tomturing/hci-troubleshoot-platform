"""
data-pipeline/raw_to_sop/ingestor.py — API 入库客户端

独立实现的 httpx 客户端，调用 kb-service POST /api/sop/ingest。
与 kbd/import_sop.py 的逻辑等价，但完全独立，不跨模块 import。
"""
from __future__ import annotations

import hashlib
import logging
from typing import Any

import httpx

logger = logging.getLogger("raw_to_sop.ingestor")


async def ingest_sop_markdown(
    *,
    kb_service_url: str,
    token: str,
    source_id: str,
    title: str,
    content_md: str,
    category_id: str | None = None,
    timeout: float = 120.0,
) -> dict[str, Any]:
    """调用 POST /api/sop/ingest 将 SOP Markdown 入库。

    Args:
        kb_service_url: kb-service 基础 URL（如 http://localhost:8004）
        token:          INTERNAL_API_TOKEN
        source_id:      幂等 key（重复提交同 source_id 会触发 override 逻辑）
        title:          SOP 文档标题
        content_md:     完整 Markdown 内容
        category_id:    分类编码（可选）
        timeout:        HTTP 请求超时（秒）

    Returns:
        {
            "success": bool,
            "document_id": int | None,
            "status": str,          # "draft" | "published"
            "message": str,
        }

    Raises:
        httpx.HTTPStatusError: API 返回非 2xx 状态码
        httpx.TimeoutException:  请求超时
    """
    # 计算 content_md 的 SHA256 作为 docx_hash（用于幂等去重，等价于文件 hash）
    content_hash = hashlib.sha256(content_md.encode("utf-8")).hexdigest()

    url = f"{kb_service_url.rstrip('/')}/api/sop/ingest"
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {
        "source_id": source_id,
        "title": title,
        "content_md": content_md,
        "docx_hash": content_hash,
    }
    if category_id:
        payload["category_id"] = category_id

    logger.info(
        "POST /api/sop/ingest source_id=%s title=%s hash=%s",
        source_id,
        title[:40],
        content_hash[:16],
    )

    async with httpx.AsyncClient(timeout=timeout) as client:
        response = await client.post(url, headers=headers, json=payload)
        response.raise_for_status()
        data = response.json()

    logger.info(
        "入库成功 document_id=%s status=%s",
        data.get("document_id"),
        data.get("status"),
    )
    return {
        "success": True,
        "document_id": data.get("document_id"),
        "status": data.get("status", "draft"),
        "message": data.get("message", ""),
    }
