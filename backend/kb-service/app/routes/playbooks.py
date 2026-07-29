"""按 S0 已确认分类返回完整的已发布知识清单。"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from fastapi import APIRouter, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.dynamic_resource.adapters import kbd_resource_payload, sop_resource_payload
from shared.dynamic_resource.loader import snapshot_revision_metadata
from shared.dynamic_resource.publisher import DynamicResourcePublisher
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from shared.schemas.signal_generation import current_tool_contract_revision
from shared.schemas.signal_schema import validate_publishable_signals_json
from sqlalchemy import select

from app.models.kbd_entry import KbdEntry
from app.models.sop_document import SopDocument

logger = get_logger("kb-service-playbooks")
router = APIRouter(prefix="/api/kb/v2", tags=["playbooks"])

_db_manager: DatabaseManager | None = None


def set_dependencies(db: DatabaseManager) -> None:
    global _db_manager
    _db_manager = db


def _signals(entry: KbdEntry) -> list[dict[str, Any]]:
    raw = entry.signals_json or []
    if isinstance(raw, dict):
        raw = raw.get("signals", [])
    return [signal for signal in raw if isinstance(signal, dict)] if isinstance(raw, list) else []


def _execution_issues(
    signals: list[dict[str, Any]],
    document: dict[str, Any] | None = None,
) -> list[str]:
    """返回自动执行硬门禁问题；知识条目本身不会因此从清单消失。"""
    issues: list[str] = []
    schema_issue: str | None = None
    validation_document = document or {"schema_version": 2, "signals": signals}
    try:
        validate_publishable_signals_json(validation_document)
    except Exception as exc:
        # jsonschema.ValidationError 的 str(exc) 会附带整段 Schema 与实例，既不利于
        # API 消费，也可能产生数 KB 的重复错误；message 才是可操作的门禁原因。
        schema_issue = str(getattr(exc, "message", exc))
    seen_ids: set[str] = set()
    for index, signal in enumerate(signals, start=1):
        signal_id = str(signal.get("id") or signal.get("signal_id") or "").strip()
        if not signal_id:
            issues.append(f"signal[{index}] 缺少 signal_id")
        elif signal_id in seen_ids:
            issues.append(f"signal_id 重复: {signal_id}")
        else:
            seen_ids.add(signal_id)
        acquire = signal.get("acquire") or {}
        if not acquire.get("tool"):
            issues.append(f"{signal_id or f'signal[{index}]'} 缺少 acquire.tool")
        tool = str(acquire.get("tool") or "")
        matcher = signal.get("match")
        produces = ((signal.get("orchestrate") or {}).get("produces") or [])
        has_match = isinstance(matcher, dict)
        has_produces = any(
            isinstance(item, dict) and isinstance(item.get("name"), str) and item["name"].strip()
            for item in produces
        )
        if tool.startswith("qkv_") and (has_match or not has_produces):
            issues.append(
                f"{signal_id or f'signal[{index}]'} 的 QKV 必须配置有效产出变量且 match 为 null"
            )
        if tool.startswith("qfk_") and has_match == has_produces:
            # QFK v2 有两种互斥且都可自动执行的模式：
            # 1. match：对命令结果做确定性判定；
            # 2. orchestrate.produces：命令成功并完成受控提取后写入变量池。
            # 不能再把 match=None 一律判成不可执行，否则合法的产出变量链会在
            # 分类快照阶段被提前过滤，永远无法进入实际执行器。
            issues.append(
                f"{signal_id or f'signal[{index}]'} 必须且只能配置确定性 matcher 或有效产出变量"
            )
    # match/produces 互斥已经由上面的带 signal_id 诊断表达，不重复返回 Schema
    # 的 signals[index] 版本；其他 Schema 问题（字段缺失、类型错误、非法枚举等）
    # 仍作为第一条硬门禁问题返回。
    if schema_issue and not (
        "必须且只能配置“关键字判定(match)”或“产出变量(orchestrate.produces)”之一" in schema_issue
        or "是产出变量信号，必须配置 orchestrate.produces 且 match 必须为 null" in schema_issue
    ):
        issues.insert(0, f"signals_json 契约校验失败: {schema_issue}")
    generation = validation_document.get("generation_metadata") or {}
    if isinstance(generation, dict) and generation:
        if generation.get("status") == "stale":
            issues.append("Signal/Contract 生成输入已变化，必须重新抽取或完成人工复核")
        if generation.get("tool_contract_revision") != current_tool_contract_revision():
            issues.append("Signal/Contract 使用的工具契约版本已过期，必须重新编译")
    if not signals:
        issues.append("未配置关键信号")
    return issues


@router.get("/categories/{category_id}/playbooks")
async def get_category_playbooks(
    category_id: str,
    taxonomy_version: str | None = Query(None, max_length=64),
) -> dict[str, Any]:
    """返回分类内全部已发布 SOP/KBD，不接受 query、top_k 或检索门禁。"""
    if _db_manager is None:
        raise HTTPException(status_code=503, detail="服务依赖未初始化")

    async with _db_manager.async_session_factory() as session:
        sop_rows = await session.execute(
            select(SopDocument)
            .where(SopDocument.category_id == category_id, SopDocument.status == "published")
            .order_by(SopDocument.id)
        )
        kbd_rows = await session.execute(
            select(KbdEntry)
            .where(KbdEntry.category_id == category_id, KbdEntry.status == "published")
            .order_by(KbdEntry.id)
        )
        sops = list(sop_rows.scalars().all())
        kbds = list(kbd_rows.scalars().all())

        trace_id = get_current_trace_id()
        serialized_sops: list[dict[str, Any]] = []
        serialized_kbds: list[dict[str, Any]] = []
        revision_keys: list[str] = []

        for sop in sops:
            snapshot = await DynamicResourcePublisher(session).ensure_published(
                **sop_resource_payload(sop), trace_id=trace_id
            )
            revision = snapshot_revision_metadata(snapshot)
            revision_keys.append(f"sop:{sop.id}:{revision.get('revision', 0)}")
            serialized_sops.append(
                {
                    "id": sop.id,
                    "title": sop.title,
                    "category_id": sop.category_id,
                    "content_md": sop.content_md,
                    "tree_json": sop.tree_json,
                    "variable_schema": sop.variable_schema or [],
                    "status": sop.status,
                    "resource_revision": revision,
                }
            )

        for kbd in kbds:
            signals = _signals(kbd)
            raw_signal_doc = kbd.signals_json if isinstance(kbd.signals_json, dict) else {}
            verification_contract = raw_signal_doc.get("verification_contract") or {}
            generation_metadata = raw_signal_doc.get("generation_metadata") or {}
            issues = _execution_issues(signals, raw_signal_doc)
            snapshot = await DynamicResourcePublisher(session).ensure_published(
                **kbd_resource_payload(kbd), trace_id=trace_id
            )
            revision = snapshot_revision_metadata(snapshot)
            revision_keys.append(f"kbd:{kbd.id}:{revision.get('revision', 0)}")
            serialized_kbds.append(
                {
                    "id": str(kbd.id),
                    "kbd_id": kbd.id,
                    "support_id": kbd.support_id,
                    "name": kbd.title,
                    "title": kbd.title,
                    "category_id": kbd.category_id,
                    "status": kbd.status,
                    "executable": not issues,
                    "execution_issues": issues,
                    "signals": signals,
                    "verification_contract": verification_contract,
                    "generation_metadata": generation_metadata,
                    "root_cause": kbd.root_cause,
                    "solution": kbd.solution,
                    "problem_description": kbd.problem_description,
                    "resource_revision": revision,
                }
            )

        await session.commit()

    snapshot_material = json.dumps(revision_keys, ensure_ascii=False, separators=(",", ":"))
    snapshot_id = "kbsnap-" + hashlib.sha256(snapshot_material.encode()).hexdigest()[:16]
    logger.info(
        event="category_playbooks_loaded",
        category_id=category_id,
        snapshot_id=snapshot_id,
        sop_count=len(serialized_sops),
        kbd_count=len(serialized_kbds),
        trace_id=trace_id,
    )
    return {
        "category": {"id": category_id, "taxonomy_version": taxonomy_version},
        "snapshot_id": snapshot_id,
        "sops": serialized_sops,
        "kbds": serialized_kbds,
    }
