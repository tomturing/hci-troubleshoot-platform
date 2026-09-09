"""
backend/kb-service/app/services/signal_asset_service.py
信号建模资产（模板库、最佳实践库、异常复盘记录）服务层
提供带内存缓存的高性能数据访问与异常持久化
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Mapping
from typing import Any

from shared.observability.otel import get_current_trace_id
from shared.schemas.acquirer_args import (
    ACQUIRER_ARGS_SCHEMA,
    BACKEND_TOOLS,
    DEFAULT_EXTERNAL_SIGNAL_VARIABLES,
    EXECUTABLE_SIGNAL_TOOLS,
    QKV_ALLOWED_PRODUCE_PATHS,
    QKV_STANDARD_OUTPUTS,
)
from shared.schemas.log_source_catalog import LOG_MATCHER_TYPES
from sqlalchemy import select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.signal_assets import SignalBestPractice, SignalFailureExtraction, SignalModelingTemplate

logger = logging.getLogger("signal_asset_service")

# 内存缓存结构：{key: (expire_at, data)}
_CACHE: dict[str, tuple[float, Any]] = {}
CACHE_TTL_SECONDS = 60.0


class SignalAssetService:
    @staticmethod
    def _infer_pattern_category(tool: str, signal: dict[str, Any]) -> str:
        args = (signal.get("acquire") or {}).get("args") or {}
        matcher = signal.get("match") or {}
        matcher_type = matcher.get("type") if isinstance(matcher, dict) else "produces"
        subject = args.get("file") or args.get("command") or args.get("keyword") or "标准契约"
        return f"{tool}:{matcher_type}({str(subject)[:24]})"

    @classmethod
    async def sync_published_kbd_best_practices(
        cls,
        session: AsyncSession,
        *,
        kbd: Any,
        source_revision: int,
        source_checksum: str,
    ) -> int:
        """把一次专家发布快照沉淀为不可变 Few-Shot 资产，并切换 active 指针。"""

        await session.execute(
            update(SignalBestPractice)
            .where(SignalBestPractice.source_kbd_id == kbd.id)
            .values(is_active=False)
        )
        templates = await session.execute(select(SignalModelingTemplate.id, SignalModelingTemplate.tool_name))
        template_ids = {tool_name: template_id for template_id, tool_name in templates.all()}
        document = getattr(kbd, "signals_json", None) or {}
        signals = document.get("signals") if isinstance(document, dict) else []
        activated = 0
        for signal in signals or []:
            if not isinstance(signal, dict):
                continue
            tool = str((signal.get("acquire") or {}).get("tool") or "")
            if tool not in EXECUTABLE_SIGNAL_TOOLS:
                continue
            signal_id = str(signal.get("id") or "").strip()
            if not signal_id:
                continue
            existing = await session.scalar(
                select(SignalBestPractice).where(
                    SignalBestPractice.source_kbd_id == kbd.id,
                    SignalBestPractice.source_checksum == source_checksum,
                    SignalBestPractice.signal_id == signal_id,
                )
            )
            if existing is not None:
                existing.is_active = True
                activated += 1
                continue
            args = (signal.get("acquire") or {}).get("args") or {}
            evidence = (
                (signal.get("provenance") or {}).get("evidence")
                or args.get("instruction")
                or getattr(kbd, "title", "")
            )
            session.add(
                SignalBestPractice(
                    template_id=template_ids.get(tool),
                    tool_name=tool,
                    pattern_category=cls._infer_pattern_category(tool, signal),
                    source_kbd_id=kbd.id,
                    support_id=getattr(kbd, "support_id", None),
                    source_revision=source_revision,
                    source_checksum=source_checksum,
                    signal_id=signal_id,
                    raw_evidence=str(evidence),
                    signal_json=json.loads(json.dumps(signal, ensure_ascii=False)),
                    design_notes=(signal.get("review") or {}).get("notes")
                    or f"来源于已发布 KBD {getattr(kbd, 'support_id', kbd.id)} 修订 {source_revision}",
                    completeness_score=10,
                    is_active=True,
                )
            )
            activated += 1
        cls.invalidate_cache()
        return activated

    @classmethod
    async def deactivate_kbd_best_practices(cls, session: AsyncSession, kbd_id: int) -> None:
        await session.execute(
            update(SignalBestPractice)
            .where(SignalBestPractice.source_kbd_id == kbd_id)
            .values(is_active=False)
        )
        cls.invalidate_cache()

    @staticmethod
    def invalidate_cache() -> None:
        _CACHE.clear()

    @classmethod
    async def get_all_templates(cls, session: AsyncSession) -> dict[str, dict[str, Any]]:
        """获取所有激活的信号类型模板（带内存 TTL 缓存）"""
        cache_key = "all_templates"
        now = time.monotonic()
        if cache_key in _CACHE:
            expire_at, val = _CACHE[cache_key]
            if now < expire_at:
                return val

        res = await session.execute(select(SignalModelingTemplate).where(SignalModelingTemplate.is_active.is_(True)))
        templates = res.scalars().all()
        stored = {
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
        result: dict[str, dict[str, Any]] = {}
        generic_matchers = ["keyword", "regex", "state", "boolean", "threshold", "delta", "trend", "exists"]
        for tool_name in sorted(EXECUTABLE_SIGNAL_TOOLS):
            descriptive = stored.get(tool_name, {})
            allowed_matchers = (
                list(LOG_MATCHER_TYPES)
                if tool_name == "qfk_log"
                else generic_matchers
                if tool_name in BACKEND_TOOLS
                else []
            )
            result[tool_name] = {
                "id": descriptive.get("id"),
                "tool_name": tool_name,
                "category": "backend" if tool_name in BACKEND_TOOLS else "frontend",
                "description": descriptive.get("description") or "由代码契约生成的 Signal 建模模板",
                # 机器可执行字段永远覆盖数据库里的历史描述性快照，避免旧模板继续
                # 诱导 LLM 生成已被 Handler/Schema 淘汰的参数。
                "acquire_schema": ACQUIRER_ARGS_SCHEMA[tool_name],
                "allowed_matcher_types": allowed_matchers,
                "variable_protocol": {
                    "external_variables": list(DEFAULT_EXTERNAL_SIGNAL_VARIABLES),
                    "allowed_produce_paths": sorted(QKV_ALLOWED_PRODUCE_PATHS.get(tool_name, ())),
                    "fixed_outputs": [
                        {"name": name, "path": path}
                        for name, path in QKV_STANDARD_OUTPUTS.get(tool_name, ())
                    ],
                },
                "anti_patterns": descriptive.get("anti_patterns") or [],
                "contract_source": "shared.schemas.acquirer_args",
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

        # 显式列查询，兼容迁移期间旧环境缺少新增 trace_id 列的情况。
        res = await session.execute(
            text(
                """
                SELECT id, tool_name, pattern_category, support_id,
                       source_revision, source_checksum, signal_id,
                       raw_evidence, signal_json, design_notes
                FROM signal_best_practice
                WHERE tool_name = :tool_name AND is_active = TRUE
                ORDER BY completeness_score DESC, id DESC LIMIT :limit
                """
            ),
            {"tool_name": tool_name, "limit": limit},
        )
        practices = res.mappings().all()
        # 保持轻量单测/替代驱动对旧 Result.scalars() 形态的兼容。
        if (not isinstance(practices, list) or not practices) and hasattr(res, "scalars"):
            practices = res.scalars().all()
        result = [
            {
                "id": bp["id"] if isinstance(bp, Mapping) else bp.id,
                "tool_name": bp["tool_name"] if isinstance(bp, Mapping) else bp.tool_name,
                "pattern_category": bp["pattern_category"] if isinstance(bp, Mapping) else bp.pattern_category,
                "support_id": bp["support_id"] if isinstance(bp, Mapping) else bp.support_id,
                "source_revision": bp["source_revision"] if isinstance(bp, Mapping) else bp.source_revision,
                "source_checksum": bp["source_checksum"] if isinstance(bp, Mapping) else bp.source_checksum,
                "signal_id": bp["signal_id"] if isinstance(bp, Mapping) else bp.signal_id,
                "raw_evidence": bp["raw_evidence"] if isinstance(bp, Mapping) else bp.raw_evidence,
                "signal_json": bp["signal_json"] if isinstance(bp, Mapping) else bp.signal_json,
                "design_notes": bp["design_notes"] if isinstance(bp, Mapping) else bp.design_notes,
            }
            for bp in practices
        ]
        _CACHE[cache_key] = (now + CACHE_TTL_SECONDS, result)
        return result

    @classmethod
    async def record_failure(
        cls,
        session: AsyncSession | None = None,
        *,
        db_manager: Any = None,
        kbd_id: int | None,
        stage: str,
        raw_content: str,
        reason: str,
        detail_payload: dict[str, Any] | None = None,
        persist: bool = True,
    ) -> int:
        """记录抽取链路异常到 signal_failure_extraction 表，不抛出异常阻塞主流程"""
        if not persist:
            logger.info(
                "dry-run 跳过异常复盘写入 stage=%s reason=%s kbd_id=%s",
                stage,
                reason,
                kbd_id,
            )
            return -1
        trace_id = get_current_trace_id() or f"signal-failure:{kbd_id or 'unknown'}:{stage}"
        record_data = {
            "kbd_id": kbd_id,
            "stage": stage,
            "raw_content": raw_content[:4000] if raw_content else "",
            "reason": reason,
            "detail_payload": detail_payload or {},
            "trace_id": trace_id,
        }
        # 1. 优先使用 db_manager 开辟独立隔离事务提交，避免随业务 session 回滚
        if db_manager is not None:
            try:
                async with db_manager.async_session_factory() as independent_session:
                    record = SignalFailureExtraction(**record_data)
                    independent_session.add(record)
                    await independent_session.commit()
                    logger.info(
                        "独立事务沉淀抽取异常记录 id=%d stage=%s reason=%s kbd_id=%s", record.id, stage, reason, kbd_id
                    )
                    return record.id
            except Exception as exc:
                logger.warning("独立事务记录抽取异常失败: %s", exc)

        # 2. 备用方式：使用传入 session 并通过 savepoint 隔离
        if session is not None:
            try:
                async with session.begin_nested():
                    record = SignalFailureExtraction(**record_data)
                    session.add(record)
                    await session.flush()
                    logger.info(
                        "已在当前 session 嵌套保存点沉淀抽取异常记录 id=%d stage=%s reason=%s", record.id, stage, reason
                    )
                    return record.id
            except Exception as exc:
                logger.warning("当前 session 记录抽取异常失败: %s", exc)

        return -1
