"""
backend/kb-service/app/services/signal_asset_service.py
信号建模资产（模板库、最佳实践库、异常复盘记录）服务层
提供带内存缓存的高性能数据访问与异常持久化
"""
from __future__ import annotations

import logging
import time
from typing import Any

from app.models.signal_assets import SignalBestPractice, SignalFailureExtraction, SignalModelingTemplate
from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession

logger = logging.getLogger("signal_asset_service")

# 内存缓存结构：{key: (expire_at, data)}
_CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 60.0


class SignalAssetService:
    @classmethod
    async def get_all_templates(cls, session: AsyncSession) -> dict[str, dict[str, Any]]:
        """获取所有激活的信号类型模板（带内存 TTL 缓存）"""
        cache_key = "all_templates"
        now = time.monotonic()
        if cache_key in _CACHE:
            expire_at, val = _CACHE[cache_key]
            if now < expire_at:
                return val

        res = await session.execute(
            select(SignalModelingTemplate).where(SignalModelingTemplate.is_active.is_(True))
        )
        templates = res.scalars().all()
        result = {
            t.tool_name: {
                "id": t.id,
                "tool_name": t.tool_name,
                "category": t.category,
                "description": t.description,
                "acquire_schema": t.acquire_schema or {},
                "allowed_matcher_types": t.allowed_matcher_types or [],
                "variable_protocol": t.variable_protocol or {},
                "anti_patterns": t.anti_patterns or [],
            }
            for t in templates
        }
        _CACHE[cache_key] = (now + CACHE_TTL_SECONDS, result)
        return result

    @classmethod
    async def get_best_practices_by_tool(
        cls,
        session: AsyncSession,
        tool_name: str,
        limit: int = 3,
    ) -> list[dict[str, Any]]:
        """按 tool_name 获取最佳实践黄金案例（Few-Shot 注入用，带内存 TTL 缓存）"""
        cache_key = f"bp:{tool_name}:{limit}"
        now = time.monotonic()
        if cache_key in _CACHE:
            expire_at, val = _CACHE[cache_key]
            if now < expire_at:
                return val

        res = await session.execute(
            select(SignalBestPractice)
            .where(
                SignalBestPractice.tool_name == tool_name,
                SignalBestPractice.is_active.is_(True),
            )
            .order_by(desc(SignalBestPractice.completeness_score), desc(SignalBestPractice.id))
            .limit(limit)
        )
        practices = res.scalars().all()
        result = [
            {
                "id": bp.id,
                "tool_name": bp.tool_name,
                "pattern_category": bp.pattern_category,
                "support_id": bp.support_id,
                "raw_evidence": bp.raw_evidence,
                "signal_json": bp.signal_json,
                "design_notes": bp.design_notes,
            }
            for bp in practices
        ]
        _CACHE[cache_key] = (now + CACHE_TTL_SECONDS, result)
        return result

    @classmethod
    async def record_failure(
        cls,
        session: AsyncSession,
        *,
        kbd_id: int | None,
        stage: str,
        raw_content: str,
        reason: str,
        detail_payload: dict[str, Any] | None = None,
    ) -> int:
        """记录抽取链路异常到 signal_failure_extraction 表，不抛出异常阻塞主流程"""
        try:
            record = SignalFailureExtraction(
                kbd_id=kbd_id,
                stage=stage,
                raw_content=raw_content[:4000] if raw_content else "",
                reason=reason,
                detail_payload=detail_payload or {},
            )
            session.add(record)
            await session.flush()
            logger.info("已沉淀抽取异常记录 id=%d stage=%s reason=%s kbd_id=%s", record.id, stage, reason, kbd_id)
            return record.id
        except Exception as exc:
            logger.warning("记录抽取异常失败（忽略此错误以免影响主链路）: %s", exc)
            return -1
