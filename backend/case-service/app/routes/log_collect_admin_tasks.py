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

import base64
import binascii
import gzip
import json
import time
from collections.abc import Mapping
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
from ..domain.log_collect_display import (
    build_network_view,
    build_transfer_view,
    delivery_evidence_ok,
    derive,
)
from ..domain.log_collect_display_generated import (
    ARTIFACT_QUALITY_LABELS,
    ARTIFACT_REASON_LABELS,
    format_size,
)
from ..schemas.log_collect_admin import (
    AdminLogCollectSummary,
    AdminLogCollectTaskDetail,
    AdminLogCollectTaskItem,
    AdminLogCollectTaskListResponse,
    AdminPathSummary,
    ArtifactManifestItem,
    ArtifactManifestResponse,
    DestinationProgressResponse,
    NetworkProbeResponse,
    ProcessLogContentResponse,
    ProcessLogItem,
    ProcessLogListResponse,
    TransferMetricsResponse,
)
from ..schemas.log_collect_display import (
    LogCollectNetworkInfo,
    LogCollectTransferInfo,
    build_display_payload,
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

# 只取判定真正需要的 detail 键：整包 detail 会随探测/审计不断变大，
# 列表按页取全量 detail 是纯粹的 IO 浪费（且让慢查询随时间恶化）。
_EVIDENCE_DETAIL = """jsonb_strip_nulls(jsonb_build_object(
               'artifact_quality', t.detail -> 'artifact_quality',
               'artifact_reason', t.detail -> 'artifact_reason',
               'p1_archive_check', t.detail -> 'p1_archive_check',
               'delivery_evidence', t.detail -> 'delivery_evidence',
               'integrity_mismatch', t.detail -> 'integrity_mismatch',
               'bridge_outdated', t.detail -> 'bridge_outdated'
           ))"""

_SELECT = f"""
    SELECT t.task_id, t.case_id, c.client_id,
           COALESCE(cu.short_name, cu.name) AS customer_name,
           CAST(t.origin_node_ip AS text) AS origin_node_ip,
           t.upload_mode, t.status, t.progress, t.archive_size, t.object_size,
           t.delivery_status, t.relay_status, t.relay_upload_id,
           t.integrity_state, t.detail -> 'p1_archive_check' AS archive_check,
           t.archive_path, t.archive_sha256, t.acli_version, t.arch,
           {_EVIDENCE_DETAIL} AS detail,
           t.error_code, t.error_message, t.trace_id,
           t.created_at, t.finished_at, t.delivery_finished_at
"""

# 汇总不再用 SQL 条件各算一遍：SQL 里写五处判定，界面里再写一处，出现分歧时
# 168 vs 146 这种对不上账就必然发生（2026-11-12 事故）。改为取同一批瘦行、
# 复用派生层 conclusion 分桶，恒等式 total == Σby_conclusion 由模型校验器强制。
_SUMMARY_SELECT = f"""
    SELECT t.upload_mode, t.status, t.progress, t.archive_size, t.object_size,
           t.delivery_status, t.relay_status, t.archive_path,
           {_EVIDENCE_DETAIL} AS detail
"""

_CONCLUSION_KEYS = ("pending", "success", "partial", "unfinished", "failed", "cancelled")


def _coerce_json(value: Any) -> Any:
    """JSONB 在不同驱动下可能是 dict 也可能是字符串；统一成对象。

    静默拿到字符串会让派生层把证据当空气，界面又回到「已完成 100%」，
    所以这里必须显式解析，解析失败宁可当空证据也不猜。
    """
    if isinstance(value, str | bytes | bytearray):
        try:
            return json.loads(value)
        except (ValueError, TypeError):
            return {}
    return value


def _normalize_row(row: dict[str, Any]) -> dict[str, Any]:
    detail = _coerce_json(row.get("detail"))
    row["detail"] = dict(detail) if isinstance(detail, Mapping) else {}
    check = _coerce_json(row.get("archive_check"))
    row["archive_check"] = dict(check) if isinstance(check, Mapping) else None
    return row


def _empty_counters() -> dict[str, Any]:
    return {
        "total": 0,
        "delivered": 0,
        "unverified": 0,
        "by_conclusion": dict.fromkeys(_CONCLUSION_KEYS, 0),
    }


def _count_row(counters: dict[str, Any], row: Mapping[str, Any]) -> None:
    payload = derive(row)
    conclusion = payload["display"]["conclusion"]
    counters["total"] += 1
    counters["by_conclusion"][conclusion] = counters["by_conclusion"].get(conclusion, 0) + 1
    if str(row.get("delivery_status") or "") == "delivered":
        counters["delivered"] += 1
        if not delivery_evidence_ok(row):
            counters["unverified"] += 1


def _summary_of(rows: list[dict[str, Any]]) -> AdminLogCollectSummary:
    total = _empty_counters()
    groups = {"acli_direct": _empty_counters(), "bridge_relay": _empty_counters()}
    for row in rows:
        normalized = _normalize_row(dict(row))
        for counters in (total, groups.get(str(normalized.get("upload_mode") or ""))):
            if counters is not None:
                _count_row(counters, normalized)

    def path(counters: dict[str, Any]) -> AdminPathSummary:
        return AdminPathSummary(
            total=counters["total"],
            # 「传输中」= 结论仍是 pending（采集或交付在途），二者必须是同一个数。
            transferring=counters["by_conclusion"]["pending"],
            delivered=counters["delivered"],
            unverified=counters["unverified"],
            failed=counters["by_conclusion"]["failed"],
            by_conclusion=counters["by_conclusion"],
        )

    return AdminLogCollectSummary(
        total=total["total"],
        transferring=total["by_conclusion"]["pending"],
        delivered=total["delivered"],
        unverified=total["unverified"],
        failed=total["by_conclusion"]["failed"],
        by_conclusion=total["by_conclusion"],
        p1=path(groups["acli_direct"]),
        p2=path(groups["bridge_relay"]),
    )


def _in_clause(ids: list[str], prefix: str = "id") -> tuple[str, dict[str, Any]]:
    """生成 IN 占位符：值仍走 bind params，只有占位符个数来自列表长度。"""
    params = {f"{prefix}_{index}": value for index, value in enumerate(ids)}
    return ", ".join(f":{key}" for key in params), params


async def _page_transfer(task_ids: list[str]) -> dict[str, dict[str, Any]]:
    """一页任务的最新传输样本：一次查询，避免 N+1。"""
    if not task_ids:
        return {}
    placeholders, params = _in_clause(task_ids)
    rows = await _fetch(
        f"""
        SELECT DISTINCT ON (task_id) task_id, stage, bytes_done, bytes_total, percent,
               elapsed_ms, rate_bps, eta_sec, stalled, source, created_at
        FROM log_collect_transfer_metric
        WHERE task_id IN ({placeholders})
        ORDER BY task_id, seq DESC
        """,
        params,
    )
    return {str(row["task_id"]): dict(row) for row in rows}


async def _page_probes(task_ids: list[str]) -> dict[str, list[dict[str, Any]]]:
    """一页任务的每目标最新探测：一次查询，避免 N+1。"""
    if not task_ids:
        return {}
    placeholders, params = _in_clause(task_ids)
    rows = await _fetch(
        f"""
        SELECT DISTINCT ON (task_id, target) task_id, target, reachable, grade,
               rtt_avg_ms, jitter_ms, loss_rate, http_code, up_bps, down_bps,
               advice, created_at
        FROM log_collect_network_probe
        WHERE task_id IN ({placeholders})
        ORDER BY task_id, target, created_at DESC
        """,
        params,
    )
    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        grouped.setdefault(str(row["task_id"]), []).append(dict(row))
    return grouped


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
    task_ids = [str(row["task_id"]) for row in rows]
    page_transfer = await _page_transfer(task_ids)
    page_probes = await _page_probes(task_ids)

    items = []
    for row in rows:
        data = _normalize_row(dict(row))
        task_id = str(data["task_id"])
        item = AdminLogCollectTaskItem(**data)
        payload = build_display_payload(
            data,
            metrics=page_transfer.get(task_id),
            probes=page_probes.get(task_id),
        )
        item.display = payload.display
        item.artifact = payload.artifact
        item.transfer = payload.transfer
        item.network = payload.network
        items.append(item)

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
        summary=_summary_of(summary_rows),
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


# ── 证据端点：详情抽屉四个页签的数据来源 ─────────────────────────────────
# 全部只读、按 task_id 过滤、显式 limit：既避免整包 detail 进内存，
# 也避免一个人在抽屉里点开就把日志服务器或数据库拖满。

_PROCESS_LOG_CONTENT_MAX_BYTES = 2 * 1024 * 1024
_MANIFEST_ENTRIES_MAX = 200


async def _task_row(task_id: UUID) -> dict[str, Any]:
    rows = await _fetch(f"{_SELECT} {_JOIN} WHERE t.task_id = :task_id", {"task_id": str(task_id)})
    if not rows:
        raise HTTPException(status_code=404, detail="任务不存在")
    return _normalize_row(dict(rows[0]))


async def _latest_transfer(task_id: UUID) -> dict[str, Any] | None:
    rows = await _fetch(
        """
        SELECT stage, bytes_done, bytes_total, percent, elapsed_ms, rate_bps, eta_sec,
               stalled, source, created_at
        FROM log_collect_transfer_metric
        WHERE task_id = :task_id
        ORDER BY seq DESC
        LIMIT 1
        """,
        {"task_id": str(task_id)},
    )
    return build_transfer_view(rows[0]) if rows else None


async def _latest_probes(task_id: UUID) -> list[dict[str, Any]]:
    return await _fetch(
        """
        SELECT DISTINCT ON (target)
               target, reachable, grade, rtt_avg_ms, jitter_ms, loss_rate, http_code,
               up_bps, down_bps, advice, created_at
        FROM log_collect_network_probe
        WHERE task_id = :task_id
        ORDER BY target, created_at DESC
        """,
        {"task_id": str(task_id)},
    )


@admin_router.get("/{task_id}", response_model=AdminLogCollectTaskDetail)
async def admin_task_detail(task_id: UUID) -> AdminLogCollectTaskDetail:
    """单任务详情：列表字段 + 最新传输样本 + 网络探测结论。"""
    row = await _task_row(task_id)
    metrics = await _latest_transfer(task_id)
    probes = await _latest_probes(task_id)
    payload = build_display_payload(row, metrics=metrics, probes=probes)

    item = AdminLogCollectTaskDetail(**row)
    # 结构化原文按「详情专用白名单」单独取：列表 SQL 只取判定所需 6 键，
    # 逐台明细/归因来源/原始输出只对单任务展开，避免按页拖大 detail。
    extra_rows = await _fetch(
        """SELECT jsonb_strip_nulls(jsonb_build_object(
                   'spc_hosts', t.detail -> 'spc_hosts',
                   'error_code_derivation', t.detail -> 'error_code_derivation',
                   'create_output', t.detail -> 'create_output',
                   'write_audit', t.detail -> 'write_audit'
               )) AS extra
           FROM log_collect_task t WHERE t.task_id = :task_id""",
        {"task_id": str(task_id)},
    )
    extra = _coerce_json(extra_rows[0]["extra"]) if extra_rows else {}
    item.detail = {
        **dict(row.get("detail") or {}),
        **(extra if isinstance(extra, Mapping) else {}),
    }
    item.display = payload.display
    item.artifact = payload.artifact
    item.transfer = payload.transfer
    item.network = payload.network
    return item


@admin_router.get("/{task_id}/transfer-metrics", response_model=TransferMetricsResponse)
async def transfer_metrics(
    task_id: UUID,
    limit: int = Query(120, ge=1, le=1000, description="最多返回的样本数（最新在前）"),
) -> TransferMetricsResponse:
    """传输时间序列。样本来自 bridge 周期上报，缺失即「暂无数据」，不补零。"""
    await _task_row(task_id)
    rows = await _fetch(
        """
        SELECT stage, bytes_done, bytes_total, percent, elapsed_ms, rate_bps, eta_sec,
               stalled, source, created_at
        FROM log_collect_transfer_metric
        WHERE task_id = :task_id
        ORDER BY seq DESC
        LIMIT :limit
        """,
        {"task_id": str(task_id), "limit": limit},
    )
    items = [
        LogCollectTransferInfo.model_validate(view)
        for view in (build_transfer_view(row) for row in rows)
        if view is not None
    ]
    return TransferMetricsResponse(task_id=task_id, items=items, latest=items[0] if items else None)


@admin_router.get("/{task_id}/network-probe", response_model=NetworkProbeResponse)
async def network_probe(task_id: UUID) -> NetworkProbeResponse:
    """下发前网络探测：每目标最新一条，按当前交付路径定级。"""
    row = await _task_row(task_id)
    probes = await _latest_probes(task_id)
    view = build_network_view(probes, row.get("upload_mode"))
    return NetworkProbeResponse(
        task_id=task_id,
        network=LogCollectNetworkInfo.model_validate(view) if view is not None else None,
    )


@admin_router.get("/{task_id}/process-logs", response_model=ProcessLogListResponse)
async def process_logs(task_id: UUID) -> ProcessLogListResponse:
    """节点侧采集进程日志清单（不含正文，正文单独取）。"""
    await _task_row(task_id)
    rows = await _fetch(
        """
        SELECT log_id, host, path, size, sha256, truncated, status, error, collected_at
        FROM log_collect_process_log
        WHERE task_id = :task_id
        ORDER BY collected_at DESC NULLS LAST
        """,
        {"task_id": str(task_id)},
    )
    items = [
        ProcessLogItem(
            log_id=row["log_id"],
            host=row["host"],
            path=row["path"],
            size_bytes=row["size"],
            size_text=format_size(row["size"]),
            sha256=row["sha256"],
            truncated=bool(row["truncated"]),
            status=row["status"],
            error=row["error"],
            collected_at=row["collected_at"],
        )
        for row in rows
    ]
    return ProcessLogListResponse(task_id=task_id, items=items)


@admin_router.get("/{task_id}/process-logs/{log_id}/content", response_model=ProcessLogContentResponse)
async def process_log_content(task_id: UUID, log_id: UUID) -> ProcessLogContentResponse:
    """进程日志正文。

    安全边界：内容只可能来自 bridge 上报时落库的 gzip 字节，不接受任何路径参数；
    仍然复核 path 是绝对路径，防止把库里被污染的相对路径当成可展示对象。
    """
    rows = await _fetch(
        """
        SELECT log_id, host, path, size, content_gz, truncated, status, collected_at
        FROM log_collect_process_log
        WHERE task_id = :task_id AND log_id = :log_id
        """,
        {"task_id": str(task_id), "log_id": str(log_id)},
    )
    if not rows:
        raise HTTPException(status_code=404, detail="进程日志不存在")
    row = dict(rows[0])

    path = str(row.get("path") or "")
    if not path.startswith("/") or ".." in path:
        raise HTTPException(status_code=409, detail="进程日志路径异常，拒绝展示")

    raw = row.get("content_gz")
    truncated = bool(row.get("truncated"))
    if raw is None:
        content = ""
    else:
        payload = raw
        if isinstance(raw, str):
            # 兼容早期把 base64 文本直接落库的形态。
            try:
                payload = base64.b64decode(raw, validate=True)
            except (binascii.Error, ValueError) as exc:
                raise HTTPException(status_code=500, detail="进程日志内容损坏") from exc
        if isinstance(payload, memoryview):
            payload = bytes(payload)
        if not isinstance(payload, bytes | bytearray):
            raise HTTPException(status_code=500, detail="进程日志内容格式异常")
        try:
            decoded = gzip.decompress(bytes(payload))
        except (OSError, EOFError, binascii.Error) as exc:
            logger.warning(
                event="admin_log_collect_process_log_decode_failed",
                message="进程日志解压失败",
                task_id=str(task_id),
                log_id=str(log_id),
                error=str(exc),
                trace_id=get_current_trace_id(),
            )
            raise HTTPException(status_code=500, detail="进程日志内容损坏") from exc
        if len(decoded) > _PROCESS_LOG_CONTENT_MAX_BYTES:
            decoded = decoded[:_PROCESS_LOG_CONTENT_MAX_BYTES]
            truncated = True
        content = decoded.decode("utf-8", errors="replace")

    logger.info(
        event="admin_log_collect_process_log_read",
        message="管理端读取节点进程日志",
        task_id=str(task_id),
        log_id=str(log_id),
        bytes=len(content.encode("utf-8")),
        truncated=truncated,
        trace_id=get_current_trace_id(),
    )
    return ProcessLogContentResponse(
        log_id=row["log_id"],
        host=row["host"],
        path=path,
        content=content,
        truncated=truncated,
        size_bytes=row["size"],
        collected_at=row["collected_at"],
    )


@admin_router.get("/{task_id}/artifact-manifest", response_model=ArtifactManifestResponse)
async def artifact_manifest(task_id: UUID) -> ArtifactManifestResponse:
    """包内容探测清单：识别空壳包的唯一强证据（体积只能判「过小」）。"""
    await _task_row(task_id)
    rows = await _fetch(
        """
        SELECT source, archive_size, archive_sha256, entry_count, entry_total_bytes,
               has_encrypted, quality, reason, entries, inspected_at
        FROM log_collect_artifact_manifest
        WHERE task_id = :task_id
        ORDER BY inspected_at DESC
        """,
        {"task_id": str(task_id)},
    )
    items: list[ArtifactManifestItem] = []
    for row in rows:
        entries = row.get("entries") if isinstance(row.get("entries"), list) else []
        quality = str(row.get("quality") or "unknown")
        reason = row.get("reason")
        items.append(
            ArtifactManifestItem(
                source=row["source"],
                archive_size=row.get("archive_size"),
                archive_size_text=format_size(row.get("archive_size")),
                archive_sha256=row.get("archive_sha256"),
                entry_count=row.get("entry_count"),
                entry_total_bytes=row.get("entry_total_bytes"),
                entry_total_text=format_size(row.get("entry_total_bytes")),
                has_encrypted=row.get("has_encrypted"),
                quality=quality,
                quality_label=ARTIFACT_QUALITY_LABELS.get(quality, ARTIFACT_QUALITY_LABELS["unknown"]),
                reason=reason,
                reason_text=ARTIFACT_REASON_LABELS.get(str(reason or ""), None),
                entries=entries[:_MANIFEST_ENTRIES_MAX] or None,
                entries_truncated=len(entries) > _MANIFEST_ENTRIES_MAX,
                inspected_at=row.get("inspected_at"),
            )
        )
    return ArtifactManifestResponse(task_id=task_id, items=items)
