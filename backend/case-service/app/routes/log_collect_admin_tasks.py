"""日志收集「日志上传」管理端只读端点。

三条不可妥协的边界：

1. **只读**：本文件只有 GET，没有任何写库路径。客户面板与客户身份链路完全不经过这里，
   内部令牌由 api-gateway 的 ``require_admin`` 重签后注入（``require_admin_token``）。
2. **不新增客户名列**：客户名从 ``case.customer_id → customer.short_name/name`` 取，
   未关联客户时为 NULL；``client_id`` 单独返回。将来外部系统按 ``customer.code``
   导入客户档案并回填 ``case.customer_id`` 即可，无需 DDL，也不会出现两处存名字。
3. **查不到 ≠ 失败**：日志服务器进度是外部事实，只有 ``status=done + target_path``
   算强证据；404/超时/会话过期一律降级为 ``no_record`` / ``unsupported`` /
   ``unavailable``，绝不写入任何任务状态。列表接口**完全不查 5888**，避免 N 个任务
   乘以轮询打爆单进程日志服务器。
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query
from shared.database.postgres import DatabaseManager
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import text

from ..config import settings
from ..schemas.log_collect_admin import (
    AdminLogCollectSummary,
    AdminLogCollectTaskItem,
    AdminLogCollectTaskListResponse,
    AdminPathSummary,
    DestinationProgressResponse,
)
from ..security.auth import require_admin_token
from ..services.log_collect_relay_service import SangforRelayClient

logger = get_logger("log-collect-admin-tasks")

admin_router = APIRouter(
    prefix="/internal/log-collect/admin/tasks",
    tags=["log-collect-admin-tasks"],
    dependencies=[Depends(require_admin_token)],
)

database_manager: DatabaseManager | None = None
PROGRESS_TIMEOUT_SECONDS = 3.0
PROGRESS_CACHE_TTL_SECONDS = 10
PROGRESS_CACHE_MAX = 512
_progress_cache: dict[str, tuple[datetime, DestinationProgressResponse]] = {}

VALID_STATUSES = {
    "pending",
    "probing",
    "installing",
    "collecting",
    "packaging",
    "uploading",
    "completed",
    "failed",
    "cancelled",
}
VALID_DELIVERY = {"none", "queued", "pulling", "uploading", "relaying", "delivered", "failed"}
VALID_MODES = {"acli_direct", "bridge_relay", "none"}


def set_database_manager(db_manager: DatabaseManager) -> None:
    global database_manager
    database_manager = db_manager


async def _fetch(sql: str, params: dict[str, Any]) -> list[dict[str, Any]]:
    if database_manager is None:
        raise HTTPException(status_code=500, detail="Database not initialized")
    async for session in database_manager.get_session():
        result = await session.execute(text(sql), params)
        return [dict(row) for row in result.mappings().all()]
    return []


def _build_filters(
    *,
    since: datetime,
    until: datetime,
    case_id: str | None,
    node_ip: str | None,
    upload_mode: str | None,
    status: str | None,
    delivery_status: str | None,
) -> tuple[str, dict[str, Any]]:
    """所有用户输入都走 bind params；枚举字段额外白名单，避免脏值静默返回空列表。"""
    if status and status not in VALID_STATUSES:
        raise HTTPException(status_code=400, detail=f"非法采集状态：{status}")
    if delivery_status and delivery_status not in VALID_DELIVERY:
        raise HTTPException(status_code=400, detail=f"非法交付结论：{delivery_status}")
    if upload_mode and upload_mode not in VALID_MODES:
        raise HTTPException(status_code=400, detail=f"非法交付路径：{upload_mode}")

    clauses = ["t.created_at >= :since", "t.created_at <= :until"]
    params: dict[str, Any] = {"since": since, "until": until}
    if case_id:
        clauses.append("t.case_id ILIKE :case_id")
        params["case_id"] = f"%{case_id}%"
    if node_ip:
        clauses.append("CAST(t.origin_node_ip AS text) ILIKE :node_ip")
        params["node_ip"] = f"%{node_ip}%"
    if upload_mode:
        clauses.append("t.upload_mode = :upload_mode")
        params["upload_mode"] = upload_mode
    if status:
        clauses.append("t.status = :status")
        params["status"] = status
    if delivery_status:
        clauses.append("t.delivery_status = :delivery_status")
        params["delivery_status"] = delivery_status
    return " AND ".join(clauses), params


_JOIN = """
    FROM log_collect_task t
    LEFT JOIN "case" c ON c.case_id = t.case_id
    LEFT JOIN customer cu ON cu.customer_id = c.customer_id
"""

_SELECT = """
    SELECT t.task_id, t.case_id, c.client_id,
           COALESCE(cu.short_name, cu.name) AS customer_name,
           CAST(t.origin_node_ip AS text) AS origin_node_ip,
           t.upload_mode, t.status, t.progress, t.archive_size,
           t.delivery_status, t.relay_status, t.relay_upload_id,
           t.integrity_state, t.detail -> 'p1_archive_check' AS archive_check,
           t.error_code, t.error_message, t.trace_id,
           t.created_at, t.finished_at, t.delivery_finished_at
"""

_TRANSFERRING = """(
        t.status IN ('collecting','packaging','uploading')
        OR t.delivery_status IN ('queued','pulling','uploading','relaying')
    )"""
_FAILED = "(t.status = 'failed' OR t.delivery_status = 'failed')"
_UNVERIFIED = "(t.detail -> 'p1_archive_check' ->> 'verified') = 'false'"

_SUMMARY_SELECT = f"""
    SELECT count(*) AS total,
           count(*) FILTER (WHERE {_TRANSFERRING}) AS transferring,
           count(*) FILTER (WHERE t.delivery_status = 'delivered') AS delivered,
           count(*) FILTER (WHERE {_UNVERIFIED}) AS unverified,
           count(*) FILTER (WHERE {_FAILED}) AS failed,
           count(*) FILTER (WHERE t.upload_mode = 'acli_direct') AS p1_total,
           count(*) FILTER (WHERE t.upload_mode = 'acli_direct' AND {_TRANSFERRING}) AS p1_transferring,
           count(*) FILTER (WHERE t.upload_mode = 'acli_direct' AND t.delivery_status = 'delivered') AS p1_delivered,
           count(*) FILTER (WHERE t.upload_mode = 'acli_direct' AND {_UNVERIFIED}) AS p1_unverified,
           count(*) FILTER (WHERE t.upload_mode = 'acli_direct' AND {_FAILED}) AS p1_failed,
           count(*) FILTER (WHERE t.upload_mode = 'bridge_relay') AS p2_total,
           count(*) FILTER (WHERE t.upload_mode = 'bridge_relay' AND {_TRANSFERRING}) AS p2_transferring,
           count(*) FILTER (WHERE t.upload_mode = 'bridge_relay' AND t.delivery_status = 'delivered') AS p2_delivered,
           count(*) FILTER (WHERE t.upload_mode = 'bridge_relay' AND {_UNVERIFIED}) AS p2_unverified,
           count(*) FILTER (WHERE t.upload_mode = 'bridge_relay' AND {_FAILED}) AS p2_failed
"""


def _summary_of(row: dict[str, Any]) -> AdminLogCollectSummary:
    def path(prefix: str) -> AdminPathSummary:
        return AdminPathSummary(
            total=int(row.get(f"{prefix}total", 0)),
            transferring=int(row.get(f"{prefix}transferring", 0)),
            delivered=int(row.get(f"{prefix}delivered", 0)),
            unverified=int(row.get(f"{prefix}unverified", 0)),
            failed=int(row.get(f"{prefix}failed", 0)),
        )

    return AdminLogCollectSummary(
        total=int(row.get("total", 0)),
        transferring=int(row.get("transferring", 0)),
        delivered=int(row.get("delivered", 0)),
        unverified=int(row.get("unverified", 0)),
        failed=int(row.get("failed", 0)),
        p1=path("p1_"),
        p2=path("p2_"),
    )


@admin_router.get("", response_model=AdminLogCollectTaskListResponse)
async def list_admin_tasks(
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    days: int = Query(7, ge=1, le=90, description="时间范围（天），默认近 7 天"),
    case_id: str | None = Query(None, max_length=20),
    node_ip: str | None = Query(None, max_length=64),
    upload_mode: str | None = Query(None, description="acli_direct / bridge_relay / none"),
    status: str | None = None,
    delivery_status: str | None = None,
) -> AdminLogCollectTaskListResponse:
    """跨工单分页列表 + 汇总计数（只读，不查日志服务器）。

    汇总条随筛选联动：P1/P2 分列复用同一套口径，避免前端拿两套数字自己拼。
    """
    started = time.monotonic()
    now = datetime.now(UTC)
    since, until = now - timedelta(days=days), now
    where, params = _build_filters(
        since=since,
        until=until,
        case_id=case_id,
        node_ip=node_ip,
        upload_mode=upload_mode,
        status=status,
        delivery_status=delivery_status,
    )

    rows = await _fetch(
        f"{_SELECT} {_JOIN} WHERE {where} ORDER BY t.created_at DESC LIMIT :limit OFFSET :offset",
        {**params, "limit": page_size, "offset": (page - 1) * page_size},
    )
    count_rows = await _fetch(f"SELECT count(*) AS total {_JOIN} WHERE {where}", params)
    summary_rows = await _fetch(f"{_SUMMARY_SELECT} {_JOIN} WHERE {where}", params)

    items = []
    for row in rows:
        data = dict(row)
        check = data.get("archive_check")
        data["archive_check"] = check if isinstance(check, dict) else None
        items.append(AdminLogCollectTaskItem(**data))

    logger.info(
        event="admin_log_collect_tasks_read",
        message="管理端读取日志上传列表",
        page=page,
        page_size=page_size,
        total=int(count_rows[0]["total"]) if count_rows else 0,
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        trace_id=get_current_trace_id(),
    )
    return AdminLogCollectTaskListResponse(
        items=items,
        total=int(count_rows[0]["total"]) if count_rows else 0,
        page=page,
        page_size=page_size,
        summary=_summary_of(summary_rows[0]) if summary_rows else AdminLogCollectSummary(),
    )


# ── 日志服务器真实进度（平台代理）─────────────────────────────────────────

_TASK_SQL = """
    SELECT t.task_id, t.case_id, t.upload_mode, t.relay_upload_id,
           t.created_at, t.archive_size, t.delivery_status
    FROM log_collect_task t WHERE t.task_id = :task_id
"""


def _cache_get(task_id: UUID) -> DestinationProgressResponse | None:
    hit = _progress_cache.get(str(task_id))
    if not hit:
        return None
    cached_at, payload = hit
    if datetime.now(UTC) - cached_at > timedelta(seconds=PROGRESS_CACHE_TTL_SECONDS):
        _progress_cache.pop(str(task_id), None)
        return None
    return payload


def _cache_put(task_id: UUID, payload: DestinationProgressResponse) -> None:
    now = datetime.now(UTC)
    for key, (cached_at, _) in list(_progress_cache.items()):
        if now - cached_at > timedelta(seconds=PROGRESS_CACHE_TTL_SECONDS):
            _progress_cache.pop(key, None)
    if len(_progress_cache) >= PROGRESS_CACHE_MAX:
        _progress_cache.pop(next(iter(_progress_cache)), None)
    _progress_cache[str(task_id)] = (now, payload)


def _iso(value: Any) -> datetime | None:
    if not value or not isinstance(value, str):
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _map_progress(
    *,
    task_id: UUID,
    path: str,
    data: dict[str, Any] | None,
    state: str,
    reason: str | None = None,
) -> DestinationProgressResponse:
    data = data or {}
    digest = data.get("digest")
    sha = digest.get("sha256") if isinstance(digest, dict) else data.get("sha256")
    status = str(data.get("status") or "")
    target = data.get("target_path")
    if state == "query" and status == "done" and target:
        state = "done"
    elif state == "query":
        state = "uploading" if status == "uploading" else "no_record"

    def as_int(key: str) -> int | None:
        value = data.get(key)
        return int(value) if isinstance(value, (int, float)) else None

    return DestinationProgressResponse(
        task_id=task_id,
        path=path,
        state=state,
        progress=as_int("progress"),
        uploaded_size=as_int("uploaded_size"),
        file_size=as_int("file_size"),
        received_chunks=as_int("received_chunks"),
        chunk_total=as_int("chunk_total"),
        target_path=str(target) if target else None,
        sha256=str(sha) if sha else None,
        finished_at=_iso(data.get("finished_at")),
        checked_at=datetime.now(UTC),
        reason=reason,
    )


def _session_matches_generation(data: dict[str, Any], task: dict[str, Any]) -> bool:
    """会话按代际绑定：同一工单可能有多轮上传，别把上一轮的 100% 当成本轮。"""
    upload_id = str(data.get("upload_id") or "")
    relay_id = str(task.get("relay_upload_id") or "")
    if relay_id and upload_id == relay_id:
        return False  # 平台 P2 代传会话，不是这一轮 P1 的
    started = _iso(data.get("start_time"))
    created = task.get("created_at")
    if started and isinstance(created, datetime):
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if started < created - timedelta(minutes=5) or started > created + timedelta(hours=24):
            return False
    return True


@admin_router.get("/{task_id}/destination-progress", response_model=DestinationProgressResponse)
async def destination_progress(task_id: UUID) -> DestinationProgressResponse:
    """按任务查日志服务器真实进度：P1 按产物名、P2 按 relay_upload_id。

    前端 30s 只对「进行中」的行调这个端点；后端再加 10s TTL 缓存，
    防止多个管理端同时轮询把单进程 5888 打爆。任何异常都降级为「暂无记录」，
    不写库、不改任务状态。
    """
    started = time.monotonic()
    cached = _cache_get(task_id)
    if cached:
        return cached

    rows = await _fetch(_TASK_SQL, {"task_id": str(task_id)})
    if not rows:
        raise HTTPException(status_code=404, detail="日志收集任务不存在")
    task = rows[0]

    mode = str(task.get("upload_mode") or "")
    path = "P1" if mode == "acli_direct" else "P2" if mode == "bridge_relay" else "unknown"
    client = SangforRelayClient(
        settings.LOG_COLLECT_RELAY_URL,
        token=settings.LOG_COLLECT_RELAY_TOKEN,
    )

    payload: DestinationProgressResponse
    if path == "unknown" or (path == "P2" and not task.get("relay_upload_id")):
        payload = _map_progress(
            task_id=task_id,
            path=path,
            data=None,
            state="no_record",
            reason="任务尚未确定交付路径" if path == "unknown" else "P2 代传会话尚未建立",
        )
    else:
        try:
            async with httpx.AsyncClient(timeout=PROGRESS_TIMEOUT_SECONDS) as http:
                if path == "P2":
                    data = await client.upload_status(http, str(task["relay_upload_id"]))
                    payload = _map_progress(
                        task_id=task_id,
                        path=path,
                        data=data,
                        state="query",
                        reason=None if data else "日志服务器会话已结束或已过期（24h）",
                    )
                else:
                    probe_state, data = await client.upload_progress(http, f"{task['case_id']}.zip")
                    if probe_state == "unsupported":
                        # 旧版 5888 没有该端点：平台先上、5888 后上也安全
                        payload = _map_progress(
                            task_id=task_id,
                            path=path,
                            data=None,
                            state="unsupported",
                            reason="该日志服务器不支持进度查询",
                        )
                    elif data is None or data.get("found") is False:
                        payload = _map_progress(
                            task_id=task_id,
                            path=path,
                            data=None,
                            state="no_record",
                            reason="日志服务器上还没有这次上传的记录",
                        )
                    elif not _session_matches_generation(data, task):
                        payload = _map_progress(
                            task_id=task_id,
                            path=path,
                            data=None,
                            state="no_record",
                            reason="本次上传会话与任务代际不匹配",
                        )
                    else:
                        payload = _map_progress(task_id=task_id, path=path, data=data, state="query")
        except (httpx.RequestError, httpx.HTTPStatusError, ValueError) as exc:
            logger.warning(
                event="admin_log_collect_progress_degraded",
                message="日志服务器进度查询降级",
                task_id=str(task_id),
                error=str(exc),
                trace_id=get_current_trace_id(),
            )
            payload = _map_progress(
                task_id=task_id,
                path=path,
                data=None,
                state="unavailable",
                reason="日志服务器暂时不可用",
            )

    _cache_put(task_id, payload)
    logger.info(
        event="admin_log_collect_progress_read",
        message="管理端读取日志服务器进度",
        task_id=str(task_id),
        path=path,
        state=payload.state,
        elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        trace_id=get_current_trace_id(),
    )
    return payload
