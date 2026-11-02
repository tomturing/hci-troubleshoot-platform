"""工单详情页的「证据聚合」管理端点。

为什么单独开一条路由，而不是把这些字段塞进 ``GET /api/cases/admin/{case_id}``：
工单表只回答「这单是什么」，回答不了「这单采了什么、AI 怎么诊断的」。两类数据来自
完全不同的表（log_collect_task / bridge_execution_logs / dsh_diagnosis_session），
塞进工单响应会让详情接口变成一个随业务膨胀的聚合器，也会把面向客户的 CaseResponse
污染成内部审计结构。
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from sqlalchemy import text

from ..config import settings
from ..repositories.log_collect_repo import LogCollectTaskRepository
from ..security.auth import require_admin_token
from ..services.dsh_diagnosis_service import DshDiagnosisService, IngestStats
from ..services.log_collect_service import LogCollectService

logger = get_logger("case-insights")

router = APIRouter(
    prefix="/api/cases/admin",
    tags=["case-insights"],
    dependencies=[Depends(require_admin_token)],
)

database_manager: DatabaseManager | None = None


def set_database_manager(db_manager: DatabaseManager) -> None:
    """设置数据库管理器（由 main.py 调用）。"""
    global database_manager
    database_manager = db_manager


async def get_session(request: Request):
    # 优先取 app.state（lifespan 已写入，是唯一真源），模块级变量只作兼容兜底：
    # 只依赖模块级 set_*() 的话，一旦 lifespan 少调一行，端点会恒返 500
    # 「Database not initialized」，而启动/健康检查全绿——正是 2026-11-02 staging 的现场。
    manager = getattr(request.app.state, "database_manager", None) or database_manager
    if manager is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in manager.get_session():
        yield session


# delivery / upload_mode 只在界面上有意义，映射放在服务端，避免前端到处硬编码字符串
_UPLOAD_MODE_LABELS = {
    "acli_direct": "P1 · 节点直传日志服务器",
    "bridge_relay": "P2 · 经平台中转",
    "none": "未交付",
}

_DELIVERY_STATUS_LABELS = {
    "queued": "排队中",
    "pulling": "拉取中",
    "uploading": "上传中",
    "delivered": "已交付",
    "failed": "交付失败",
    "cancelled": "已取消",
    "none": "未开始",
}

_TASK_STATUS_LABELS = {
    "pending": "待开始",
    "probing": "正在探测",
    "installing": "正在安装 acli",
    "collecting": "正在采集",
    "packaging": "正在打包",
    "uploading": "正在上传",
    "completed": "已完成",
    "failed": "失败",
    "cancelled": "已取消",
}

_CASE_SQL = text(
    """
    SELECT case_id, title, description, status, trace_id, metadata, created_at
    FROM "case"
    WHERE case_id = :case_id
    """
)

_SCENARIO_SQL = text(
    """
    SELECT value, label, summary, collects, typical_symptoms, duration_hint, enabled
    FROM log_collect_scenario
    WHERE value = ANY(:values)
    """
)

_PROBE_SQL = text(
    """
    SELECT created_at, event, level, extra
    FROM bridge_execution_logs
    WHERE case_id = :case_id
      AND event IN ('log_collect.probe', 'log_collect.create')
    ORDER BY created_at DESC
    LIMIT :limit
    """
)


def _iso(value: Any) -> Any:
    return value.isoformat() if hasattr(value, "isoformat") else value


def _recommended_path(probe: dict[str, Any] | None) -> dict[str, Any]:
    """把 probe 结论翻译成「这次为什么该走哪条路」。

    第一性原理：建议路径不是一个新字段，而是 probe 的事实推出来的结论。
    单独存一份「建议」只会与事实漂移，所以每次都用事实重算。
    """
    if not probe:
        return {"value": "unknown", "label": "未知（无探测记录）", "reason": "没有下发前探测快照"}
    reachable = probe.get("log_server_reachable")
    if reachable is True:
        return {
            "value": "acli_direct",
            "label": "P1 · 节点直传",
            "reason": f"日志服务器可达（HTTP {probe.get('http_code') or '-'}）",
        }
    if reachable is False:
        return {
            "value": "bridge_relay",
            "label": "P2 · 经平台中转",
            "reason": f"日志服务器不可达（{probe.get('note') or probe.get('catalog_note') or '探测失败'}）",
        }
    return {"value": "unknown", "label": "未知（探测字段缺失）", "reason": "probe 快照里没有 log_server_reachable"}


def _task_payload(task: Any, scenarios: dict[str, dict[str, Any]]) -> dict[str, Any]:
    detail = dict(task.detail or {})
    probe = detail.get("probe") if isinstance(detail.get("probe"), dict) else None
    modules = list(task.modules or [])
    labels = []
    for value in modules:
        item = scenarios.get(value)
        labels.append(
            {
                "value": value,
                # 字典缺失（历史任务早于字典上线）时退回枚举值，不显示空白
                "label": (item or {}).get("label") or value,
                "summary": (item or {}).get("summary"),
                "duration_hint": (item or {}).get("duration_hint"),
            }
        )
    day_span = None
    if task.date_from and task.date_to:
        day_span = (task.date_to - task.date_from).days + 1
    return {
        "task_id": str(task.task_id),
        "status": task.status,
        "status_label": _TASK_STATUS_LABELS.get(task.status, task.status),
        "progress": task.progress,
        "upload_mode": task.upload_mode,
        "upload_mode_label": _UPLOAD_MODE_LABELS.get(task.upload_mode or "none"),
        "delivery_status": task.delivery_status,
        "delivery_status_label": _DELIVERY_STATUS_LABELS.get(task.delivery_status or "none"),
        "integrity_state": task.integrity_state,
        "created_at": task.created_at,
        "updated_at": task.updated_at,
        "finished_at": task.finished_at,
        "created_by": task.created_by,
        "trace_id": task.trace_id,
        # ── 用户选项 ──
        "origin_node_ip": str(task.origin_node_ip) if task.origin_node_ip else None,
        "modules": labels,
        "date_from": task.date_from,
        "date_to": task.date_to,
        "day_span": day_span,
        "ips": list(task.ips or []),
        "extra_paths": list(task.extra_paths or []),
        "remote_url": task.remote_url,
        # ── 环境元数据 ──
        "arch": task.arch,
        "acli_version": task.acli_version,
        "probe": probe,
        "recommended_path": _recommended_path(probe),
        "baseline_report_hash": detail.get("baseline_report_hash"),
        "delivery_evidence": detail.get("delivery_evidence"),
        "delivery_evidence_note": detail.get("delivery_evidence_note"),
        # ── 产物与交付 ──
        "archive": {
            "path": task.archive_path,
            "size": task.archive_size,
            "sha256": task.archive_sha256,
        },
        "object": {
            "key": task.object_key,
            "size": task.object_size,
            "sha256": task.object_sha256,
        },
        "delivery": {
            "attempts": task.delivery_attempts,
            "started_at": task.delivery_started_at,
            "finished_at": task.delivery_finished_at,
            "error": task.delivery_error,
        },
        "relay": {
            "status": task.relay_status,
            "chunk_index": task.relay_chunk_index,
            "chunk_total": task.relay_chunk_total,
            "chunk_size": task.relay_chunk_size,
            "restarts": task.relay_restarts,
            "attempt": task.relay_attempt,
            "last_error": task.relay_last_error,
        },
        "cleanup": {"state": task.cleanup_state, "reason": task.cleanup_reason},
        "error": {"code": task.error_code, "message": task.error_message},
    }


@router.get("/{case_id}/log-collect")
async def get_case_log_collect(
    case_id: str,
    limit: int = Query(20, ge=1, le=100, description="最多返回多少个历史任务"),
    session=Depends(get_session),
) -> dict[str, Any]:
    """[Admin] 日志收集证据：用户选项 + 环境元数据 + 交付事实。

    数据源三处，缺一不可：
    - ``log_collect_task``：用户当时选了什么（场景/时间/主机/交付路径）与服务端归一化结果；
    - ``log_collect_scenario``：场景枚举的中文释义（任务表只存枚举值）；
    - ``bridge_execution_logs``：下发前探测的原始事实（建议路径的依据、节点可用磁盘等）。
    """
    case_row = (await session.execute(_CASE_SQL, {"case_id": case_id})).mappings().first()
    if case_row is None:
        raise HTTPException(status_code=404, detail="Case not found")

    service = LogCollectService(LogCollectTaskRepository(session))
    tasks, total = await service.list_tasks(case_id, created_by=None, limit=limit)

    scenario_values = sorted({value for task in tasks for value in (task.modules or [])})
    scenarios: dict[str, dict[str, Any]] = {}
    if scenario_values:
        rows = (await session.execute(_SCENARIO_SQL, {"values": scenario_values})).mappings().all()
        scenarios = {str(row["value"]): dict(row) for row in rows}

    probes = [
        {
            "created_at": row["created_at"],
            "event": row["event"],
            "level": row["level"],
            "extra": row["extra"],
        }
        for row in (await session.execute(_PROBE_SQL, {"case_id": case_id, "limit": 5})).mappings().all()
    ]

    metadata = case_row["metadata"] if isinstance(case_row["metadata"], dict) else {}
    return {
        "case_id": case_row["case_id"],
        "case_title": case_row["title"],
        "case_status": case_row["status"],
        "case_created_at": case_row["created_at"],
        "case_trace_id": case_row["trace_id"],
        # 来源只存在于 JSONB metadata（建单时写入），工单表没有独立列
        "case_source": metadata.get("source"),
        "task_total": total,
        "tasks": [_task_payload(task, scenarios) for task in tasks],
        "probes": probes,
    }


@router.get("/{case_id}/dsh-sessions")
async def list_case_dsh_sessions(
    case_id: str,
    limit: int = Query(20, ge=1, le=50, description="最多返回多少条会话"),
    refresh: bool = Query(True, description="是否先增量扫描会话目录（false 只读已归档记录）"),
    session=Depends(get_session),
) -> dict[str, Any]:
    """[Admin] 该工单的 DSH 诊断会话（完整对话 + 最终结论）。

    为什么在请求里做增量扫描而不是常驻后台任务：会话文件只在人真正打开诊断页时
    才需要被看到，一次扫描的成本（读文件 + 解压）远低于常驻轮询；且扫描幂等，
    多副本并发最多是重复劳动，不会写坏数据。
    """
    services = DshDiagnosisService(
        session,
        root=settings.DSH_SESSIONS_DIR,
        max_files=settings.DSH_SESSIONS_MAX_FILES,
        max_bytes=settings.DSH_SESSIONS_MAX_BYTES,
    )
    stats = IngestStats()
    if refresh and settings.DSH_SESSIONS_INGEST_ENABLED:
        stats = await services.ingest()
        if stats.parsed or stats.failed:
            logger.info(
                event="dsh_sessions_ingested",
                message="DSH 会话增量入库",
                case_id=case_id,
                **stats.as_dict(),
            )
    items = await services.list_by_case(case_id, limit=limit)
    for item in items:
        item["started_at"] = _iso(item.get("started_at"))
        item["last_activity_at"] = _iso(item.get("last_activity_at"))
        item["ingested_at"] = _iso(item.get("ingested_at"))
        item["source_mtime"] = _iso(item.get("source_mtime"))
    return {
        "case_id": case_id,
        "total": len(items),
        "ingest": stats.as_dict(),
        "items": items,
    }
