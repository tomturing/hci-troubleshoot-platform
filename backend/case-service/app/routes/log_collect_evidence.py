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
import re
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Query, Request, Response
from shared.observability.logger import get_logger
from shared.observability.otel import get_current_trace_id
from sqlalchemy import text

from ..domain.log_collect_alerts import (
    extract_collect_alerts,
    is_collect_alert_source,
    merge_collect_alerts,
)
from ..domain.log_collect_display_generated import (
    MIN_ARCHIVE_BYTES,
    NETWORK_GRADE_LABELS,
    THRESHOLDS,
)
from ..schemas.log_collect_evidence import (
    ArtifactManifestIn,
    BandwidthAck,
    EvidenceAck,
    EvidenceErrorIn,
    FailureSnapshotIn,
    NetworkProbeAck,
    NetworkProbeIn,
    ProcessLogIn,
    TransferMetricIn,
)
from ..services.log_collect_evidence_service import (
    MAX_BANDWIDTH_BYTES,
    LogCollectEvidenceTicket,
    artifact_reason_code,
    derive_manifest_quality,
    probe_grade,
    reconcile_manifest_hosts,
)
from .log_collect import get_log_collect_service

logger = get_logger("log-collect-evidence")

router = APIRouter(prefix="/api/direct/log-collect", tags=["log-collect-evidence"])
tickets = LogCollectEvidenceTicket()

# 节点日志白名单根目录：acli 把运行日志统一写在 today/acli 下（acli.log、
# cmd_output/*.log、plugins/<name>/*.log）。只收 .log，且必须是该根目录的绝对子路径。
PROCESS_LOG_DIR = "/sf/log/today/acli/"
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


# 上报方版本头：bridge 每条证据请求都带，用来回答"这条数据是哪个 bridge 发的"。
#
# 为什么不能只靠启动事件 bridge.startup：那一刻浏览器通常还没连上，事件只能走
# 桥侧日志直传，一旦直传失败就永久丢失。2026-11-24 Q2026112400942 现场即如此：
# 平台侧查不到"这台跑的哪个版本"，只能让客户到桌面翻本地日志。
BRIDGE_VERSION_HEADER = "X-Bridge-Version"
BRIDGE_VERSION_MAX_LEN = 64
# 只接受版本串常见字符：既防注入，也避免把任意文本塞进 detail 后污染界面。
_BRIDGE_VERSION_PATTERN = re.compile(r"^[0-9A-Za-z._+()-]{1,64}$")


def _clean_bridge_version(raw: str | None) -> str:
    """规范化版本串；不合法一律当没上报（宁可空，不可脏）。"""
    value = (raw or "").strip()
    if not value or len(value) > BRIDGE_VERSION_MAX_LEN:
        return ""
    return value if _BRIDGE_VERSION_PATTERN.fullmatch(value) else ""


async def _record_bridge_version(service: Any, task_id: UUID, raw: str | None) -> None:
    """把上报方版本写进任务 detail，且只在变化时写一次。

    metrics 每 2 秒一条，如果无条件更新会把这个高频端点变成写放大源头，
    所以 SQL 里用 ``IS DISTINCT FROM`` 收敛成"版本变了才写"。
    """
    version = _clean_bridge_version(raw)
    if not version:
        return
    await service.repository.session.execute(
        text(
            """
            UPDATE log_collect_task
            SET detail = COALESCE(detail, '{}'::jsonb)
                         || jsonb_build_object('bridge_version', CAST(:version AS text)),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
              AND COALESCE(detail ->> 'bridge_version', '') IS DISTINCT FROM :version
            """
        ),
        {"task_id": str(task_id), "version": version},
    )


@router.post("/tasks/{task_id}/metrics", response_model=EvidenceAck)
async def put_metric(
    task_id: UUID,
    payload: TransferMetricIn,
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    x_bridge_version: str = Header(default="", alias=BRIDGE_VERSION_HEADER),
    service: Any = Depends(get_log_collect_service),
) -> EvidenceAck:
    """一条传输样本；``(task_id, seq)`` 幂等。"""
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    task = await _require_task(service, task_id)
    session = service.repository.session
    # 交付期间每 2 秒一条，正好覆盖"等待/拉取/上传"全程；
    # 内部按"版本变化才写"收敛，不会把这个高频端点变成写放大。
    await _record_bridge_version(service, task_id, x_bridge_version)
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
    x_bridge_version: str = Header(default="", alias=BRIDGE_VERSION_HEADER),
    service: Any = Depends(get_log_collect_service),
) -> NetworkProbeAck:
    """一次探测结果；分级由平台按丢包/RTT/抖动推导，不信任客户端 grade。"""
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    task = await _require_task(service, task_id)
    # 探测是每个任务的第一个证据请求，最能代表"是谁在跑这份采集"。
    bridge_version = _clean_bridge_version(payload.bridge_version) or _clean_bridge_version(
        x_bridge_version
    )
    await _record_bridge_version(service, task_id, bridge_version)
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
            # 覆盖 payload 里的空值：新 bridge 走请求头，旧 bridge 的 payload 字段仍为空。
            "bridge_version": bridge_version or None,
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
    # 目录为空时上报目录哨兵路径（status=empty、无正文），用于区分「没日志」与「没回传」；
    # 其余记录仍严格限定 today/acli 下的 .log，避免放宽后任意文件被写入。
    empty_marker = payload.status == "empty" and path.rstrip("/") == PROCESS_LOG_DIR.rstrip("/")
    if (
        not empty_marker
        and (
            not path.startswith(PROCESS_LOG_DIR)
            or not path.endswith(".log")
        )
        or ".." in path
        or "\x00" in path
    ):
        raise HTTPException(status_code=400, detail="进程日志路径不在白名单目录内（仅限 today/acli 下的 .log）")
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
    # 预算按轮独立：init 轮采到的安装日志不能被终态轮挤掉，
    # 否则失败时最关键的证据（acli 安装/初始化）反而丢失。
    used = await session.execute(
        text(
            "SELECT COALESCE(SUM(size), 0) FROM log_collect_process_log "
            "WHERE task_id = :task_id AND phase = :phase"
        ),
        {"task_id": str(task_id), "phase": payload.phase},
    )
    if int(used.scalar() or 0) + payload.size > MAX_PROCESS_LOG_TASK_BYTES:
        raise HTTPException(status_code=413, detail="该轮进程日志总量超过上限")

    log_id = uuid4()
    result = await session.execute(
        text(
            """
            INSERT INTO log_collect_process_log
                (log_id, task_id, case_id, trace_id, host, path, size, sha256,
                 content_gz, truncated, status, error, collected_at, phase)
            VALUES
                (:log_id, :task_id, :case_id, :trace_id, :host, :path, :size, :sha256,
                 :content_gz, :truncated, :status, :error, :collected_at, :phase)
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
            "phase": payload.phase,
        },
    )
    inserted = result.first()
    await session.commit()
    # 旁路派生：正文已在上面提交，派生失败绝不能再把回执打成 500。
    # 2026-11-24 现场就是这里没有兜底：_sync_collect_alerts 内部一个 TypeError
    # 直接把 /process-logs 变成 500，桥以为证据没上报成功，证据清单因此缺失
    # （客户界面 S7 只能显示「未知」），而正文其实早就写进库了。
    try:
        await _sync_collect_alerts(
            service,
            task_id=task_id,
            path=path,
            payload=payload,
            content=content,
            trace_id=_trace_of(task),
        )
    except Exception:  # noqa: BLE001 - 契约要求：派生失败只记日志，不影响回执
        logger.warning(
            "collect_alerts.sync_failed task_id=%s trace_id=%s",
            task_id, _trace_of(task), exc_info=True,
        )
    return EvidenceAck(
        task_id=task_id,
        id=str(inserted[0]) if inserted else str(log_id),
        duplicate=inserted is None,
    )


async def _sync_collect_alerts(
    service: Any,
    *,
    task_id: UUID,
    path: str,
    payload: Any,
    content: bytes | None,
    trace_id: str,
) -> None:
    """把采集插件日志里的失败行抽成告警快照，供客户/管理两端共用。

    证据正文已经落库，这一步是旁路派生：失败只记日志，绝不影响上报回执。
    只在 collection_log.log 且正文可读时运行；重采后日志干净会自动清空旧告警。
    """
    if not is_collect_alert_source(path) or not content:
        return
    try:
        # 变量名不能叫 text：那会遮蔽 sqlalchemy.text，下面第 421 行就会变成
        # 「'str' object is not callable」，整个上报接口 500（2026-11-24 现场）。
        log_text = gzip.decompress(content).decode("utf-8", errors="replace")
    except (OSError, EOFError, UnicodeError) as exc:
        logger.warning(
            "collect_alerts.decode_failed task_id=%s path=%s error=%s trace_id=%s",
            task_id, path, str(exc), trace_id,
        )
        return
    fresh = extract_collect_alerts(
        path,
        log_text,
        collected_at=payload.collected_at.isoformat() if payload.collected_at else None,
        trace_id=trace_id,
        node_ip=payload.host,
    )
    session = service.repository.session
    row = await session.execute(
        text("SELECT detail FROM log_collect_task WHERE task_id = :task_id"),
        {"task_id": str(task_id)},
    )
    raw_detail = row.scalar()
    detail = dict(raw_detail) if isinstance(raw_detail, dict) else {}
    merged = merge_collect_alerts(detail.get("collect_alerts"), fresh, source=path)
    previous = detail.get("collect_alerts")
    if previous == merged or (not previous and not merged):
        return
    await session.execute(
        text(
            """
            UPDATE log_collect_task
            SET detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object(
                    'collect_alerts', CAST(:alerts AS jsonb)),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
            """
        ),
        {"task_id": str(task_id), "alerts": json.dumps(merged, ensure_ascii=False)},
    )
    await session.commit()
    logger.info(
        "log_collect.collect_alerts task_id=%s source=%s alert_count=%s trace_id=%s",
        task_id, path, len(merged), trace_id,
    )


@router.post("/tasks/{task_id}/artifact-manifest", response_model=EvidenceAck)
async def put_artifact_manifest(
    task_id: UUID,
    payload: ArtifactManifestIn,
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    x_bridge_version: str = Header(default="", alias=BRIDGE_VERSION_HEADER),
    service: Any = Depends(get_log_collect_service),
) -> EvidenceAck:
    """包内容探测清单；质量由中央目录事实推导，并回写任务证据快照。"""
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    # P1 直传任务没有 transfer-metrics 流，清单是他们唯一的"交付前"证据，
    # 版本挂在这里才能覆盖直传场景。
    await _record_bridge_version(service, task_id, x_bridge_version)
    quality, reason = derive_manifest_quality(
        archive_size=payload.archive_size,
        entry_count=payload.entry_count,
        entry_total_bytes=payload.entry_total_bytes,
        readable=payload.readable,
    )
    # 目标节点对账：中央目录 ok 只说明包能读，不代表每台都产出了 sp_<ip>.tgz。
    # 只有把创建时的目标快照与条目名做差集，才能拦住「acli 报 6/6、实际 3 台」的假完成。
    missing_hosts, unexpected_hosts = reconcile_manifest_hosts(
        payload.expected_hosts,
        [str(entry.get("name") or "") for entry in payload.entries if isinstance(entry, dict)],
    )
    if quality == "ok" and missing_hosts:
        quality, reason = "incomplete", "missing_hosts"
    task = await _require_task(service, task_id)
    session = service.repository.session
    # 空包体检的细分码：一个原因对应一个码，页面据此给出不同处置建议。
    artifact_code = artifact_reason_code(quality, reason)
    params = {
        "task_id": str(task_id),
        "case_id": task.get("case_id"),
        "trace_id": _trace_of(task),
        "artifact_code": artifact_code,
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
        # 客户面板轮询只拿任务行：证据回传成功这件事必须能直接从 detail 读到，
        # 否则 S7 只能靠 quality 猜，永远不会变成「成功」（Q2026110302166）。
        "manifest_snapshot": json.dumps(
            {
                "received": True,
                "source": payload.source,
                "quality": quality,
                "reason": reason,
                "entry_count": payload.entry_count,
                "entry_total_bytes": payload.entry_total_bytes,
                "has_encrypted": payload.has_encrypted,
                "readable": payload.readable,
                "expected_hosts": payload.expected_hosts or [],
                "missing_hosts": missing_hosts,
                "unexpected_hosts": unexpected_hosts,
                "inspected_at": datetime.now(UTC).isoformat(),
                "trace_id": _trace_of(task),
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
                 :archive_sha256, :entry_count, :entry_total_bytes, CAST(:entries AS jsonb),
                 :has_encrypted, :quality, :reason, CAST(:evidence AS jsonb))
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
                entry_total_bytes = :entry_total_bytes, entries = CAST(:entries AS jsonb),
                has_encrypted = :has_encrypted, quality = :quality, reason = :reason,
                evidence = CAST(:evidence AS jsonb), inspected_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id AND source = :source
              AND COALESCE(archive_sha256, '') = COALESCE(:archive_sha256, '')
            """
        ),
        params,
    )
    # jsonb_build_object 的参数是多态 any：绑定参数若只出现在这里，asyncpg 无法
    # 推断类型（IndeterminateDatatypeError: could not determine data type of parameter）。
    # quality/reason 必须显式 CAST AS text 给定类型（Q2026111700614 现场 500 根因）。
    await session.execute(
        text(
            """
            UPDATE log_collect_task
            SET detail = (
                    COALESCE(detail, '{}'::jsonb)
                    -- 「证据是否到达」与「包内容好不好读」是两件事，必须分开写。
                    -- 旧实现把两者塞进同一个 jsonb_build_object，再用
                    -- `WHERE detail ->> 'artifact_quality' IS DISTINCT FROM 'ok' OR :quality = 'ok'`
                    -- 整体挡住：一旦质量已被判过 ok，这次上报就连清单快照一起丢掉，
                    -- 客户侧 S7 永远停在「未知」（2026-11-24 5 个任务命中）。
                    -- 快照无条件写，质量保留"已 ok 不被更差结论覆盖"的语义。
                    || jsonb_build_object(
                        'artifact_manifest', CAST(:manifest_snapshot AS jsonb))
                    || CASE
                           WHEN (detail ->> 'artifact_quality' IS DISTINCT FROM 'ok'
                                 OR :quality = 'ok')
                           THEN jsonb_build_object(
                               'artifact_quality', CAST(:quality AS text),
                               'artifact_reason', CAST(:reason AS text),
                               -- 空包体检细分码：NULLIF 让「未探测/正常」不落空壳键，
                               -- 读侧只用 `detail ->> 'artifact_code'` 就能判断有没有细分码。
                               'artifact_code', NULLIF(CAST(:artifact_code AS text), ''),
                               'artifact_evidence', CAST(:evidence AS jsonb))
                           ELSE '{}'::jsonb
                       END
                    -- 成功清单到达即作废旧的失败陈述：重试成功不该继续显示失败。
                ) - 'evidence_error',
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
            """
        ),
        params,
    )
    await session.commit()
    # 清单可能晚于失败终态到达：失败后再补一次产物定位，用户才有「上传到平台」的入口。
    # 旁路动作，失败只记日志，不影响证据落库本身。
    if str(task.get("status") or "") in ("failed", "cancelled"):
        try:
            await service.backfill_failed_artifact(task_id)
        except Exception as exc:  # noqa: BLE001 - 补登记不能反噬证据写入
            logger.warning(
                "artifact.backfill_failed",
                task_id=str(task_id),
                trace_id=_trace_of(task),
                error=str(exc),
            )
    logger.info(
        "log_collect.artifact_manifest task_id=%s quality=%s reason=%s code=%s entries=%s trace_id=%s",
        task_id,
        quality,
        reason,
        artifact_code,
        payload.entry_count,
        _trace_of(task),
    )
    return EvidenceAck(
        task_id=task_id,
        id=str(inserted[0]) if inserted else None,
        duplicate=inserted is None,
    )


@router.post("/tasks/{task_id}/evidence-error", response_model=EvidenceAck)
async def put_evidence_error(
    task_id: UUID,
    payload: EvidenceErrorIn,
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    service: Any = Depends(get_log_collect_service),
) -> EvidenceAck:
    """记录一次明确的证据上报失败（HTTP 码 / 异常）。

    为什么需要它：平台只能观察到「有没有收到清单」，无法凭缺失区分
    「桥还没跑完」与「真的传不进去」。历史上两者都被写成 S7 失败，
    用户看到「证据回传失败」而字节其实早已到齐（Q2026110302166）；
    反过来，真的传不进去时又只能说「未知」，管理员无从定责。
    这条陈述只写事实，结论仍由 derive 推导。

    已收到成功清单的任务不再接受失败陈述：旧失败不能覆盖新成功。
    """
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    task = await _require_task(service, task_id)
    session = service.repository.session
    await session.execute(
        text(
            """
            UPDATE log_collect_task
            SET detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object(
                    'evidence_error', CAST(:payload AS jsonb)),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
              AND detail -> 'artifact_manifest' IS NULL
            """
        ),
        {
            "task_id": str(task_id),
            "payload": json.dumps(
                {
                    "kind": payload.kind,
                    "error": payload.error,
                    "http_code": payload.http_code,
                    "at": datetime.now(UTC).isoformat(),
                    "trace_id": _trace_of(task),
                },
                ensure_ascii=False,
            ),
        },
    )
    await session.commit()
    logger.warning(
        "log_collect.evidence_error task_id=%s kind=%s http_code=%s error=%s trace_id=%s",
        task_id,
        payload.kind,
        payload.http_code,
        payload.error[:200],
        _trace_of(task),
    )
    return EvidenceAck(task_id=task_id)


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


@router.post("/tasks/{task_id}/failure-snapshot", response_model=EvidenceAck)
async def put_failure_snapshot(
    task_id: UUID,
    # 显式声明为 body：本文件其余端点依赖模型注解自动识别，但新端点在此 FastAPI
    # 版本下会被当成 query 参数（422 loc=["query","payload"]），直接标注最稳。
    payload: FailureSnapshotIn = Body(...),
    x_evidence_ticket: str = Header(default="", alias="X-Evidence-Ticket"),
    service: Any = Depends(get_log_collect_service),
) -> EvidenceAck:
    """失败现场快照：节点上关键路径与磁盘事实，写入 detail.failure_snapshot。

    第一性原理：失败结论已经写进任务状态，但「为什么失败」的现场（文件在不在、
    磁盘还有多少）只有节点知道。把这份事实随证据上报，才能做到「组件坏掉时
    平台自己闭环」，而不是每次都让客户登录设备敲 ls / df。

    只写 evidence 容器（服务端 single-writer），不碰任务状态与交付字段。
    """
    if not tickets.verify(str(task_id), x_evidence_ticket):
        raise _unauthorized()
    await _require_task(service, task_id)
    session = service.repository.session
    snapshot = {
        "reason": payload.reason,
        "host": payload.host,
        "files": payload.files,
        "disk": payload.disk,
        "collected_at": payload.collected_at or datetime.now(UTC).isoformat(),
    }
    await session.execute(
        text(
            """
            UPDATE log_collect_task
            SET detail = COALESCE(detail, '{}'::jsonb) || jsonb_build_object(
                    'failure_snapshot', CAST(:snapshot AS jsonb)),
                updated_at = CURRENT_TIMESTAMP
            WHERE task_id = :task_id
            """
        ),
        {"task_id": str(task_id), "snapshot": json.dumps(snapshot, ensure_ascii=False)},
    )
    await session.commit()
    logger.info(
        "log_collect.failure_snapshot task_id=%s reason=%s host=%s files=%s",
        task_id,
        payload.reason,
        payload.host,
        len(payload.files),
    )
    return EvidenceAck(task_id=task_id)
