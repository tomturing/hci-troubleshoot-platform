/**
 * 自动生成，请勿手改。
 *
 * 源：contracts/log_collect_display.json
 * 生成：python3 scripts/gen_log_collect_display.py
 */

export const CONCLUSION_LABELS = {
  "cancelled": "已取消",
  "failed": "失败",
  "partial": "已交付 · 有疑点",
  "pending": "进行中",
  "success": "已交付"
} as const

export const CONCLUSION_TONES = {
  "cancelled": "info",
  "failed": "error",
  "partial": "warning",
  "pending": "info",
  "success": "success"
} as const

export const PHASE_LABELS = {
  "collecting": "采集中",
  "fetching": "拉取中",
  "queued": "排队中",
  "relaying": "代传中",
  "settled": "已结束",
  "uploading": "上传中"
} as const

export const CONDITION_LABELS = {
  "artifact_missing": "未取到有效日志",
  "artifact_suspect": "产物可疑",
  "artifact_uninspected": "包内容未探测",
  "artifact_verified": "产物已校验",
  "bridge_outdated": "桥接端版本较旧",
  "delivered_unverified": "已交付未核验",
  "handoff_pending": "采集完成但未交付",
  "integrity_mismatch": "完整性不一致",
  "network_degraded": "网络质量不佳",
  "stalled": "传输已停滞"
} as const

export const ARTIFACT_QUALITY_LABELS = {
  "invalid": "无效",
  "ok": "有效",
  "suspect": "可疑",
  "unknown": "未知"
} as const

export const ARTIFACT_REASON_LABELS = {
  "empty_archive": "压缩包内没有条目",
  "entry_count_low": "条目数量明显低于预期",
  "metadata_only": "仅含配置元数据，没有日志内容",
  "missing": "未取到压缩包",
  "not_inspected": "尚未探测包内容",
  "ok": "内容校验通过",
  "too_small": "压缩包体积异常偏小",
  "unreadable": "包内容无法读取"
} as const

export const NETWORK_GRADE_LABELS = {
  "fair": "一般",
  "good": "良好",
  "poor": "较差",
  "unknown": "未知"
} as const

export const TRANSFER_STAGE_LABELS = {
  "collect": "采集",
  "fetch": "拉取",
  "relay": "代传",
  "upload": "上传"
} as const

export const UPLOAD_MODE_LABELS = {
  "acli_direct": "P1 直传日志服务器",
  "bridge_relay": "P2 经平台中转",
  "unknown": "未定"
} as const

export type Conclusion = keyof typeof CONCLUSION_LABELS
export type Phase = keyof typeof PHASE_LABELS
export type Condition = keyof typeof CONDITION_LABELS
export type ArtifactQuality = keyof typeof ARTIFACT_QUALITY_LABELS
export type ArtifactReason = keyof typeof ARTIFACT_REASON_LABELS
export type NetworkGrade = keyof typeof NETWORK_GRADE_LABELS
export type TransferStage = keyof typeof TRANSFER_STAGE_LABELS

export const SIZE_UNITS = ["B", "KiB", "MiB", "GiB", "TiB"] as const
export const SIZE_BASE = 1024
export const SIZE_ZERO_TEXT = "0 B"
export const SIZE_MISSING_TEXT = "—"
export const SIZE_DIGITS_LT_10 = 2
export const SIZE_DIGITS_LT_100 = 1
export const SIZE_DIGITS_GTE_100 = 0

export const THRESHOLDS = {
  "max_process_log_file_bytes": 2097152,
  "max_process_log_task_bytes": 20971520,
  "min_archive_bytes": 1048576,
  "stall_seconds": 60
} as const

export const PROGRESS_BANDS = {
  "collect_end": 60,
  "delivery_end": 90
} as const

export const MIN_ARCHIVE_BYTES = 1048576
export const MAX_PROCESS_LOG_FILE_BYTES = 2097152
export const MAX_PROCESS_LOG_TASK_BYTES = 20971520
export const STALL_SECONDS = 60
export const COLLECT_BAND_END = 60
export const DELIVERY_BAND_END = 90

/** 统一的体积展示口径（与后端 format_size 逐字对齐）。 */
export function formatLogCollectSize(value: number | null | undefined): string {
  if (value === null || value === undefined) return SIZE_MISSING_TEXT
  const size = Number(value)
  if (!Number.isFinite(size) || size < 0) return SIZE_MISSING_TEXT
  if (size < SIZE_BASE) return `${Math.round(size)} B`
  let index = 0
  let scaled = size
  while (scaled >= SIZE_BASE && index < SIZE_UNITS.length - 1) {
    scaled /= SIZE_BASE
    index += 1
  }
  const digits = scaled < 10 ? SIZE_DIGITS_LT_10 : scaled < 100 ? SIZE_DIGITS_LT_100 : SIZE_DIGITS_GTE_100
  return `${scaled.toFixed(digits)} ${SIZE_UNITS[index]}`
}
