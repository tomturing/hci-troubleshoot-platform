/** 自动生成，请勿手改。源：contracts/log_collect_display.json */
export const UI_TEXT = {
  "bridge_outdated_hint": "桥接端版本较旧，暂无该数据",
  "bucket_labels": {
    "cancelled": "已取消",
    "failed": "失败",
    "partial": "已交付 · 有疑点",
    "pending": "进行中",
    "success": "已交付",
    "unfinished": "未完成 · 待补交付"
  },
  "delivery_text": {
    "cancelled": "已取消",
    "delivered": "已交付",
    "failed": "失败",
    "none": "未开始",
    "pulling": "拉取中",
    "queued": "待补交付",
    "relaying": "平台中转中",
    "uploading": "上传中"
  },
  "fallback": {
    "no_code": "平台未记录到可定位的错误码：请先看下方诊断卡与任务详情里的 error_message / create_output，记录任务号与调用链ID后提交支持；不要反复重试，避免覆盖现场。",
    "no_path": "平台不提供路径",
    "no_reason": "平台未记录失败原因（可在桥日志中按工单号检索）",
    "unknown": "—"
  },
  "labels": {
    "copy_trace": "复制调用链",
    "details": "详情",
    "error_code": "错误码",
    "failure_reason": "失败原因",
    "how_to_check": "如何检查",
    "how_to_fix": "如何处理",
    "observed_facts": "观察事实",
    "platform_conclusion": "平台结论",
    "synthetic_progress": "合成进度",
    "task_failed": "任务失败",
    "trace_id": "调用链ID"
  },
  "missing_data": {
    "bridge_outdated": "桥接端版本较旧，暂无该数据；日志本身不受影响",
    "genuinely_empty": "该环节尚无上报记录",
    "not_reached": "本次任务未进入该环节，因此没有该数据",
    "report_failed": "证据上报失败，请按调用链查看上报错误"
  },
  "mode_text": {
    "acli_direct": "P1 直传日志服务器",
    "bridge_relay": "P2 平台中转",
    "none": "未定",
    "platform_relay": "P2 平台中转",
    "unknown": "未定"
  },
  "render_policy": {
    "admin_views": [
      "list",
      "detail",
      "attribution",
      "evidence"
    ],
    "bridge_outdated_max_per_view": 1,
    "conditions_skip_stages_before": "S4",
    "conditions_skip_when_not_collected": [
      "artifact_missing"
    ],
    "customer_views": [
      "form",
      "result",
      "message"
    ],
    "error_code_authoritative": true,
    "error_code_max_per_view": 1,
    "error_message_max_per_view": 1
  },
  "status_text": {
    "cancelled": "已取消",
    "collecting": "采集中",
    "completed": "已完成",
    "creating": "任务下发中",
    "failed": "失败",
    "installing": "工具安装中",
    "packaging": "打包中",
    "pending": "待下发",
    "probing": "探测中",
    "relaying": "平台中转中",
    "uploading": "上传中"
  }
} as const

export const STATUS_TEXT = {
  "cancelled": "已取消",
  "collecting": "采集中",
  "completed": "已完成",
  "creating": "任务下发中",
  "failed": "失败",
  "installing": "工具安装中",
  "packaging": "打包中",
  "pending": "待下发",
  "probing": "探测中",
  "relaying": "平台中转中",
  "uploading": "上传中"
} as const
export const DELIVERY_TEXT = {
  "cancelled": "已取消",
  "delivered": "已交付",
  "failed": "失败",
  "none": "未开始",
  "pulling": "拉取中",
  "queued": "待补交付",
  "relaying": "平台中转中",
  "uploading": "上传中"
} as const
export const MODE_TEXT = {
  "acli_direct": "P1 直传日志服务器",
  "bridge_relay": "P2 平台中转",
  "none": "未定",
  "platform_relay": "P2 平台中转",
  "unknown": "未定"
} as const
export const BUCKET_LABELS = {
  "cancelled": "已取消",
  "failed": "失败",
  "partial": "已交付 · 有疑点",
  "pending": "进行中",
  "success": "已交付",
  "unfinished": "未完成 · 待补交付"
} as const
export const MISSING_DATA_TEXT = {
  "bridge_outdated": "桥接端版本较旧，暂无该数据；日志本身不受影响",
  "genuinely_empty": "该环节尚无上报记录",
  "not_reached": "本次任务未进入该环节，因此没有该数据",
  "report_failed": "证据上报失败，请按调用链查看上报错误"
} as const
export const UI_FALLBACK = {
  "no_code": "平台未记录到可定位的错误码：请先看下方诊断卡与任务详情里的 error_message / create_output，记录任务号与调用链ID后提交支持；不要反复重试，避免覆盖现场。",
  "no_path": "平台不提供路径",
  "no_reason": "平台未记录失败原因（可在桥日志中按工单号检索）",
  "unknown": "—"
} as const
export const RENDER_POLICY = {
  "admin_views": [
    "list",
    "detail",
    "attribution",
    "evidence"
  ],
  "bridge_outdated_max_per_view": 1,
  "conditions_skip_stages_before": "S4",
  "conditions_skip_when_not_collected": [
    "artifact_missing"
  ],
  "customer_views": [
    "form",
    "result",
    "message"
  ],
  "error_code_authoritative": true,
  "error_code_max_per_view": 1,
  "error_message_max_per_view": 1
} as const
export const OBSERVABILITY_CONTRACT = {
  "event_names": [
    "log_collect.create",
    "log_collect.spc_fanout_failed",
    "log_collect.progress",
    "log_collect.network_probe_failed",
    "log_collect.artifact_manifest_failed",
    "log_collect.node_manifest_failed",
    "log_collect.process_log_failed",
    "log_collect.evidence_tls_relaxed",
    "log_collect.attribution_derived",
    "delivery.session_rebuilt"
  ],
  "forbidden_words": [
    "rm -",
    "rmdir ",
    "shutil.rmtree",
    "del /",
    "rm "
  ],
  "log_fields": [
    "trace_id",
    "task_id",
    "case_id",
    "code",
    "layer",
    "component",
    "stage",
    "outcome",
    "source",
    "duration_ms",
    "contract_version"
  ],
  "metrics": [
    "log_collect_task_total",
    "log_collect_error_total",
    "log_collect_attribution_total",
    "log_collect_display_derive_seconds",
    "log_collect_evidence_report_total",
    "log_collect_evidence_tls_relaxed_total",
    "log_collect_trace_fallback_total",
    "log_collect_error_view_total"
  ],
  "trace": {
    "db_column": "trace_id",
    "header": "X-Trace-ID",
    "legacy_header": "X-Request-ID",
    "priority": [
      "X-Trace-ID",
      "traceparent",
      "context",
      "generated"
    ],
    "required_in_tables": [
      "log_collect_task",
      "bridge_execution_logs",
      "log_collect_artifact_manifest",
      "log_collect_network_probe",
      "log_collect_transfer_metric",
      "log_collect_process_log"
    ],
    "upstream_5888_header": "X-Request-ID"
  }
} as const

