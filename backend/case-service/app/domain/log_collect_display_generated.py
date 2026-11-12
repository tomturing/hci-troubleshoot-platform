"""自动生成，请勿手改。

源：contracts/log_collect_display.json
生成：python3 scripts/gen_log_collect_display.py
"""

from __future__ import annotations

CONCLUSION_LABELS = {
    "cancelled": "已取消",
    "failed": "失败",
    "partial": "已交付 · 产物可疑",
    "pending": "进行中",
    "success": "已交付"
}

CONCLUSION_TONES = {
    "cancelled": "info",
    "failed": "error",
    "partial": "warning",
    "pending": "info",
    "success": "success"
}

PHASE_LABELS = {
    "collecting": "采集中",
    "fetching": "拉取中",
    "queued": "排队中",
    "relaying": "代传中",
    "settled": "已结束",
    "uploading": "上传中"
}

CONDITION_LABELS = {
    "artifact_missing": "未取到有效日志",
    "artifact_suspect": "产物可疑",
    "artifact_verified": "产物已校验",
    "bridge_outdated": "桥接端版本较旧",
    "delivered_unverified": "已交付未核验",
    "handoff_pending": "采集完成但未交付",
    "integrity_mismatch": "完整性不一致",
    "network_degraded": "网络质量不佳",
    "stalled": "传输已停滞"
}

ARTIFACT_QUALITY_LABELS = {
    "invalid": "无效",
    "ok": "有效",
    "suspect": "可疑",
    "unknown": "未知"
}

UPLOAD_MODE_LABELS = {
    "acli_direct": "P1 直传日志服务器",
    "bridge_relay": "P2 经平台中转",
    "unknown": "未定"
}

SIZE_UNITS = ('B', 'KiB', 'MiB', 'GiB', 'TiB')
SIZE_BASE = 1024
SIZE_ZERO_TEXT = '0 B'
SIZE_MISSING_TEXT = '—'
SIZE_DIGITS_LT_10 = 2
SIZE_DIGITS_LT_100 = 1
SIZE_DIGITS_GTE_100 = 0

THRESHOLDS = {
    "max_process_log_file_bytes": 2097152,
    "max_process_log_task_bytes": 20971520,
    "min_archive_bytes": 1048576,
    "stall_seconds": 60
}

PROGRESS_BANDS = {
    "collect_end": 60,
    "delivery_end": 90
}

MIN_ARCHIVE_BYTES = THRESHOLDS["min_archive_bytes"]
MAX_PROCESS_LOG_FILE_BYTES = THRESHOLDS["max_process_log_file_bytes"]
MAX_PROCESS_LOG_TASK_BYTES = THRESHOLDS["max_process_log_task_bytes"]
STALL_SECONDS = THRESHOLDS["stall_seconds"]
COLLECT_BAND_END = PROGRESS_BANDS["collect_end"]
DELIVERY_BAND_END = PROGRESS_BANDS["delivery_end"]


def format_size(value: object) -> str:
    """统一的体积展示口径。

    None / 非数字 -> 缺失占位；0 -> "0 B"（0 是事实，不是缺失）；
    >= 1 KiB 按 1024 进制，<10 两位、<100 一位、其余整数位。
    """
    if value is None:
        return SIZE_MISSING_TEXT
    try:
        size = float(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return SIZE_MISSING_TEXT
    if size != size or size < 0:  # NaN
        return SIZE_MISSING_TEXT
    if size < SIZE_BASE:
        return f"{round(size):d} B"
    index = 0
    scaled = size
    while scaled >= SIZE_BASE and index < len(SIZE_UNITS) - 1:
        scaled /= SIZE_BASE
        index += 1
    if scaled < 10:
        digits = SIZE_DIGITS_LT_10
    elif scaled < 100:
        digits = SIZE_DIGITS_LT_100
    else:
        digits = SIZE_DIGITS_GTE_100
    return f"{scaled:.{digits}f} {SIZE_UNITS[index]}"
