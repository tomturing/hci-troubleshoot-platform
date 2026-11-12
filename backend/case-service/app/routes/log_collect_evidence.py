"""日志收集证据数据面：bridge 上报事实，平台推导结论。

不经网关：进程日志 gzip 正文可能接近 2 MiB，网关默认 1 MiB。
鉴权使用短期证据票据（绑定 task_id / probe_id），由 ``create`` 响应与
``network-probe`` 响应分别签发；不复用上传令牌——探测发生在上传会话存在之前。

安全边界：
- 进程日志目录白名单固定，路径必须绝对且不含 ``..``；
- 客户端只提交中央目录事实，``quality`` 由平台推导；
- 所有写入按唯一键幂等，重复上报不会把时间序列或统计放大。
"""

from __future__ import annotations

import base64
import binascii
import gzip
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, Response
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import text

from ..domain.log_collect_display_generated import (
    MIN_ARCHIVE_BYTES,
    NETWORK_GRADE_LABELS,
    THRESHOLDS,
)
from ..schemas.log_collect_evidence import (
    ArtifactManifestIn,
    BandwidthAck,
    EvidenceAck,
    NetworkProbeAck,
    NetworkProbeIn,
    ProcessLogIn,
    TransferMetricIn,
)
from ..services.log_collect_evidence_service import (
    MAX_BANDWIDTH_BYTES,
    LogCollectEvidenceTicket,
    derive_manifest_quality,
    probe_grade,
)
from .log_collect import get_log_collect_service

logger = get_logger("log-collect-evidence")

router = APIRouter(prefix="/api/direct/log-collect", tags=["log-collect-evidence"])
tickets = LogCollectEvidenceTicket()

PROCESS_LOG_DIR = "/sf/log/today/acli/plugins/log_collect/"
MAX_PROCESS_LOG_FILE_BYTES = int(THRESHOLDS["max_process_log_file_bytes"])
MAX_PROCESS_LOG_TASK_BYTES = int(THRESHOLDS["max_process_log_task_bytes"])


def _unauthorized() -> HTTPException:
    return HTTPException(status_code=401, detail="证据票据无效或已过期")


async def _require_task(service: Any, task_id: UUID) -> dict[str, Any]:
    rows = await service.repository.session.execute(
        text(
            """
            SELECT task_id, case_id, upload_mode, trace_id
            FROM log_collect_task WHERE task_id = :task_id
            """
        ),
        {"task_id": str(task_id)},
    )
    mapped = [dict(row) for row in rows.mappings().all()]
    if not mapped:
        raise HTTPException(status_code=404, detail="任务不存在")
    return mapped[0]


def _trace_of(task: dict[str, Any]) -> str:
    return str(task.get("trace_id") or get_current_trace_id() or "")


@router.post("/tasks/{task_id}/metrics", response_model=EvidenceAck)
async def put_metric(
    task_id: UUID,
    payload: TransferMetricIn,
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    service: Any = Depends(get_log_collect_service),
) -> EvidenceAck:
    """一条传输样本；``(task_id, seq)`` 幂等。"""
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    task = await _require_task(service, task_id)
    session = service.repository.session
    result = await session.execute(
        text(
            """
            INSERT INTO log_collect_transfer_metric
                (task_id, trace_id, seq, stage, bytes_done, bytes_total, percent,
                 elapsed_ms, rate_bps, eta_sec, stalled, source)
            VALUES
                (:task_id, :trace_id, :seq, :stage, :bytes_done, :bytes_total, :percent,
                 :elapsed_ms, :rate_bps, :eta_sec, :stalled, :source)
            ON CONFLICT DO NOTHING
            RETURNING id
            """
        ),
        {"task_id": str(task_id), "trace_id": _trace_of(task), **payload.model_dump()},
    )
    inserted = result.first()
    snapshot = {**payload.model_dump(), "created_at": datetime.now(UTC).isoformat()}
    # 客户面板只轮询任务行：把最新样本写进 detail 快照，读侧就不必 N+1 查窄表。
    # seq 单调保护：乱序上报（重试/多 bridge）不得用旧样本覆盖新样本。
    await session.execute(
        text(
            """
            UPDATE log_collect_task
            SET detail = COALESCE(detail, '{}'::jsonb)
                         || jsonb_build_object('transfer', CAST(:snapshot AS jsonb)),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
              AND :seq >= COALESCE((detail -> 'transfer' ->> 'seq')::bigint, -1)
            """
        ),
        {
            "task_id": str(task_id),
            "seq": payload.seq,
            "snapshot": json.dumps(snapshot, ensure_ascii=False),
        },
    )
    await session.commit()
    return EvidenceAck(
        task_id=task_id,
        id=str(inserted[0]) if inserted else None,
        duplicate=inserted is None,
    )


@router.post("/tasks/{task_id}/network-probe", response_model=NetworkProbeAck)
async def put_network_probe(
    task_id: UUID,
    payload: NetworkProbeIn,
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    service: Any = Depends(get_log_collect_service),
) -> NetworkProbeAck:
    """一次探测结果；分级由平台按丢包/RTT/抖动推导，不信任客户端 grade。"""
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    task = await _require_task(service, task_id)
    grade = probe_grade(
        reachable=payload.reachable,
        loss_rate=payload.loss_rate,
        rtt_avg_ms=payload.rtt_avg_ms,
        jitter_ms=payload.jitter_ms,
    )
    probe_id = uuid4()
    await service.repository.session.execute(
        text(
            """
            INSERT INTO log_collect_network_probe
                (probe_id, case_id, task_id, trace_id, upload_mode, target, reachable,
                 sample_count, loss_rate, rtt_min_ms, rtt_avg_ms, rtt_max_ms, jitter_ms,
                 dns_ms, connect_ms, http_code, up_bps, down_bps, grade, advice, samples,
                 bridge_version)
            VALUES
                (:probe_id, :case_id, :task_id, :trace_id, :upload_mode, :target, :reachable,
                 :sample_count, :loss_rate, :rtt_min_ms, :rtt_avg_ms, :rtt_max_ms, :jitter_ms,
                 :dns_ms, :connect_ms, :http_code, :up_bps, :down_bps, :grade, :advice,
                 CAST(:samples AS jsonb), :bridge_version)
            """
        ),
        {
            "probe_id": str(probe_id),
            "case_id": task.get("case_id"),
            "task_id": str(task_id),
            "trace_id": _trace_of(task),
            "upload_mode": task.get("upload_mode"),
            "grade": grade,
            "samples": json.dumps(payload.samples, ensure_ascii=False),
            **payload.model_dump(exclude={"samples"}),
        },
    )
    await service.repository.session.execute(
        text(
            """
            WITH latest AS (
                SELECT DISTINCT ON (target)
                       target, reachable, grade, rtt_avg_ms, jitter_ms, loss_rate,
                       http_code, up_bps, down_bps, advice, created_at
                FROM log_collect_network_probe
                WHERE task_id = :task_id
                ORDER BY target, created_at DESC
            )
            UPDATE log_collect_task
            SET detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object(
                    'network_probes',
                    COALESCE((SELECT jsonb_agg(to_jsonb(latest)) FROM latest), '[]'::jsonb)),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
            """
        ),
        {"task_id": str(task_id)},
    )
    await service.repository.session.commit()
    logger.info(
        "log_collect.network_probe task_id=%s probe_id=%s target=%s grade=%s trace_id=%s",
        task_id,
        probe_id,
        payload.target,
        grade,
        _trace_of(task),
    )
    return NetworkProbeAck(
        task_id=task_id,
        id=str(probe_id),
        probe_id=probe_id,
        ticket=tickets.issue(str(probe_id), probe=True),
        grade=grade,
        grade_label=NETWORK_GRADE_LABELS.get(grade, NETWORK_GRADE_LABELS["unknown"]),
    )


@router.post("/tasks/{task_id}/process-logs", response_model=EvidenceAck)
async def put_process_log(
    task_id: UUID,
    payload: ProcessLogIn,
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    service: Any = Depends(get_log_collect_service),
) -> EvidenceAck:
    """一条节点采集进程日志（gzip 字节落库，正文由管理端单独读取）。"""
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    path = payload.path
    if not path.startswith(PROCESS_LOG_DIR) or ".." in path or "\x00" in path:
        raise HTTPException(status_code=400, detail="进程日志路径不在白名单目录内")
    if payload.size > MAX_PROCESS_LOG_FILE_BYTES:
        raise HTTPException(status_code=413, detail="单个进程日志超过上限")

    content: bytes | None = None
    if payload.content_gz_b64:
        try:
            content = base64.b64decode(payload.content_gz_b64, validate=True)
            gzip.decompress(content)
        except (binascii.Error, ValueError, OSError, EOFError) as exc:
            raise HTTPException(status_code=400, detail="进程日志内容不是合法 gzip") from exc

    task = await _require_task(service, task_id)
    session = service.repository.session
    used = await session.execute(
        text("SELECT COALESCE(SUM(size), 0) FROM log_collect_process_log WHERE task_id = :task_id"),
        {"task_id": str(task_id)},
    )
    if int(used.scalar() or 0) + payload.size > MAX_PROCESS_LOG_TASK_BYTES:
        raise HTTPException(status_code=413, detail="该任务进程日志总量超过上限")

    log_id = uuid4()
    result = await session.execute(
        text(
            """
            INSERT INTO log_collect_process_log
                (log_id, task_id, case_id, trace_id, host, path, size, sha256,
                 content_gz, truncated, status, error, collected_at)
            VALUES
                (:log_id, :task_id, :case_id, :trace_id, :host, :path, :size, :sha256,
                 :content_gz, :truncated, :status, :error, :collected_at)
            ON CONFLICT DO NOTHING
            RETURNING log_id
            """
        ),
        {
            "log_id": str(log_id),
            "task_id": str(task_id),
            "case_id": task.get("case_id"),
            "trace_id": _trace_of(task),
            "host": payload.host,
            "path": path,
            "size": payload.size,
            "sha256": payload.sha256,
            "content_gz": content,
            "truncated": payload.truncated,
            "status": payload.status,
            "error": payload.error,
            "collected_at": payload.collected_at or datetime.now(UTC),
        },
    )
    inserted = result.first()
    await session.commit()
    return EvidenceAck(
        task_id=task_id,
        id=str(inserted[0]) if inserted else str(log_id),
        duplicate=inserted is None,
    )


@router.post("/tasks/{task_id}/artifact-manifest", response_model=EvidenceAck)
async def put_artifact_manifest(
    task_id: UUID,
    payload: ArtifactManifestIn,
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    service: Any = Depends(get_log_collect_service),
) -> EvidenceAck:
    """包内容探测清单；质量由中央目录事实推导，并回写任务证据快照。"""
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    quality, reason = derive_manifest_quality(
        archive_size=payload.archive_size,
        entry_count=payload.entry_count,
        entry_total_bytes=payload.entry_total_bytes,
        readable=payload.readable,
    )
    task = await _require_task(service, task_id)
    session = service.repository.session
    params = {
        "task_id": str(task_id),
        "case_id": task.get("case_id"),
        "trace_id": _trace_of(task),
        "source": payload.source,
        "archive_path": payload.archive_path,
        "archive_size": payload.archive_size,
        "archive_sha256": payload.archive_sha256,
        "entry_count": payload.entry_count,
        "entry_total_bytes": payload.entry_total_bytes,
        "entries": json.dumps(payload.entries[:500], ensure_ascii=False),
        "has_encrypted": payload.has_encrypted,
        "quality": quality,
        "reason": reason,
        "evidence": json.dumps(
            {
                "min_archive_bytes": MIN_ARCHIVE_BYTES,
                "readable": payload.readable,
                "declared_entries": len(payload.entries),
                "source": payload.source,
                **payload.evidence,
            },
            ensure_ascii=False,
        ),
    }
    result = await session.execute(
        text(
            """
            INSERT INTO log_collect_artifact_manifest
                (task_id, case_id, trace_id, source, archive_path, archive_size,
                 archive_sha256, entry_count, entry_total_bytes, entries, has_encrypted,
                 quality, reason, evidence)
            VALUES
                (:task_id, :case_id, :trace_id, :source, :archive_path, :archive_size,
                 :archive_sha256, :entry_count, :entry_total_bytes, :entries::jsonb,
                 :has_encrypted, :quality, :reason, :evidence::jsonb)
            ON CONFLICT DO NOTHING
            RETURNING manifest_id
            """
        ),
        params,
    )
    inserted = result.first()
    # 重新探测（例如修复后重采）必须允许覆盖旧结论，否则质量永远锁死在第一次。
    await session.execute(
        text(
            """
            UPDATE log_collect_artifact_manifest
            SET archive_size = :archive_size, entry_count = :entry_count,
                entry_total_bytes = :entry_total_bytes, entries = :entries::jsonb,
                has_encrypted = :has_encrypted, quality = :quality, reason = :reason,
                evidence = :evidence::jsonb, inspected_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id AND source = :source
              AND COALESCE(archive_sha256, '') = COALESCE(:archive_sha256, '')
            """
        ),
        params,
    )
    await session.execute(
        text(
            """
            UPDATE log_collect_task
            SET detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object(
                    'artifact_quality', :quality,
                    'artifact_reason', :reason,
                    'artifact_evidence', :evidence::jsonb),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
              AND (detail ->> 'artifact_quality' IS DISTINCT FROM 'ok' OR :quality = 'ok')
            """
        ),
        params,
    )
    await session.commit()
    logger.info(
        "log_collect.artifact_manifest task_id=%s quality=%s reason=%s entries=%s trace_id=%s",
        task_id,
        quality,
        reason,
        payload.entry_count,
        _trace_of(task),
    )
    return EvidenceAck(
        task_id=task_id,
        id=str(inserted[0]) if inserted else None,
        duplicate=inserted is None,
    )


@router.post("/network-probe/{probe_id}/uplink", response_model=BandwidthAck)
async def bandwidth_uplink(
    probe_id: UUID,
    request: Request,
    x_probe_ticket: str = Header(default="", alias="X-Probe-Ticket"),
) -> BandwidthAck:
    """受控上行测速：body 即丢，不落盘、不进数据库。"""
    if not tickets.verify(str(probe_id), x_probe_ticket):
        raise _unauthorized()
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > MAX_BANDWIDTH_BYTES:
            raise HTTPException(status_code=413, detail="测速载荷超过上限")
    return BandwidthAck(bytes=received, probe_id=probe_id)


@router.get("/network-probe/{probe_id}/downlink")
async def bandwidth_downlink(
    probe_id: UUID,
    x_probe_ticket: str = Header(default="", alias="X-Probe-Ticket"),
    bytes_: int = Query(1024 * 1024, alias="bytes", ge=1, le=MAX_BANDWIDTH_BYTES),
) -> Response:
    """受控下行测速：返回固定字节，不查库、不落盘。"""
    if not tickets.verify(str(probe_id), x_probe_ticket):
        raise _unauthorized()
    # 固定字节流：可复现、可比对，且不会把数据库内容泄到测速通道。
    body = (b"0123456789abcdef" * ((bytes_ // 16) + 1))[:bytes_]
    return Response(
        content=body,
        media_type="application/octet-stream",
        headers={"Cache-Control": "no-store", "X-Probe-Id": str(probe_id)},
    )
