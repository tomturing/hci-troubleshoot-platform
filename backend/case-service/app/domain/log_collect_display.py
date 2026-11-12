"""日志收集展示派生层：``phase / conclusion / conditions`` 的唯一判定点。

为什么需要这一层：任务表里只有「采集状态」与「交付状态」两个正交事实，
没有任何字段能单独回答「这次究竟有没有拿到可用日志」。界面过去直接把
``status=completed`` 渲染成绿色「已完成」，于是出现「已完成 100% +
交付结论 未开始 + 包大小 —」并排（Q2026111201130），608 字节空壳包被判成功
（Q2026111200624）。判定一旦散落在前端，每种视图都会各自发明口径。

三维语义：
    phase       当前环节，互斥
    conclusion  业务结论，互斥，``pending`` 表示尚未终结
    conditions  并存的观察事实，多值

conclusion 的三条硬规则（对抗性审查后收敛）：
    1. 已结束但从未交付 → ``failed``；若按 ``pending`` 渲染，界面会重现
       「已完成 100% + 交付未开始」，且交付可补、结论必须诚实。
    2. 已交付但产物不可用/不可信 → ``partial``：字节确实到了，结论是
       「有疑点，建议重采」，既不宣称成功也不宣称什么都没到。
    3. 只有「已交付 + 交付证据成立 + 包内容校验通过」才是 ``success``。
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from .log_collect_display_generated import (
    ARTIFACT_QUALITY_LABELS,
    ARTIFACT_REASON_LABELS,
    COLLECT_BAND_END,
    CONCLUSION_LABELS,
    CONCLUSION_TONES,
    CONDITION_LABELS,
    DELIVERY_BAND_END,
    MIN_ARCHIVE_BYTES,
    NETWORK_GRADE_LABELS,
    PHASE_LABELS,
    STALL_SECONDS,
    TRANSFER_STAGE_LABELS,
    UPLOAD_MODE_LABELS,
    format_size,
)

SETTLED_STATUSES: frozenset[str] = frozenset({"completed", "failed", "cancelled"})
_ARTIFACT_QUALITIES = frozenset({"ok", "suspect", "invalid", "unknown"})
# 「最差档」排序：unknown 排在最低，避免一个未探测目标把已知的 poor 盖掉。
_GRADE_RANK = {"unknown": 0, "good": 1, "fair": 2, "poor": 3}

# 顺序固定：同一份数据在任何入口都必须渲染出同一个顺序。
_CONDITION_ORDER: tuple[str, ...] = (
    "artifact_verified",
    "artifact_suspect",
    "artifact_missing",
    "artifact_uninspected",
    "integrity_mismatch",
    "handoff_pending",
    "delivered_unverified",
    "network_degraded",
    "stalled",
    "bridge_outdated",
)

# 与当前交付路径相关的探测目标：P1 只看 5888，P2 只看平台数据口。
# 混判会把「5888 不通但 P2 正常」误报成网络不佳。
_TARGET_FOR_MODE = {"acli_direct": "log_server", "bridge_relay": "platform_data"}


def _get(obj: Any, key: str, default: Any = None) -> Any:
    """同时支持 ORM 行与字典行（admin 路由用原生 SQL 取的是 dict）。"""
    if obj is None:
        return default
    if isinstance(obj, Mapping):
        return obj.get(key, default)
    return getattr(obj, key, default)


def _detail(task: Any) -> dict[str, Any]:
    raw = _get(task, "detail")
    return dict(raw) if isinstance(raw, Mapping) else {}


def _as_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _as_float(value: Any) -> float | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _normalize_quality(value: Any) -> str:
    quality = str(value or "").strip().lower()
    return quality if quality in _ARTIFACT_QUALITIES else "unknown"


def _default_reason(quality: str) -> str:
    return {"ok": "ok", "suspect": "entry_count_low", "invalid": "missing"}.get(quality, "not_inspected")


def archive_bytes(task: Any) -> int | None:
    """产物体积：P2 优先平台对象，P1 只有节点侧归档。

    0 与缺失同义（``NULLIF(x, 0)``）：0 字节不构成「有产物」的证据。
    """
    for key in ("object_size", "archive_size"):
        value = _as_int(_get(task, key))
        if value:
            return value
    return None


def assess_artifact(
    task: Any,
    manifest: Mapping[str, Any] | None = None,
    *,
    allow_size_inference: bool = True,
) -> tuple[str, str, str]:
    """产物质量判定，返回 ``(quality, reason, source)``。

    证据优先级（强 → 弱）：
        1. 内容探测清单（bridge 读 zip 中央目录）——唯一能识别空壳包的证据；
        2. ``detail.artifact_quality`` 快照（历史订正或上一次探测结论）；
        3. 体积推断——只能判「无包 / 过小」，判不出条目不足，故绝不推断 ok。

    ``allow_size_inference=False``（任务尚未终结时）禁用体积推断：
    采集中的「还没有包」不是缺陷证据，否则界面会在采集途中就报「未取到有效日志」。

    禁止用 ``unzip -t`` 推导：客户包全条目加密，密码错会被误判损坏。
    """
    detail = _detail(task)
    if isinstance(manifest, Mapping):
        quality = _normalize_quality(manifest.get("quality"))
        if quality != "unknown":
            reason = str(manifest.get("reason") or _default_reason(quality))
            return quality, reason, "manifest"

    recorded = _normalize_quality(detail.get("artifact_quality"))
    if recorded != "unknown":
        return recorded, str(detail.get("artifact_reason") or _default_reason(recorded)), "recorded"

    if not allow_size_inference:
        return "unknown", "not_inspected", "size_inference"

    size = archive_bytes(task)
    mode = str(_get(task, "upload_mode") or "")
    delivered = str(_get(task, "delivery_status") or "") == "delivered"

    if size is None:
        # P1 直传成功后 acli 自删节点包：体积缺失是正常现象，不是失败证据。
        if mode == "acli_direct" and delivered:
            return "unknown", "not_inspected", "deleted_after_delivery"
        return "invalid", "missing", "size_inference"
    if size < MIN_ARCHIVE_BYTES:
        return "invalid", "too_small", "size_inference"
    return "unknown", "not_inspected", "size_inference"


def delivery_evidence_ok(task: Any) -> bool:
    """交付证据是否达到平台可核验等级。

    P2：平台合并并代传 5888 成功（``relay_status=success``）；
    P1：平台回查过归档件（``p1_archive_check.verified``）。仅 acli 自报不算。
    """
    if str(_get(task, "relay_status") or "") == "success":
        return True
    detail = _detail(task)
    check = detail.get("p1_archive_check")
    if isinstance(check, Mapping) and check.get("verified") is True:
        return True
    return detail.get("delivery_evidence") == "archive_verified"


def derive_phase(task: Any) -> str:
    """当前环节。交付阶段优先：采集结束后交付仍在跑时，用户要看的是「代传中」。"""
    delivery = str(_get(task, "delivery_status") or "")
    # 交付已终结是比采集状态更强的事实：节点可能永远不把 status 推到 completed，
    # 但平台已经收妥字节，就不能继续把任务算作「传输中」。
    if delivery == "delivered":
        return "settled"
    if delivery == "pulling":
        return "fetching"
    if delivery == "uploading":
        return "uploading"
    if delivery == "relaying":
        return "relaying"

    status = str(_get(task, "status") or "pending")
    if status in {"pending", "probing", "installing"}:
        return "queued"
    if status in {"collecting", "packaging"}:
        return "collecting"
    if status == "uploading":
        return "uploading"
    return "settled"


def _progress_percent(
    phase: str, task: Any, transfer: Mapping[str, Any] | None, *, delivered: bool
) -> int | None:
    """合成进度（0-100）。None 表示未知，界面必须显示 —，不许显示 0。

    分段权重来自契约 ``bands``：采集 0-60、交付 60-90、终结 100。这样
    「采集完成但未交付」不会显示成 100%，从根上消除历史那种矛盾组合。
    终结态只有「平台已收妥」才给 100：零产物/从未交付的终态若给 100%，
    管理端会再次出现「合成进度 100% + 交付事实 未开始」的认知冲突。
    """
    if phase == "settled":
        return 100 if delivered else None
    if phase == "queued":
        return 0
    if phase == "collecting":
        raw = _as_int(_get(task, "progress"))
        if raw is None:
            return 0
        return max(0, min(100, raw)) * COLLECT_BAND_END // 100
    if isinstance(transfer, Mapping):
        percent = _as_float(transfer.get("percent"))
        if percent is not None:
            percent = max(0.0, min(100.0, percent))
            span = DELIVERY_BAND_END - COLLECT_BAND_END
            return int(round(COLLECT_BAND_END + percent * span / 100))
    return None


def _reason_text(reason: str) -> str:
    """体积类原因补上具体阈值，避免用户面对一句「过小」无从判断。"""
    label = ARTIFACT_REASON_LABELS.get(reason) or ARTIFACT_REASON_LABELS["not_inspected"]
    if reason == "too_small":
        return f"{label}（可用阈值 {format_size(MIN_ARCHIVE_BYTES)}）"
    return label


def _artifact_payload(
    task: Any,
    manifest: Mapping[str, Any] | None,
    *,
    allow_size_inference: bool,
) -> dict[str, Any]:
    quality, reason, source = assess_artifact(task, manifest, allow_size_inference=allow_size_inference)
    size = archive_bytes(task)
    entry_count = _as_int(manifest.get("entry_count")) if isinstance(manifest, Mapping) else None
    entry_total = _as_int(manifest.get("entry_total_bytes")) if isinstance(manifest, Mapping) else None
    return {
        "quality": quality,
        "quality_label": ARTIFACT_QUALITY_LABELS.get(quality, ARTIFACT_QUALITY_LABELS["unknown"]),
        "reason": reason,
        "reason_text": _reason_text(reason),
        "size": size,
        "size_text": format_size(size),
        "entry_count": entry_count,
        "entry_total_bytes": entry_total,
        "entry_total_text": format_size(entry_total) if entry_total is not None else None,
        "source": source,
    }


def _sample_time(sample: Mapping[str, Any] | None) -> datetime | None:
    value = sample.get("created_at") if isinstance(sample, Mapping) else None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def _transfer_payload(
    task: Any,
    metrics: Mapping[str, Any] | None,
    *,
    now: datetime,
) -> dict[str, Any] | None:
    """传输样本：优先服务端窄表最新样本，其次 detail 快照。

    老 bridge 不上报时返回 None —— 界面显示「桥接端版本较旧，暂无该数据」，
    绝不能拿 0 或 100 顶替（那正是历史假进度的来源）。
    """
    source: Mapping[str, Any] | None = metrics if isinstance(metrics, Mapping) else None
    if source is None:
        snapshot = _detail(task).get("transfer")
        source = snapshot if isinstance(snapshot, Mapping) else None
    if source is None:
        return None

    stage = str(source.get("stage") or "").strip() or None
    rate_bps = _as_float(source.get("rate_bps"))
    bytes_done = _as_int(source.get("bytes_done"))
    bytes_total = _as_int(source.get("bytes_total"))
    percent = _as_float(source.get("percent"))
    if percent is None and bytes_done is not None and bytes_total:
        percent = bytes_done * 100.0 / bytes_total

    stalled = bool(source.get("stalled"))
    stalled_seconds = _as_int(source.get("stalled_seconds"))
    if stalled and stalled_seconds is None:
        created = _sample_time(source)
        if created is not None:
            reference = created if created.tzinfo else created.replace(tzinfo=UTC)
            stalled_seconds = max(0, int((now - reference).total_seconds()))

    return {
        "stage": stage,
        "stage_label": TRANSFER_STAGE_LABELS.get(stage) if stage else None,
        "bytes_done": bytes_done,
        "bytes_total": bytes_total,
        "percent": round(percent, 2) if percent is not None else None,
        "elapsed_ms": _as_int(source.get("elapsed_ms")),
        "rate_bps": rate_bps,
        "rate_text": f"{format_size(rate_bps)}/s" if rate_bps and rate_bps > 0 else None,
        "eta_sec": _as_int(source.get("eta_sec")),
        "stalled": stalled,
        "stalled_seconds": stalled_seconds if stalled else None,
        "stall_threshold_seconds": STALL_SECONDS,
        "updated_at": _sample_time(source),
        "source": str(source.get("source") or "") or None,
    }


def _network_payload(
    probes: Iterable[Mapping[str, Any]] | None,
    upload_mode: str | None,
) -> dict[str, Any] | None:
    """网络探测：每目标取最新一条，并按交付路径挑选相关目标定级。

    告警只认相关目标（P1 看 5888、P2 看平台数据口），否则「5888 不通但 P2
    正常」会被误判成网络不佳，把用户赶去切换交付模式。
    """
    if not probes:
        return None
    latest: dict[str, Mapping[str, Any]] = {}
    for probe in probes:
        if not isinstance(probe, Mapping):
            continue
        target = str(probe.get("target") or "unknown")
        current = latest.get(target)
        if current is None:
            latest[target] = probe
            continue
        stamp, current_stamp = _sample_time(probe), _sample_time(current)
        if stamp and (current_stamp is None or stamp > current_stamp):
            latest[target] = probe
    if not latest:
        return None

    targets: list[dict[str, Any]] = []
    for target, probe in sorted(latest.items()):
        grade = str(probe.get("grade") or "unknown")
        if grade not in NETWORK_GRADE_LABELS:
            grade = "unknown"
        up_bps = _as_float(probe.get("up_bps"))
        down_bps = _as_float(probe.get("down_bps"))
        targets.append(
            {
                "target": target,
                "reachable": probe.get("reachable"),
                "grade": grade,
                "grade_label": NETWORK_GRADE_LABELS[grade],
                "rtt_avg_ms": _as_float(probe.get("rtt_avg_ms")),
                "jitter_ms": _as_float(probe.get("jitter_ms")),
                "loss_rate": _as_float(probe.get("loss_rate")),
                "http_code": _as_int(probe.get("http_code")),
                "up_bps": up_bps,
                "down_bps": down_bps,
                "up_text": f"{format_size(up_bps)}/s" if up_bps else None,
                "down_text": f"{format_size(down_bps)}/s" if down_bps else None,
                "advice": probe.get("advice"),
                "checked_at": _sample_time(probe),
            }
        )

    relevant_target = _TARGET_FOR_MODE.get(str(upload_mode or ""))
    relevant = [item for item in targets if item["target"] == relevant_target]
    if not relevant:
        # 路径未定（老任务）时退化为「所有目标里最差的一个」：宁可提示，不可漏报。
        relevant = targets
    # 取相关目标里最差的一档；初值必须是 None，否则 unknown(3) 永远压过 good(0)。
    overall: str | None = None
    for item in relevant:
        if overall is None or _GRADE_RANK.get(item["grade"], 3) > _GRADE_RANK.get(overall, 3):
            overall = item["grade"]
    overall = overall or "unknown"

    return {
        "grade": overall,
        "grade_label": NETWORK_GRADE_LABELS[overall],
        "relevant_target": relevant_target,
        "targets": targets,
        "checked_at": max(
            (item["checked_at"] for item in targets if item["checked_at"] is not None),
            default=None,
        ),
    }


def build_transfer_view(sample: Mapping[str, Any] | None, *, now: datetime | None = None) -> dict[str, Any] | None:
    """单条传输样本 → 展示结构（管理端时间序列复用同一口径）。"""
    if not isinstance(sample, Mapping):
        return None
    return _transfer_payload(None, sample, now=now or datetime.now(UTC))


def build_network_view(probes: Iterable[Mapping[str, Any]] | None, upload_mode: Any = None) -> dict[str, Any] | None:
    """探测行集合 → 展示结构（每目标取最新，按交付路径定级）。"""
    return _network_payload(probes, str(upload_mode or "") or None)


def _conclusion(
    *,
    status: str,
    delivery_status: str,
    phase: str,
    quality: str,
    delivered: bool,
    evidence_ok: bool,
) -> str:
    if status == "cancelled" or delivery_status == "cancelled":
        return "cancelled"
    if status == "failed" or delivery_status == "failed":
        return "failed"
    if phase != "settled":
        return "pending"
    if delivered:
        return "success" if evidence_ok and quality == "ok" else "partial"
    # 已结束但从未交付（Q2026111201130 的形态）：不能显示「进行中」。
    return "failed"


def _build_conditions(
    *,
    phase: str,
    quality: str,
    delivered: bool,
    evidence_ok: bool,
    status: str,
    delivery_status: str,
    detail: Mapping[str, Any],
    transfer: Mapping[str, Any] | None,
    network: Mapping[str, Any] | None,
) -> list[dict[str, str]]:
    present: set[str] = set()

    if phase == "settled" or quality != "unknown":
        if quality == "ok":
            present.add("artifact_verified")
        elif quality == "suspect":
            present.add("artifact_suspect")
        elif quality == "invalid":
            # 事实描述：608 字节空壳包同样属于「未取到有效日志」。
            present.add("artifact_missing")
        elif phase == "settled":
            present.add("artifact_uninspected")

    if detail.get("integrity_mismatch") or detail.get("sha_mismatch"):
        present.add("integrity_mismatch")
    if status == "completed" and delivery_status == "none":
        present.add("handoff_pending")
    if delivered and not evidence_ok:
        present.add("delivered_unverified")
    if isinstance(network, Mapping) and network.get("grade") == "poor":
        present.add("network_degraded")
    if isinstance(transfer, Mapping) and transfer.get("stalled"):
        present.add("stalled")
    if detail.get("bridge_outdated"):
        present.add("bridge_outdated")

    return [{"key": key, "label": CONDITION_LABELS[key]} for key in _CONDITION_ORDER if key in present]


def derive(
    task: Any,
    *,
    manifest: Mapping[str, Any] | None = None,
    metrics: Mapping[str, Any] | None = None,
    probes: Iterable[Mapping[str, Any]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """返回响应四件套：``display / artifact / transfer / network``。

    ``manifest`` / ``metrics`` / ``probes`` 由调用方按需提供：
    客户面板与列表只用任务行（含 detail 快照）以避免 N+1；管理端详情抽屉
    再额外传入三张窄表的真实行。
    """
    now = now or datetime.now(UTC)
    status = str(_get(task, "status") or "")
    delivery_status = str(_get(task, "delivery_status") or "")
    detail = _detail(task)
    phase = derive_phase(task)
    delivered = delivery_status == "delivered"
    evidence_ok = delivery_evidence_ok(task)

    artifact = _artifact_payload(task, manifest, allow_size_inference=phase == "settled")
    quality = artifact["quality"]
    transfer = _transfer_payload(task, metrics, now=now)
    # 客户面板轮询只拿任务行；证据写侧会把「每目标最新探测」落进 detail，
    # 这里读快照即可，避免每次轮询多查两张窄表。管理端显式传 probes 时优先用真表。
    effective_probes = probes
    if effective_probes is None and isinstance(detail.get("network_probes"), list):
        effective_probes = detail["network_probes"]
    network = _network_payload(effective_probes, _get(task, "upload_mode"))
    conclusion = _conclusion(
        status=status,
        delivery_status=delivery_status,
        phase=phase,
        quality=quality,
        delivered=delivered,
        evidence_ok=evidence_ok,
    )

    return {
        "display": {
            "phase": phase,
            "phase_label": PHASE_LABELS[phase],
            "conclusion": conclusion,
            "conclusion_label": CONCLUSION_LABELS[conclusion],
            "conclusion_tone": CONCLUSION_TONES[conclusion],
            "conditions": _build_conditions(
                phase=phase,
                quality=quality,
                delivered=delivered,
                evidence_ok=evidence_ok,
                status=status,
                delivery_status=delivery_status,
                detail=detail,
                transfer=transfer,
                network=network,
            ),
            "progress_percent": _progress_percent(phase, task, transfer, delivered=delivered),
            "upload_mode_label": UPLOAD_MODE_LABELS.get(
                str(_get(task, "upload_mode") or ""), UPLOAD_MODE_LABELS["unknown"]
            ),
        },
        "artifact": artifact,
        "transfer": transfer,
        "network": network,
    }


def snapshot_artifact_evidence(task: Any) -> dict[str, Any] | None:
    """保存边界补写包证据快照；无变更返回 None。

    对应迁移 065 的实时版本：终态任务若还没有 ``detail.artifact_quality``，
    按体积可判定的「无包 / 过小」补上，避免新产生的行继续以「已完成」
    混过界面。已由内容探测写入的结论优先级更高，绝不覆盖。
    """
    status = str(_get(task, "status") or "")
    if status not in {"completed", "failed"}:
        return None
    detail = _detail(task)
    if _normalize_quality(detail.get("artifact_quality")) != "unknown":
        return None
    quality, reason, source = assess_artifact(task)
    if quality == "unknown":
        return None
    detail["artifact_quality"] = quality
    detail["artifact_reason"] = reason
    detail["artifact_evidence"] = source
    detail.setdefault(
        "display_audit",
        {
            "source": "save_boundary_snapshot",
            "at": datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "before": {"status": status, "artifact_quality": None},
        },
    )
    return detail
