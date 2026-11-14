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
  "success": "已交付",
  "unfinished": "未完成 · 待补交付"
} as const

export const CONCLUSION_TONES = {
  "cancelled": "info",
  "failed": "error",
  "partial": "warning",
  "pending": "info",
  "success": "success",
  "unfinished": "warning"
} as const

export const PHASE_LABELS = {
  "collecting": "采集中",
  "fetching": "拉取中",
  "queued": "排队中",
  "relaying": "代传中",
  "settled": "已结束",
  "uploading": "上传中",
  "verifying": "核验中"
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
  "stalled": "传输已停滞",
  "verifying_archive": "正在核验归档件"
} as const

export const ARTIFACT_QUALITY_LABELS = {
  "invalid": "无效",
  "ok": "有效",
  "suspect": "可疑",
  "unknown": "未知"
} as const

export const ARTIFACT_REASON_LABELS = {
  "content_too_small": "包内日志内容偏少",
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
  "fetching": "拉取",
  "relaying": "代传",
  "uploading": "上传"
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
  "archive_verify_wait_minutes": 10,
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

export const SCOPE_LABELS = {
  "cloud": "云端",
  "intranet": "内网",
  "local": "本地"
} as const

export const COMPONENTS = {
  "acli": {
    "duty": "版本、下发、采集进度、压缩、直传动作",
    "label": "acli工具",
    "scope": "intranet",
    "tag": "[内网·acli工具]"
  },
  "bridge": {
    "duty": "协议解析、任务下发、证据上报、P2 中继",
    "label": "terminal_bridge",
    "scope": "local",
    "tag": "[本地·terminal_bridge]"
  },
  "hci": {
    "duty": "主机、SSH/cgroup、磁盘、产物落盘路径",
    "label": "HCI环境",
    "scope": "intranet",
    "tag": "[内网·HCI环境]"
  },
  "htp": {
    "duty": "任务状态、归档回查、结论派生、界面",
    "label": "htp平台",
    "scope": "cloud",
    "tag": "[云端·htp平台]"
  },
  "log_server": {
    "duty": "收包、归档命名、票据校验",
    "label": "日志服务器5888",
    "scope": "cloud",
    "tag": "[云端·日志服务器5888]"
  }
} as const

export const COMPONENT_ORDER = ["hci", "acli", "bridge", "htp", "log_server"] as const

export const STAGES = {
  "S1": {
    "components": [
      "hci"
    ],
    "criteria": "probe.hosts_count / available_mb / architecture",
    "label": "环境探测",
    "scope_label": "内网",
    "scopes": [
      "intranet"
    ]
  },
  "S2": {
    "components": [
      "acli"
    ],
    "criteria": "acli_sync.install_done / acli_build_time",
    "label": "工具就绪",
    "scope_label": "内网",
    "scopes": [
      "intranet"
    ]
  },
  "S3": {
    "components": [
      "bridge"
    ],
    "criteria": "create_ack=confirmed、exit_code=0",
    "label": "任务下发",
    "scope_label": "本地",
    "scopes": [
      "local"
    ]
  },
  "S4": {
    "components": [
      "hci",
      "acli"
    ],
    "criteria": "percent/done/total、archive_path/size",
    "label": "采集打包",
    "scope_label": "内网",
    "scopes": [
      "intranet"
    ]
  },
  "S5": {
    "components": [
      "acli",
      "bridge",
      "htp",
      "log_server"
    ],
    "criteria": "5888 UploadComplete / relay 分片",
    "label": "交付传输",
    "note": "P1：acli→5888；P2：bridge→htp→5888",
    "scope_label": "内网 → 云端",
    "scopes": [
      "intranet",
      "cloud"
    ]
  },
  "S6": {
    "components": [
      "htp",
      "log_server"
    ],
    "criteria": "p1_archive_check.verified、/archive_status",
    "label": "归档核验",
    "scope_label": "云端",
    "scopes": [
      "cloud"
    ]
  },
  "S7": {
    "components": [
      "bridge",
      "htp"
    ],
    "criteria": "4443 /api/direct/log-collect 四类端点、四张证据表",
    "label": "证据回传",
    "scope_label": "本地 → 云端",
    "scopes": [
      "local",
      "cloud"
    ]
  },
  "S8": {
    "components": [
      "htp"
    ],
    "criteria": "derive() 输出、横幅/抽屉",
    "label": "结论呈现",
    "scope_label": "云端",
    "scopes": [
      "cloud"
    ]
  }
} as const

export const STAGE_STATUS_LABELS = {
  "failed": "失败",
  "running": "进行中",
  "skipped": "未启用",
  "success": "成功",
  "unknown": "未知",
  "waiting": "等待中"
} as const

export const FAULT_CATALOG = {
  "LC-5888-001": {
    "action": "请联系管理员核对日志服务器鉴权配置",
    "admin_detail": "5888 upload_server.log 出现「鉴权失败: /init_upload」且无 [P1] 票据通过；当前阶段一 relaxed 匿名放行，切换强鉴权前必须确认 [P1] 票据通过。",
    "check": [],
    "component": "log_server",
    "evidence": "upload_server.log 鉴权失败 / [P1] 票据通过",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "上传票据校验失败(401)",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S5",
    "title_zh": "上传票据校验失败(401)",
    "tone": "error"
  },
  "LC-5888-002": {
    "action": "请检查节点到5888的网络，或改用P2平台中转",
    "admin_detail": "probe.log_server_reachable=false 或 network_probe target=log_server reachable=false；注意 P2 代传落点仍是同一个 5888。",
    "check": [],
    "component": "log_server",
    "evidence": "detail.probe.log_server_reachable、log_collect_network_probe",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "日志服务器不可达",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S5",
    "title_zh": "日志服务器不可达",
    "tone": "error"
  },
  "LC-5888-003": {
    "action": "请联系管理员按上传ID核对，不要直接覆盖归档件",
    "admin_detail": "verify-archive reason=size_mismatch / stale_archive；同工单目录可能留有旧包，规范名=最新包、旧包带时间戳。",
    "check": [],
    "component": "log_server",
    "evidence": "p1_archive_check.expected_size / archived_size / stored_name",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "归档件大小或时间不一致",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S6",
    "title_zh": "归档件大小或时间不一致",
    "tone": "warning"
  },
  "LC-5888-004": {
    "action": "请截图本页并联系管理员核对日志服务器，不要重复收集",
    "admin_detail": "p1_archive_check.reason=archive_missing 且 checked_at 已超过 archive_verify_wait_minutes；先查 5888 upload_server.log 的 UploadComplete 与 /sf/log 规范归档名，再决定重传还是补归档，禁止直接重新采集。",
    "check": [],
    "component": "log_server",
    "evidence": "detail.p1_archive_check.checked_at / reason、5888 upload_server.log",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "等待10分钟后日志服务器仍未发布归档件",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S6",
    "title_zh": "等待10分钟后日志服务器仍未发布归档件",
    "tone": "error"
  },
  "LC-ACLI-001": {
    "action": "请在HCI后台执行acli --version确认acli版本",
    "admin_detail": "acli 1.0.0 不同 build 的子命令集合不同；比对 detail.probe.acli_build_time 与 log_collect.create.command / output_preview，确认下发命令是否被该 build 支持。",
    "check": [],
    "component": "acli",
    "evidence": "log_collect.create.output_preview、detail.probe.acli_build_time",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "未知的命令或者命名空间(log collect create)",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "未知的命令或者命名空间(log collect create)",
    "tone": "error"
  },
  "LC-ACLI-002": {
    "action": "请联系管理员升级acli后重试，不要重复下发",
    "admin_detail": "看 acli_sync.version_outdated / install_done；旧 build 可能不支持 -m other 或 -H 票据头。",
    "check": [],
    "component": "acli",
    "evidence": "acli_sync.version_check.current_build / remote_build",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "acli版本过低或缺少日志收集插件",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "acli版本过低或缺少日志收集插件",
    "tone": "error"
  },
  "LC-ACLI-003": {
    "action": "请联系管理员更新桥接端解析规则，暂不要重复下发",
    "admin_detail": "log_collect.progress.parse_ok=false；中英文输出差异指纹为 catalog_source=platform_default + parse_ok=false。",
    "check": [],
    "component": "acli",
    "evidence": "parse_ok、status_text、catalog_source",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "采集进度无法解析",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "采集进度无法解析",
    "tone": "warning"
  },
  "LC-ACLI-004": {
    "action": "请核对采集时间范围与场景后重新收集",
    "admin_detail": "archive_size < min_archive_bytes 或 manifest 仅含配置条目；608 字节空壳包先例说明 sha256 一致不等于内容有效。",
    "check": [],
    "component": "acli",
    "evidence": "archive_size、artifact_manifest.entry_count / entry_total_bytes",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "压缩包体积异常偏小",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "压缩包体积异常偏小",
    "tone": "error"
  },
  "LC-BRIDGE-001": {
    "action": "请确认本地桥接端已启动，刷新页面后重试",
    "admin_detail": "本地 WS 未监听或 exe 过旧；查 bridge_startup.service.version 与 capabilities.log_collect_evidence。",
    "check": [],
    "component": "bridge",
    "evidence": "openSocket 错误、BRIDGE_CHANNEL_LOST",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "无法连接terminal_bridge",
    "retryable": "manual",
    "scope": "local",
    "stage": "S3",
    "title_zh": "无法连接terminal_bridge",
    "tone": "error"
  },
  "LC-BRIDGE-002": {
    "action": "日志已交付，请联系管理员更新桥接端后补传证据",
    "admin_detail": "4443 自签 CA 不在客户机系统信任库；create 的 tls_insecure 依赖浏览器会话显式授权，P1 场景永不触发，证据通道需遇 x509 自动放宽一次并留痕（只放宽证书校验，不动票据鉴权）。",
    "check": [],
    "component": "bridge",
    "evidence": "log_collect.network_probe_failed.error、log_collect.node_manifest_failed.error",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "证据上报失败：平台证书不受信",
    "retryable": "manual",
    "scope": "local",
    "stage": "S7",
    "title_zh": "证据上报失败：平台证书不受信",
    "tone": "warning"
  },
  "LC-BRIDGE-003": {
    "action": "请联系管理员更新桥接端exe，日志本身不受影响",
    "admin_detail": "capabilities.log_collect_evidence 缺失；四张证据表 0 行是能力缺失，不是采集失败，界面禁止显示 0 或空白。",
    "check": [],
    "component": "bridge",
    "evidence": "log_collect.capabilities、log_collect.evidence_enabled",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "桥接端版本较旧，暂无该数据",
    "retryable": "manual",
    "scope": "local",
    "stage": "S7",
    "title_zh": "桥接端版本较旧，暂无该数据",
    "tone": "warning"
  },
  "LC-BRIDGE-004": {
    "action": "请点「上传到平台」重试，不需要重新采集",
    "admin_detail": "看 relay_status / relay_last_error；分片已落平台可续传，failed 后需人工点一次。",
    "check": [],
    "component": "bridge",
    "evidence": "delivery.job_finished、relay_last_error",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "P2平台中转失败",
    "retryable": "manual",
    "scope": "local",
    "stage": "S5",
    "title_zh": "P2平台中转失败",
    "tone": "error"
  },
  "LC-BRIDGE-005": {
    "action": "日志包已到日志服务器，请联系管理员补传证据，不要重新收集",
    "admin_detail": "artifact_manifest 缺行使 quality=unknown，结论只能是 partial「已交付 · 有疑点」；引入方是 S7 证据回传（x509 或旧桥），不是采集失败，也不是平台损坏。先查四张证据表与 *_failed 事件，别让用户重采。",
    "check": [],
    "component": "bridge",
    "evidence": "artifact.quality=unknown、log_collect_artifact_manifest 行数、log_collect.node_manifest_failed.error",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "已交付但包内容清单未回传",
    "retryable": "manual",
    "scope": "local",
    "stage": "S7",
    "title_zh": "已交付但包内容清单未回传",
    "tone": "warning"
  },
  "LC-BRIDGE-006": {
    "action": "请查看下方诊断并截图联系管理员，不要重复下发",
    "admin_detail": "create 阶段 create_ack 非 confirmed 或 exit_code 非 0；比对 detail.create_output 与 probe.acli_build_time，重复下发会在节点上叠加多个采集任务。",
    "check": [],
    "component": "bridge",
    "evidence": "log_collect.create.output_preview、create_ack、exit_code",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "任务下发失败",
    "retryable": "manual",
    "scope": "local",
    "stage": "S3",
    "title_zh": "任务下发失败",
    "tone": "error"
  },
  "LC-ENV-001": {
    "action": "当前 HCI 节点架构不在支持范围（仅 x86_64 / aarch64），请确认节点类型。",
    "admin_detail": "检查 uname -m；若为客户定制架构需产品侧适配。",
    "business_stage": "probe",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "当前 HCI 节点架构不在支持范围（仅 x86_64 / aarch64），请确认节点类型。"
    ],
    "http_status": 422,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "不支持的硬件架构"
    ],
    "owner": "customer",
    "problem": "不支持的硬件架构",
    "retryable": false,
    "scope": "intranet",
    "stage": "S1",
    "title_zh": "不支持的硬件架构",
    "tone": "error"
  },
  "LC-ENV-002": {
    "action": "无法读取节点架构，请确认 SSH 连接正常后重试。",
    "admin_detail": "检查 uname -m 退出码与 SSH 通道；偶发超时可直接重试。",
    "business_stage": "probe",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "无法读取节点架构，请确认 SSH 连接正常后重试。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "获取系统架构失败"
    ],
    "owner": "customer",
    "problem": "获取系统架构失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S1",
    "title_zh": "获取系统架构失败",
    "tone": "error"
  },
  "LC-ENV-003": {
    "action": "目标节点未安装 acli，请先执行安装。",
    "admin_detail": "acli --version 非零或无输出；引导走安装流程（acli_sync）。",
    "business_stage": "probe",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "目标节点未安装 acli，请先执行安装。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "acli 未安装"
    ],
    "owner": "customer",
    "problem": "acli 未安装",
    "retryable": true,
    "scope": "intranet",
    "stage": "S1",
    "title_zh": "acli 未安装",
    "tone": "error"
  },
  "LC-ENV-004": {
    "action": "/sf/data/local 空间不足（采集要求每节点 ≥6GB），请清理后重试。",
    "admin_detail": "df -P -m /sf/data/local；安装阈值默认 100MB，采集阈值 6GB。",
    "business_stage": "probe",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "/sf/data/local 空间不足（采集要求每节点 ≥6GB），请清理后重试。"
    ],
    "http_status": 507,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "磁盘空间不足"
    ],
    "owner": "customer",
    "problem": "磁盘空间不足",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S1",
    "title_zh": "磁盘空间不足",
    "tone": "error"
  },
  "LC-ENV-005": {
    "action": "当前环境无法直连日志服务器，将改用中转上传或仅保留本地包。",
    "admin_detail": "curl 探测 http_code；2xx/3xx/4xx 视为可达，5xx 或连接失败为不可达。",
    "business_stage": "probe",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "当前环境无法直连日志服务器，将改用中转上传或仅保留本地包。"
    ],
    "http_status": 503,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "日志服务器不可达"
    ],
    "owner": "customer",
    "problem": "日志服务器不可达",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S1",
    "title_zh": "日志服务器不可达",
    "tone": "error"
  },
  "LC-ENV-006": {
    "action": "SSH 连接已断开，请重新连接后重试。",
    "admin_detail": "bridge 未找到 case/node 对应会话；先 ssh_connect。",
    "business_stage": "exec",
    "check": [
      "确认页面底部 SSH 连接状态。",
      "确认节点未被重启或会话未被回收。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "重新连接 SSH 后重试。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "SSH 会话不存在"
    ],
    "owner": "platform",
    "problem": "SSH 会话不存在",
    "retryable": true,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "SSH 会话不存在",
    "tone": "warning"
  },
  "LC-ENV-007": {
    "action": "SSH 认证失败，请检查账号或密钥。",
    "admin_detail": "区分密码错误、密钥不匹配、账号锁定。",
    "business_stage": "exec",
    "check": [
      "核对登录账号与密钥。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "重新登录/配置免密后重试。"
    ],
    "http_status": 401,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "SSH 认证失败"
    ],
    "owner": "customer",
    "problem": "SSH 认证失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "SSH 认证失败",
    "tone": "error"
  },
  "LC-ENV-008": {
    "action": "部分目标节点离线，已跳过；如需完整采集请恢复节点后重试。",
    "admin_detail": "插件进度会把离线节点标记为 failed/host offline。",
    "business_stage": "collect",
    "check": [
      "确认节点电源与网络。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（ENV 层原始输出）",
    "fix": [
      "节点恢复在线后重试。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "ENV",
    "match_keywords": [
      "目标节点离线"
    ],
    "owner": "customer",
    "problem": "目标节点离线",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "节点离线",
    "tone": "error"
  },
  "LC-EXEC-001": {
    "action": "目标节点不允许分配终端，命令无法执行（该命令必须使用 PTY）。",
    "admin_detail": "sshd 拒绝 RequestPty；不得降级为无 PTY（会导致退出码 0 的静默失败）。",
    "business_stage": "exec",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（EXEC 层原始输出）",
    "fix": [
      "目标节点不允许分配终端，命令无法执行（该命令必须使用 PTY）。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "EXEC",
    "match_keywords": [
      "申请伪终端失败"
    ],
    "owner": "customer",
    "problem": "申请伪终端失败",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "申请伪终端失败",
    "tone": "error"
  },
  "LC-EXEC-002": {
    "action": "命令执行超时，请检查节点负载或缩小采集范围。",
    "admin_detail": "区分 create（下行）与 progress（查询）的超时预算。",
    "business_stage": "exec",
    "check": [
      "确认节点负载、磁盘与 SSH 通道是否正常。",
      "任务详情里有对应阶段的输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（EXEC 层原始输出）",
    "fix": [
      "恢复后重试；连续超时按节点异常处理。"
    ],
    "http_status": 504,
    "kind": "runtime",
    "layer": "EXEC",
    "match_keywords": [
      "命令执行超时"
    ],
    "owner": "customer",
    "problem": "命令执行超时",
    "retryable": true,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "命令执行超时",
    "tone": "error"
  },
  "LC-EXEC-003": {
    "action": "命令执行失败，请结合错误详情排查。",
    "admin_detail": "保留 exit_code 与 stderr/stdout 摘要；上层按 plugin 语义再分类。",
    "business_stage": "exec",
    "check": [
      "查看任务详情 create_output 中的节点原始输出。",
      "确认输出是否为“专项不存在”“参数非法”等明确原因。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（EXEC 层原始输出）",
    "fix": [
      "按原始输出修正配置；这是节点侧拒绝，不是平台通道故障。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "EXEC",
    "match_keywords": [
      "命令返回非零退出码"
    ],
    "owner": "product",
    "problem": "命令返回非零退出码",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "命令返回非零",
    "tone": "error"
  },
  "LC-EXEC-004": {
    "action": "命令启动失败，请重试。",
    "admin_detail": "session.Start 失败，多为会话已关闭或同时打开过多 session。",
    "business_stage": "exec",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（EXEC 层原始输出）",
    "fix": [
      "命令启动失败，请重试。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "EXEC",
    "match_keywords": [
      "启动命令失败"
    ],
    "owner": "customer",
    "problem": "启动命令失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "启动命令失败",
    "tone": "error"
  },
  "LC-EXEC-005": {
    "action": "连接中断，未能取回执行结果。",
    "admin_detail": "session.Wait 异常；检查网络抖动与节点负载。",
    "business_stage": "exec",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（EXEC 层原始输出）",
    "fix": [
      "连接中断，未能取回执行结果。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "EXEC",
    "match_keywords": [
      "SSH 等待结果失败"
    ],
    "owner": "customer",
    "problem": "SSH 等待结果失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "SSH 等待结果失败",
    "tone": "error"
  },
  "LC-HCI-001": {
    "action": "请尝试其他终端连接HCI验证连接是否正常",
    "admin_detail": "检查 bridge_execution_logs 的 ssh.connected / ssh.disconnected(reason) / conn.role，并核对节点 22 端口与凭据；内网主机 SSH 端口可能是 22468。",
    "check": [],
    "component": "hci",
    "evidence": "ssh.disconnected.reason、ssh.output.output_preview",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "SSH连接失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S1",
    "title_zh": "SSH连接失败",
    "tone": "error"
  },
  "LC-HCI-002": {
    "action": "请联系管理员确认/sf可用空间，不要自行删除文件",
    "admin_detail": "用 probe.available_mb 与 archive_size 比对；磁盘压力会触发 Pod 驱逐并造成全栈中断，禁止在界面给出删除命令。",
    "check": [],
    "component": "hci",
    "evidence": "log_collect.probe.available_mb、archive_size_probe.diag",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "HCI磁盘空间不足",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "HCI磁盘空间不足",
    "tone": "error"
  },
  "LC-HCI-003": {
    "action": "请重新连接SSH；已完成的日志包无需重新收集",
    "admin_detail": "TMOUT auto-logout 后 session.shell_respawned 自动重生；核对断连时间是否晚于 5888 UploadComplete，晚于则不影响交付。",
    "check": [],
    "component": "hci",
    "evidence": "ssh.output.output_preview、session.shell_respawned.respawns",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "HCI会话超时断开",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "HCI会话超时断开",
    "tone": "warning"
  },
  "LC-HTP-001": {
    "action": "请等待最多10分钟或点「重新核验归档」，不要重新收集",
    "admin_detail": "P1 UploadComplete 与 5888 发布规范归档名之间存在竞态（现场实测约 31 秒）；verify-archive 返回 archive_missing 只写快照不写终态，等待窗口内结论应为 pending。",
    "check": [],
    "component": "htp",
    "evidence": "detail.p1_archive_check.reason=archive_missing / checked_at、case-service P1 归档核验日志",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "归档核验未完成，正在等待日志服务器发布归档名",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S6",
    "title_zh": "归档核验未完成，正在等待日志服务器发布归档名",
    "tone": "info"
  },
  "LC-HTP-002": {
    "action": "请打开下方诊断卡查看具体原因，并附上调用链ID反馈；不要反复重试",
    "admin_detail": "error_code 与 error_message 已有值但未命中契约关键词；请按 trace_id 串 bridge_execution_logs 与四张证据表定位，禁止据此推断为平台无数据。",
    "check": [],
    "component": "htp",
    "evidence": "trace_id、detail.write_audit",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "本次失败未能自动归因",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "本次失败未能自动归因",
    "tone": "warning"
  },
  "LC-HTP-003": {
    "action": "请刷新页面后重试；若持续失败请截图本页并联系管理员",
    "admin_detail": "客户端 GET /log-collect/tasks 或 /progress 失败；按 trace_id 查 case-service 与网关日志，重点排除 502/503/504「结果未知」，不要把一次轮询失败写成任务终态。",
    "check": [],
    "component": "htp",
    "evidence": "case-service 访问日志、网关状态码、trace_id",
    "fix": [],
    "http_status": 200,
    "kind": "signal",
    "layer": "ATTRIBUTION",
    "match_keywords": [],
    "owner": "platform",
    "problem": "任务状态或进度查询失败",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "任务状态或进度查询失败",
    "tone": "error"
  },
  "LC-INSTALL-001": {
    "action": "安装包下载请求构造失败，请重试或使用离线安装包。",
    "admin_detail": "检查下载 URL 配置与客户端构造逻辑。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "安装包下载请求构造失败，请重试或使用离线安装包。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "创建下载请求失败"
    ],
    "owner": "platform",
    "problem": "创建下载请求失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "创建下载请求失败",
    "tone": "warning"
  },
  "LC-INSTALL-002": {
    "action": "无法从官方源下载 acli 安装包，请检查客户侧网络或使用离线包。",
    "admin_detail": "检查 DNS/代理/出口策略；官方源 1110 端口。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "无法从官方源下载 acli 安装包，请检查客户侧网络或使用离线包。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "下载安装包失败"
    ],
    "owner": "customer",
    "problem": "下载安装包失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "下载安装包失败",
    "tone": "error"
  },
  "LC-INSTALL-003": {
    "action": "安装包源返回异常状态，请稍后重试。",
    "admin_detail": "记录 HTTP 状态码与 architecture 参数。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "安装包源返回异常状态，请稍后重试。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "下载源返回异常状态码"
    ],
    "owner": "platform",
    "problem": "下载源返回异常状态码",
    "retryable": true,
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "下载源返回异常状态码",
    "tone": "warning"
  },
  "LC-INSTALL-004": {
    "action": "安装包传输到节点失败，请检查 SSH 通道稳定性。",
    "admin_detail": "uploadStream 走 SSH stdin，中断多为网络或磁盘写失败。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "安装包传输到节点失败，请检查 SSH 通道稳定性。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "安装包上传到 HCI 失败"
    ],
    "owner": "customer",
    "problem": "安装包上传到 HCI 失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "安装包上传到 HCI 失败",
    "tone": "error"
  },
  "LC-INSTALL-005": {
    "action": "acli 安装器执行失败，请查看节点安装日志。",
    "admin_detail": "安装器会集群分发，失败常见于节点不可达或版本不兼容。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "acli 安装器执行失败，请查看节点安装日志。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "acli 安装命令失败"
    ],
    "owner": "customer",
    "problem": "acli 安装命令失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "acli 安装命令失败",
    "tone": "error"
  },
  "LC-INSTALL-006": {
    "action": "acli 安装完成但无法执行，请联系技术支持。",
    "admin_detail": "检查 /sf/sbin/acli wrapper 与容器状态。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "acli 安装完成但无法执行，请联系技术支持。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "安装后版本验证失败"
    ],
    "owner": "product",
    "problem": "安装后版本验证失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "安装后版本验证失败",
    "tone": "error"
  },
  "LC-INSTALL-007": {
    "action": "上传到节点的安装包与本地校验值不一致（残包已自动丢弃），可点击重试重新上传。",
    "admin_detail": "比对远端 size 与 sha256/md5；不一致常见于链路截断、同名覆盖或历史残留半包。",
    "business_stage": "verify",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "上传到节点的安装包与本地校验值不一致（残包已自动丢弃），可点击重试重新上传。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "安装包落盘校验失败"
    ],
    "owner": "customer",
    "problem": "安装包落盘校验失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S6",
    "title_zh": "安装包落盘校验失败",
    "tone": "error"
  },
  "LC-INSTALL-008": {
    "action": "下载到的安装包字节数与服务端声明不一致，请重试。",
    "admin_detail": "比对 Content-Length 与实收字节数；差异常见于代理截断或并发中断。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "下载到的安装包字节数与服务端声明不一致，请重试。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "安装包下载不完整"
    ],
    "owner": "customer",
    "problem": "安装包下载不完整",
    "retryable": true,
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "安装包下载不完整",
    "tone": "error"
  },
  "LC-INSTALL-009": {
    "action": "上一次上传仍在进行，请稍候再试（重复点击会被拒绝，避免并发写坏同名安装包）。",
    "admin_detail": "同一 caseID@nodeIP 单飞互斥，见 acli_sync.busy 日志。",
    "business_stage": "install",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（INSTALL 层原始输出）",
    "fix": [
      "上一次上传仍在进行，请稍候再试（重复点击会被拒绝，避免并发写坏同名安装包）。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "INSTALL",
    "match_keywords": [
      "安装包传输进行中"
    ],
    "owner": "platform",
    "problem": "安装包传输进行中",
    "retryable": true,
    "scope": "intranet",
    "stage": "S2",
    "title_zh": "安装包传输进行中",
    "tone": "warning"
  },
  "LC-PLATFORM-001": {
    "action": "登录状态失效，请重新登录。",
    "admin_detail": "网关签名 X-Client-ID 缺失或无效。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "登录状态失效，请重新登录。"
    ],
    "http_status": 401,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "身份校验失败"
    ],
    "owner": "platform",
    "problem": "身份校验失败",
    "retryable": true,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "身份校验失败",
    "tone": "warning"
  },
  "LC-PLATFORM-002": {
    "action": "未找到该工单。",
    "admin_detail": "BOLA 防护：不存在与非归属统一 404，避免泄露存在性。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "未找到该工单。"
    ],
    "http_status": 404,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "工单归属校验失败"
    ],
    "owner": "platform",
    "problem": "工单归属校验失败",
    "retryable": false,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "工单归属校验失败",
    "tone": "warning"
  },
  "LC-PLATFORM-003": {
    "action": "任务状态已变化，请刷新后重试。",
    "admin_detail": "终态不可复活；阶段不可倒退。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "任务状态已变化，请刷新后重试。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "非法状态转换"
    ],
    "owner": "platform",
    "problem": "非法状态转换",
    "retryable": false,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "非法状态转换",
    "tone": "warning"
  },
  "LC-PLATFORM-004": {
    "action": "进度数据异常，请刷新。",
    "admin_detail": "回退通常意味着轮询路由到了错误节点。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "进度数据异常，请刷新。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "进度回退"
    ],
    "owner": "platform",
    "problem": "进度回退",
    "retryable": false,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "进度回退",
    "tone": "warning"
  },
  "LC-PLATFORM-005": {
    "action": "该工单已有进行中的任务。",
    "admin_detail": "数据库部分唯一索引兜底并发窗口。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "该工单已有进行中的任务。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "重复活跃任务"
    ],
    "owner": "platform",
    "problem": "重复活跃任务",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "重复活跃任务",
    "tone": "warning"
  },
  "LC-PLATFORM-006": {
    "action": "服务暂时不可用，请稍后重试。",
    "admin_detail": "连接池耗尽或数据库故障；结合健康探针判断。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "服务暂时不可用，请稍后重试。"
    ],
    "http_status": 503,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "数据库不可用"
    ],
    "owner": "platform",
    "problem": "数据库不可用",
    "retryable": true,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "数据库不可用",
    "tone": "warning"
  },
  "LC-PLATFORM-007": {
    "action": "平台日志存储目录不可写（错误详情已给出目录路径、运行用户与原因），请联系运维确认容器已挂载可写卷后重试。",
    "admin_detail": "检查 LOG_COLLECT_STORAGE_ROOT 是否指向已挂载卷、Pod fsGroup=1000 是否生效；在容器内用 `id` 与 `touch <dir>/.probe` 可直接复现权限。",
    "business_stage": "upload",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "平台日志存储目录不可写（错误详情已给出目录路径、运行用户与原因），请联系运维确认容器已挂载可写卷后重试。"
    ],
    "http_status": 503,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "日志存储目录不可写"
    ],
    "owner": "platform",
    "problem": "日志存储目录不可写",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S5",
    "title_zh": "日志存储目录不可写",
    "tone": "warning"
  },
  "LC-PLATFORM-008": {
    "action": "平台日志存储空间不足（错误详情已给出所需与可用空间），请清理平台日志存储或扩容后重试。",
    "admin_detail": "合并阶段需要与包体等量的临时空间（峰值约 2 倍包体）；检查 LOG_COLLECT_STORAGE_ROOT 所在卷剩余容量。",
    "business_stage": "upload",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "平台日志存储空间不足（错误详情已给出所需与可用空间），请清理平台日志存储或扩容后重试。"
    ],
    "http_status": 503,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "日志存储空间不足"
    ],
    "owner": "platform",
    "problem": "日志存储空间不足",
    "retryable": true,
    "scope": "cloud",
    "stage": "S5",
    "title_zh": "日志存储空间不足",
    "tone": "warning"
  },
  "LC-PLATFORM-009": {
    "action": "该任务正在上传或代传中，请等待当前进度结束后再点上传。",
    "admin_detail": "delivery_status 处于 pulling/uploading/relaying；长时间不动请查 bridge 交付作业日志与 relay_status。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "该任务正在上传或代传中，请等待当前进度结束后再点上传。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "任务正在交付中"
    ],
    "owner": "customer",
    "problem": "任务正在交付中",
    "retryable": true,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "任务正在交付中",
    "tone": "error"
  },
  "LC-PLATFORM-010": {
    "action": "与终端 bridge 的连接已断开，节点操作未送达；请刷新页面确认节点在线后重试。",
    "admin_detail": "查 terminal_bridge 是否重启/OOM、WSS 心跳是否超时；未送达意味着节点侧没有留下任何任务，重试是安全的。",
    "business_stage": "api",
    "check": [
      "确认桌面端 bridge 是否仍在运行、页面是否重连。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "重连 bridge 后重试；该任务的操作未送达节点。"
    ],
    "http_status": 503,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "与 bridge 的通道断开"
    ],
    "owner": "platform",
    "problem": "与 bridge 的通道断开",
    "retryable": true,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "与 terminal_bridge 的连接已断开",
    "tone": "warning"
  },
  "LC-PLATFORM-011": {
    "action": "任务已失败，但未记录可归因的原因；请查看任务详情或联系平台侧定位。",
    "admin_detail": "失败落库时 error_message 无法匹配任何已知模式，平台按兜底码落库。该码数量上涨说明某个上游在失败时没带原因，需按 trace_id 回溯补码。",
    "business_stage": "api",
    "check": [
      "查看任务详情 error_message 与 create_output。",
      "记录任务号与时间，提交支持按 trace 检索桥日志。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "不要反复重试；先补齐证据再定位，避免丢失现场。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "失败原因未归因"
    ],
    "owner": "platform",
    "problem": "失败原因未归因",
    "retryable": "manual",
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "失败原因未归因",
    "tone": "warning"
  },
  "LC-PLATFORM-012": {
    "action": "平台处理本次请求时内部出错；请稍后重试，反复失败请联系平台侧。",
    "admin_detail": "平台返回 5xx 且非存储/数据库专项码。查 case-service 日志中同一 trace_id 的堆栈；若集中在收尾接口，优先看并发与超时预算。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "平台处理本次请求时内部出错；请稍后重试，反复失败请联系平台侧。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "平台内部错误"
    ],
    "owner": "platform",
    "problem": "平台内部错误",
    "retryable": true,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "平台内部错误",
    "tone": "warning"
  },
  "LC-PLATFORM-013": {
    "action": "采集已结束但交付会话未被 bridge 执行（僵尸会话）；请点「补交付」重新上传。",
    "admin_detail": "采集终态后 delivery_status 长期停留在 queued，bridge 侧无对应作业。多为通道断开或节点侧任务已结束未回报；历史欠账由 061 清理脚本订正。",
    "business_stage": "api",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "htp",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLATFORM 层原始输出）",
    "fix": [
      "采集已结束但交付会话未被 bridge 执行（僵尸会话）；请点「补交付」重新上传。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "PLATFORM",
    "match_keywords": [
      "交付会话未被执行"
    ],
    "owner": "platform",
    "problem": "交付会话未被执行",
    "retryable": true,
    "scope": "cloud",
    "stage": "S8",
    "title_zh": "交付会话未被执行",
    "tone": "warning"
  },
  "LC-PLUGIN-001": {
    "action": "工单号只允许字母、数字、下划线与连字符（最长 32 位）。",
    "admin_detail": "插件会把 -s 拼进产物路径与 shell 命令，必须白名单。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "工单号只允许字母、数字、下划线与连字符（最长 32 位）。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "工单号非法"
    ],
    "owner": "platform",
    "problem": "工单号非法",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "工单号非法",
    "tone": "warning"
  },
  "LC-PLUGIN-002": {
    "action": "所选收集专项不受支持，请重新选择。",
    "admin_detail": "枚举：other/system/slowdisk/network/storage/vm_start/vm_suspend/host_offline/performance/core。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "所选收集专项不受支持，请重新选择。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "收集专项非法"
    ],
    "owner": "platform",
    "problem": "收集专项非法",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "收集专项非法",
    "tone": "warning"
  },
  "LC-PLUGIN-003": {
    "action": "日期格式或范围不合法（最长 30 天，且不得晚于今天）。",
    "admin_detail": "acli -d 只支持天粒度，不支持小时/分钟。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "日期格式或范围不合法（最长 30 天，且不得晚于今天）。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "收集日期非法"
    ],
    "owner": "platform",
    "problem": "收集日期非法",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "收集日期非法",
    "tone": "warning"
  },
  "LC-PLUGIN-004": {
    "action": "目标主机列表包含非法 IP。",
    "admin_detail": "仅允许 all 或逗号分隔 IPv4。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "目标主机列表包含非法 IP。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "目标主机非法"
    ],
    "owner": "platform",
    "problem": "目标主机非法",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "目标主机非法",
    "tone": "warning"
  },
  "LC-PLUGIN-005": {
    "action": "额外路径必须是绝对路径，且不能包含 .. 或特殊字符。",
    "admin_detail": "插件把 -p 直接拼进 nohup 命令，属注入高危面。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "额外路径必须是绝对路径，且不能包含 .. 或特殊字符。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "额外收集路径非法"
    ],
    "owner": "platform",
    "problem": "额外收集路径非法",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "额外收集路径非法",
    "tone": "warning"
  },
  "LC-PLUGIN-006": {
    "action": "上传地址必须以 http:// 或 https:// 开头。",
    "admin_detail": "插件只认深信服日志服务器域名白名单，平台不能冒充。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "上传地址必须以 http:// 或 https:// 开头。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "上传地址非法"
    ],
    "owner": "platform",
    "problem": "上传地址非法",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "上传地址非法",
    "tone": "warning"
  },
  "LC-PLUGIN-007": {
    "action": "该工单已有一个进行中的日志收集任务，请等待完成或先取消。",
    "admin_detail": "并发采集会互相 kill 并覆盖产物目录，必须有唯一约束。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "该工单已有一个进行中的日志收集任务，请等待完成或先取消。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "已有进行中的任务"
    ],
    "owner": "platform",
    "problem": "已有进行中的任务",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "已有进行中的任务",
    "tone": "warning"
  },
  "LC-PLUGIN-008": {
    "action": "未找到对应的日志收集任务。",
    "admin_detail": "progress 只反映发起节点；路由到错误节点会误报不存在。",
    "business_stage": "progress",
    "check": [
      "核对任务 ID 是否属于本工单、是否已被取消/清理。",
      "确认查询的节点与创建任务的节点一致。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "重新创建任务；不要复用已结束的任务 ID。"
    ],
    "http_status": 404,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "任务不存在"
    ],
    "owner": "platform",
    "problem": "任务不存在",
    "retryable": false,
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "任务不存在或已完成",
    "tone": "warning"
  },
  "LC-PLUGIN-009": {
    "action": "进度信息格式有变化，已展示原始输出。",
    "admin_detail": "非失败：parse_ok=false 时前端展示 raw，不得据此判任务失败。",
    "business_stage": "progress",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "进度信息格式有变化，已展示原始输出。"
    ],
    "http_status": 200,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "进度解析降级"
    ],
    "owner": "product",
    "problem": "进度解析降级",
    "retryable": false,
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "进度解析降级",
    "tone": "error"
  },
  "LC-PLUGIN-010": {
    "action": "日志采集执行失败，请结合节点状态排查。",
    "admin_detail": "插件返回非零；常见于磁盘不足、目标节点离线、专项脚本异常。",
    "business_stage": "collect",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "日志采集执行失败，请结合节点状态排查。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "日志采集失败"
    ],
    "owner": "customer",
    "problem": "日志采集失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "日志采集失败",
    "tone": "error"
  },
  "LC-PLUGIN-011": {
    "action": "取消采集失败，采集进程可能仍在运行。",
    "admin_detail": "插件在无进度文件时抛 IOError；需与平台状态对账。",
    "business_stage": "cancel",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "取消采集失败，采集进程可能仍在运行。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "取消任务失败"
    ],
    "owner": "customer",
    "problem": "取消任务失败",
    "retryable": true,
    "scope": "intranet",
    "stage": "S8",
    "title_zh": "取消任务失败",
    "tone": "error"
  },
  "LC-PLUGIN-012": {
    "action": "部分主机采集失败，日志包可能不完整。",
    "admin_detail": "根据 detail.hosts 定位失败节点；离线节点会被标记 host offline。",
    "business_stage": "collect",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "部分主机采集失败，日志包可能不完整。"
    ],
    "http_status": 200,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "部分主机采集失败"
    ],
    "owner": "customer",
    "problem": "部分主机采集失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "部分主机采集失败",
    "tone": "error"
  },
  "LC-PLUGIN-013": {
    "action": "不支持的操作类型。",
    "admin_detail": "log_action 仅支持 probe / create / progress / cancel。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "不支持的操作类型。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "非法操作类型"
    ],
    "owner": "platform",
    "problem": "非法操作类型",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "非法操作类型",
    "tone": "warning"
  },
  "LC-PLUGIN-014": {
    "action": "日志收集任务 ID 缺失，无法确定场景清单备份归属。",
    "admin_detail": "create 消息必须携带平台任务 ID（exec_id），需匹配正则 ^[A-Za-z0-9._-]{1,64}$；核对前端 create 载荷与建任务响应。",
    "business_stage": "create",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "日志收集任务 ID 缺失，无法确定场景清单备份归属。"
    ],
    "http_status": 400,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "任务 ID 缺失或非法"
    ],
    "owner": "platform",
    "problem": "任务 ID 缺失或非法",
    "retryable": false,
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "任务 ID 缺失或非法",
    "tone": "warning"
  },
  "LC-PLUGIN-015": {
    "action": "acli create 退出码为 0，但节点上始终查不到任务。通常是节点 acli 版本与所选专项不匹配。",
    "admin_detail": "核对节点 acli 版本与所选专项（完整节点输出见任务详情 create_output）；用内置专项做 A/B 对照后重试。不要按任务 ID 不存在处理。",
    "business_stage": "progress",
    "check": [
      "核对节点 acli 版本与所选专项是否匹配。",
      "任务详情 create_output 里有节点完整输出。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "换用节点内置专项做 A/B 对照后重试。",
      "与任务 ID 无关，不要按“任务不存在”处理。"
    ],
    "http_status": 200,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "下发后节点未建立采集任务"
    ],
    "owner": "customer",
    "problem": "下发后节点未建立采集任务",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S4",
    "title_zh": "下发后节点未建立采集任务",
    "tone": "error"
  },
  "LC-PLUGIN-016": {
    "action": "acli 明确拒绝：收集专项在节点上不存在。平台下发的专项名与节点 acli 内置清单不一致。",
    "admin_detail": "在节点执行 acli log_collect help 核对内置专项名；修正场景/专项配置后重试。直接重试不会恢复。",
    "business_stage": "create",
    "check": [
      "在节点执行 acli log_collect help，核对可用专项名。",
      "确认平台场景配置下发的专项名与节点版本一致。"
    ],
    "component": "acli",
    "evidence": "detail.spc_hosts / bridge_execution_logs（PLUGIN 层原始输出）",
    "fix": [
      "修正场景/专项配置后重试。",
      "直接重试不会恢复，acli 会持续拒绝。"
    ],
    "http_status": 422,
    "kind": "runtime",
    "layer": "PLUGIN",
    "match_keywords": [
      "节点上没有所选专项"
    ],
    "owner": "customer",
    "problem": "节点上没有所选专项",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "节点上没有所选专项",
    "tone": "error"
  },
  "LC-SPC-001": {
    "action": "目标节点上找不到平台场景清单目录。节点可能未安装对应平台版本，或目录被移动/清理。",
    "admin_detail": "在报错 IP 上确认平台场景清单目录存在且只有一份；error_detail 带实际探测输出。确认节点版本与平台匹配后重试。",
    "business_stage": "create",
    "check": [
      "按提示里的节点 IP 登录节点，确认平台场景清单目录存在且只有一份。",
      "核对节点平台版本与发起侧是否一致。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "补齐/恢复场景清单目录后重新下发。",
      "若目录被移动，先还原再重试；不要用新建空目录代替。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "场景清单目录缺失"
    ],
    "owner": "customer",
    "problem": "场景清单目录缺失",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "场景清单目录缺失",
    "tone": "error"
  },
  "LC-SPC-002": {
    "action": "目标节点上存在多份平台场景清单目录，无法判断应该写入哪一份。",
    "admin_detail": "在报错 IP 上合并或清理多余目录，只保留一份后重试；error_detail 会给出候选目录。",
    "business_stage": "create",
    "check": [
      "按提示 IP 检查节点上是否同时存在多份场景清单目录。",
      "任务详情里的 error_detail 会给出候选目录。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "合并或清理多余目录，只保留一份后重试。",
      "无法判断哪份有效时联系运维确认，避免写错清单。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "场景清单目录不唯一"
    ],
    "owner": "customer",
    "problem": "场景清单目录不唯一",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "场景清单目录不唯一",
    "tone": "error"
  },
  "LC-SPC-003": {
    "action": "写入前无法备份原有场景清单，通常是目录不可写、磁盘空间不足或权限不足。",
    "admin_detail": "在报错 IP 上检查目标目录可写与磁盘余量；error_detail 带备份命令输出。修复后重试。",
    "business_stage": "create",
    "check": [
      "确认场景清单目录可写、磁盘余量充足。",
      "查看任务详情 error_detail 中的备份命令输出。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "释放磁盘空间或修复目录权限后重试。",
      "只读文件系统需先恢复可写，重试不会自动变好。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "场景清单备份失败"
    ],
    "owner": "customer",
    "problem": "场景清单备份失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "场景清单备份失败",
    "tone": "error"
  },
  "LC-SPC-004": {
    "action": "场景清单写入后回读校验不一致，可能是磁盘只读、空间不足或并发改写。",
    "admin_detail": "检查节点磁盘与文件系统状态，确认没有其它进程同时修改场景清单；error_detail 带校验命令输出。",
    "business_stage": "create",
    "check": [
      "确认节点磁盘未满、文件系统可写。",
      "确认没有其它任务或人工操作同时修改场景清单。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "排除磁盘/并发问题后重试。",
      "重复失败时保留 error_detail，提交支持定位。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "场景清单写入或校验失败"
    ],
    "owner": "customer",
    "problem": "场景清单写入或校验失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "场景清单写入或校验失败",
    "tone": "error"
  },
  "LC-SPC-005": {
    "action": "写入未通过后回滚也失败，节点上可能残留平台清单，重试前必须先人工核对。",
    "admin_detail": "按 detail.spc_hosts 的 IP 与备份信息核对残留；确认清理/还原后再重试，避免带残留清单采集。",
    "business_stage": "create",
    "check": [
      "按提示 IP 核对节点上是否残留平台场景清单。",
      "确认原清单备份是否仍可还原。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "先人工还原/清理残留清单，再重新下发。",
      "未核对前不要反复重试，避免带残留清单采集。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "场景清单回滚失败"
    ],
    "owner": "customer",
    "problem": "场景清单回滚失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "场景清单回滚失败",
    "tone": "error"
  },
  "LC-SPC-006": {
    "action": "从发起节点 SSH 到目标节点失败：连接被拒、路由不可达或主机密钥变化。",
    "admin_detail": "在发起节点用同一账号执行 ssh <目标IP> 复现；检查防火墙、节点状态与 known_hosts。error_detail 带原始错误。",
    "business_stage": "create",
    "check": [
      "在发起节点用同一账号执行 ssh <目标IP>，确认能否建立连接。",
      "检查节点是否在线、防火墙与 known_hosts 主机密钥是否变化。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "恢复节点网络/主机密钥后重试。",
      "主机密钥变化需人工确认后再更新，不能盲目跳过校验。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "目标节点 SSH 连接失败"
    ],
    "owner": "customer",
    "problem": "目标节点 SSH 连接失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "目标节点 SSH 连接失败",
    "tone": "error"
  },
  "LC-SPC-007": {
    "action": "SSH 认证失败：免密未配置、密钥变更或账号不一致。",
    "admin_detail": "确认发起节点到目标节点同账号免密登录可用（ssh -o BatchMode=yes <IP> true）；修复后重试。",
    "business_stage": "create",
    "check": [
      "确认发起节点到目标节点使用同一账号免密登录可用。",
      "核对近期是否更换过密钥或账号。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "重新配置免密登录后重试。",
      "认证问题与采集参数无关，改专项配置不会恢复。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "目标节点 SSH 认证失败"
    ],
    "owner": "customer",
    "problem": "目标节点 SSH 认证失败",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "目标节点 SSH 认证失败",
    "tone": "error"
  },
  "LC-SPC-008": {
    "action": "目标节点命令超过超时时间未返回，通常是节点负载高或磁盘 IO 卡住。",
    "admin_detail": "检查目标节点负载与磁盘 IO，恢复后重试；持续超时需按节点异常处理。",
    "business_stage": "create",
    "check": [
      "检查目标节点负载与磁盘 IO 是否异常。",
      "确认节点没有卡在其它重操作上。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "负载恢复后重试。",
      "持续超时按节点异常处理，不要连续重试加重负载。"
    ],
    "http_status": 504,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "目标节点命令执行超时"
    ],
    "owner": "customer",
    "problem": "目标节点命令执行超时",
    "retryable": "true",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "目标节点命令执行超时",
    "tone": "error"
  },
  "LC-SPC-009": {
    "action": "场景清单目录存在，但所选槽位的清单文件缺失或命名异常。",
    "admin_detail": "在目标节点确认对应槽位清单文件存在；核对平台场景配置与节点版本是否匹配。",
    "business_stage": "create",
    "check": [
      "确认所选槽位对应的清单文件存在且命名正确。",
      "核对平台场景配置与节点版本是否匹配。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "补齐槽位清单文件或修正场景配置后重试。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "场景清单文件缺失"
    ],
    "owner": "customer",
    "problem": "场景清单文件缺失",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "场景清单文件缺失",
    "tone": "error"
  },
  "LC-SPC-010": {
    "action": "逐台原因见任务详情，先按具体原因处理后再重新下发。",
    "admin_detail": "按 detail.spc_hosts[].error_code 与 error_detail 逐台处置；先修 SSH/认证类，再处理目录与写入类。",
    "business_stage": "create",
    "check": [
      "任务详情 detail.spc_hosts 里逐台查看 error_code 与 error_detail。",
      "先区分 SSH/认证类与目录/写入类。"
    ],
    "component": "hci",
    "evidence": "detail.spc_hosts / bridge_execution_logs（SPC 层原始输出）",
    "fix": [
      "按逐台原因分别修复后再重新下发。",
      "不要只按聚合码排查，每台原因可能不同。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "SPC",
    "match_keywords": [
      "场景清单写入未通过",
      "场景未能写入全部目标节点"
    ],
    "owner": "customer",
    "problem": "场景清单写入未通过",
    "retryable": "manual",
    "scope": "intranet",
    "stage": "S3",
    "title_zh": "场景清单写入未通过",
    "tone": "error"
  },
  "LC-TRANSFER-001": {
    "action": "未找到采集产物，可能已被清理。",
    "admin_detail": "插件 help 明确要求完成后手动删除；归档后需以平台记录为准。",
    "business_stage": "package",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "未找到采集产物，可能已被清理。"
    ],
    "http_status": 404,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "压缩包不存在"
    ],
    "owner": "customer",
    "problem": "压缩包不存在",
    "retryable": "manual",
    "scope": "local",
    "stage": "S4",
    "title_zh": "压缩包不存在",
    "tone": "error"
  },
  "LC-TRANSFER-002": {
    "action": "文件传输校验失败，请重试。",
    "admin_detail": "比对 size 与 sha256；不一致时删除半成品，禁止继续使用。",
    "business_stage": "upload",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "文件传输校验失败，请重试。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "制品校验和不一致"
    ],
    "owner": "platform",
    "problem": "制品校验和不一致",
    "retryable": true,
    "scope": "local",
    "stage": "S5",
    "title_zh": "制品校验和不一致",
    "tone": "warning"
  },
  "LC-TRANSFER-003": {
    "action": "日志包上传中断，可重试续传。",
    "admin_detail": "记录已传 offset 与块数，支持断点续传。",
    "business_stage": "upload",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "日志包上传中断，可重试续传。"
    ],
    "http_status": 502,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "分块上传失败"
    ],
    "owner": "customer",
    "problem": "分块上传失败",
    "retryable": true,
    "scope": "local",
    "stage": "S5",
    "title_zh": "分块上传失败",
    "tone": "error"
  },
  "LC-TRANSFER-004": {
    "action": "续传状态异常，请重新上传。",
    "admin_detail": "upload_id 过期或 offset 不连续。",
    "business_stage": "upload",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "续传状态异常，请重新上传。"
    ],
    "http_status": 500,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "断点续传失败"
    ],
    "owner": "platform",
    "problem": "断点续传失败",
    "retryable": "manual",
    "scope": "local",
    "stage": "S5",
    "title_zh": "断点续传失败",
    "tone": "warning"
  },
  "LC-TRANSFER-005": {
    "action": "日志包超过平台上限，请联系技术支持。",
    "admin_detail": "分块上传需放宽单请求体积，避免 413。",
    "business_stage": "upload",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "日志包超过平台上限，请联系技术支持。"
    ],
    "http_status": 413,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "超出大小限制"
    ],
    "owner": "platform",
    "problem": "超出大小限制",
    "retryable": false,
    "scope": "local",
    "stage": "S5",
    "title_zh": "超出大小限制",
    "tone": "warning"
  },
  "LC-TRANSFER-006": {
    "action": "节点暂时无法执行文件探测命令，无法确认日志包是否还在；请稍后重试。",
    "admin_detail": "aSV 受限 shell 可能没有 stat（exit 127），必须走 ls -l 兜底；各级 exit/stderr 写入 archive_probe_diag。",
    "business_stage": "package",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "节点暂时无法执行文件探测命令，无法确认日志包是否还在；请稍后重试。"
    ],
    "http_status": 503,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "压缩包状态无法确认"
    ],
    "owner": "customer",
    "problem": "压缩包状态无法确认",
    "retryable": true,
    "scope": "local",
    "stage": "S4",
    "title_zh": "压缩包状态无法确认",
    "tone": "error"
  },
  "LC-TRANSFER-007": {
    "action": "节点报告已直传成功，但日志服务器上没有找到一致的归档件；请重新采集一次，或改用「上传到平台」走中转。",
    "admin_detail": "比对 5888 GET /archive_status 的 found/size/mtime 与节点报告的 archive_size；P1 成功后插件会自动删包，核验不通过时节点已无源包，无法补交付。",
    "business_stage": "upload",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "节点报告已直传成功，但日志服务器上没有找到一致的归档件；请重新采集一次，或改用「上传到平台」走中转。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "直传归档核验未通过"
    ],
    "owner": "customer",
    "problem": "直传归档核验未通过",
    "retryable": false,
    "scope": "local",
    "stage": "S5",
    "title_zh": "直传归档核验未通过",
    "tone": "error"
  },
  "LC-TRANSFER-008": {
    "action": "日志包需要先落到客户机临时目录再上传，当前磁盘可用空间不足；请清理临时副本后重试，无需重新采集。",
    "admin_detail": "P2 中转副本默认位于 %TEMP%/hci-log-collect（Windows: C:\\Users\\<user>\\AppData\\Local\\Temp\\hci-log-collect）。清理历史 zip 后重试；bridge 侧 delivery.job_finished 会带 need/free/path。",
    "business_stage": "package",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "日志包需要先落到客户机临时目录再上传，当前磁盘可用空间不足；请清理临时副本后重试，无需重新采集。"
    ],
    "http_status": 507,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "客户机磁盘空间不足"
    ],
    "owner": "customer",
    "problem": "客户机磁盘空间不足",
    "retryable": "manual",
    "scope": "local",
    "stage": "S4",
    "title_zh": "客户机磁盘空间不足",
    "tone": "error"
  },
  "LC-TRANSFER-009": {
    "action": "客户机上还有其它交付任务未完成，本地临时空间不足以同时容纳本次交付；请等待其它任务完成后再重试，或分批交付，无需重新采集。",
    "admin_detail": "并发准入：同目录进行中作业未落地字节之和 + 本次需求 > 可用空间。可调低 HCI_BRIDGE_DELIVERY_CONCURRENCY 或清理 %TEMP%/hci-log-collect；delivery.disk_precheck 事件带 reserved_bytes。",
    "business_stage": "package",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "客户机上还有其它交付任务未完成，本地临时空间不足以同时容纳本次交付；请等待其它任务完成后再重试，或分批交付，无需重新采集。"
    ],
    "http_status": 507,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "客户机交付并发容量不足"
    ],
    "owner": "customer",
    "problem": "客户机交付并发容量不足",
    "retryable": "manual",
    "scope": "local",
    "stage": "S4",
    "title_zh": "客户机交付并发容量不足",
    "tone": "error"
  },
  "LC-TRANSFER-010": {
    "action": "节点上的压缩包还没写完（采集已到 100%，但 zip 仍在收尾或被重新生成）。此时建立的上传会话按旧大小校验，继续传每片都会失败；请等包大小不再变化后再点「上传到平台」，要最新内容请重新采集。",
    "admin_detail": "bridge 侧 delivery.* 事件可查：waitArchiveSettled 超时、pull 期间大小变化（拉取前/当前字节数）、sourceSizeGuard 命中。排查节点 zip 是否仍被 acli 写入，或是否被重复采集覆盖；确认包稳定后重开上传会话（新会话=新基线）。",
    "business_stage": "package",
    "check": [
      "按页面提示里的节点 IP 与错误码，在任务详情中核对原始输出。"
    ],
    "component": "bridge",
    "evidence": "detail.spc_hosts / bridge_execution_logs（TRANSFER 层原始输出）",
    "fix": [
      "节点上的压缩包还没写完（采集已到 100%，但 zip 仍在收尾或被重新生成）。此时建立的上传会话按旧大小校验，继续传每片都会失败；请等包大小不再变化后再点「上传到平台」，要最新内容请重新采集。"
    ],
    "http_status": 409,
    "kind": "runtime",
    "layer": "TRANSFER",
    "match_keywords": [
      "源包仍在写入"
    ],
    "owner": "customer",
    "problem": "源包仍在写入",
    "retryable": "manual",
    "scope": "local",
    "stage": "S4",
    "title_zh": "源包仍在写入",
    "tone": "error"
  }
} as const

export const FAULT_MATCHERS = [
  {
    "code": "LC-BRIDGE-002",
    "keywords": [
      "x509",
      "certificate signed by unknown authority",
      "证书不受信",
      "tls: failed to verify certificate"
    ]
  },
  {
    "code": "LC-ACLI-001",
    "keywords": [
      "未知的命令或者命名空间",
      "unknown command",
      "log collect create",
      "log_collect create"
    ]
  },
  {
    "code": "LC-5888-001",
    "keywords": [
      "上传票据校验失败",
      "票据校验失败",
      "鉴权失败",
      "[P1] 票据",
      "401 unauthorized"
    ]
  },
  {
    "code": "LC-ACLI-002",
    "keywords": [
      "acli版本过低",
      "version_outdated",
      "缺少日志收集插件",
      "acli 安装重试失败",
      "acli 重试失败",
      "acli安装失败"
    ]
  },
  {
    "code": "LC-ACLI-003",
    "keywords": [
      "parse_ok=false",
      "采集进度无法解析",
      "进度解析失败"
    ]
  },
  {
    "code": "LC-ACLI-004",
    "keywords": [
      "压缩包体积异常偏小",
      "metadata_only",
      "包内没有日志内容",
      "空壳包"
    ]
  },
  {
    "code": "LC-HCI-001",
    "keywords": [
      "SSH连接失败",
      "ssh: connect",
      "connection refused",
      "无法连接SSH",
      "ssh_exchange_identification"
    ]
  },
  {
    "code": "LC-HCI-002",
    "keywords": [
      "磁盘空间不足",
      "no space left",
      "DiskPressure",
      "磁盘压力"
    ]
  },
  {
    "code": "LC-HCI-003",
    "keywords": [
      "auto-logout",
      "会话已断开",
      "SSH 会话已断开",
      "TMOUT"
    ]
  },
  {
    "code": "LC-BRIDGE-001",
    "keywords": [
      "无法连接本地服务",
      "BRIDGE_CHANNEL_LOST",
      "连接 terminal_bridge 超时",
      "本地桥接端未启动",
      "websocket connection",
      "WebSocket 连接失败",
      "无法连接桥接端"
    ]
  },
  {
    "code": "LC-BRIDGE-003",
    "keywords": [
      "桥接端版本较旧",
      "capabilities.log_collect_evidence=false",
      "暂无该数据"
    ]
  },
  {
    "code": "LC-BRIDGE-004",
    "keywords": [
      "代传失败",
      "relay_last_error",
      "P2平台中转失败"
    ]
  },
  {
    "code": "LC-BRIDGE-006",
    "keywords": [
      "下发失败",
      "任务下发失败",
      "log_collect create 失败"
    ]
  },
  {
    "code": "LC-HTP-001",
    "keywords": [
      "archive_missing",
      "未核验到归档件",
      "归档核验未完成"
    ]
  },
  {
    "code": "LC-HTP-003",
    "keywords": [
      "任务状态加载失败",
      "任务进度查询失败",
      "任务状态查询失败"
    ]
  },
  {
    "code": "LC-BRIDGE-005",
    "keywords": [
      "包内容未探测",
      "artifact_uninspected",
      "包内容清单未回传"
    ]
  },
  {
    "code": "LC-5888-002",
    "keywords": [
      "日志服务器不可达",
      "log_server_reachable=false",
      "5888不可达"
    ]
  },
  {
    "code": "LC-5888-003",
    "keywords": [
      "size_mismatch",
      "stale_archive",
      "归档件大小不一致"
    ]
  },
  {
    "code": "LC-5888-004",
    "keywords": [
      "归档核验等待超时",
      "archive_verify_timeout",
      "始终未发布归档件"
    ]
  },
  {
    "code": "LC-HTP-002",
    "keywords": [
      "未给出可识别的错误码",
      "未返回具体原因"
    ]
  },
  {
    "code": "LC-SPC-001",
    "keywords": [
      "场景清单目录缺失"
    ]
  },
  {
    "code": "LC-SPC-002",
    "keywords": [
      "场景清单目录不唯一"
    ]
  },
  {
    "code": "LC-SPC-003",
    "keywords": [
      "场景清单备份回读不一致",
      "场景清单备份失败"
    ]
  },
  {
    "code": "LC-SPC-004",
    "keywords": [
      "场景清单写入后校验不一致",
      "场景清单写入被拒绝",
      "场景清单写入失败"
    ]
  },
  {
    "code": "LC-SPC-009",
    "keywords": [
      "场景清单文件缺失"
    ]
  },
  {
    "code": "LC-SPC-005",
    "keywords": [
      "场景清单回滚失败"
    ]
  },
  {
    "code": "LC-SPC-007",
    "keywords": [
      "节点 SSH 认证失败"
    ]
  },
  {
    "code": "LC-SPC-008",
    "keywords": [
      "节点命令执行超时"
    ]
  },
  {
    "code": "LC-SPC-006",
    "keywords": [
      "节点 SSH 连接失败"
    ]
  },
  {
    "code": "LC-SPC-010",
    "keywords": [
      "场景清单写入未通过",
      "场景未能写入全部目标节点"
    ]
  },
  {
    "code": "LC-PLUGIN-016",
    "keywords": [
      "收集专项",
      "节点上没有所选专项"
    ]
  },
  {
    "code": "LC-PLUGIN-015",
    "keywords": [
      "未建立采集任务"
    ]
  },
  {
    "code": "LC-PLATFORM-007",
    "keywords": [
      "存储目录不可写",
      "不可写"
    ]
  },
  {
    "code": "LC-PLATFORM-008",
    "keywords": [
      "存储空间不足"
    ]
  },
  {
    "code": "LC-PLATFORM-013",
    "keywords": [
      "判定为僵尸会话已清理"
    ]
  },
  {
    "code": "LC-PLATFORM-012",
    "keywords": [
      "平台收尾失败",
      "INTERNAL_ERROR",
      "服务内部错误",
      "status code 5"
    ]
  },
  {
    "code": "LC-TRANSFER-009",
    "keywords": [
      "客户机还有其它交付任务",
      "并发容量不足"
    ]
  },
  {
    "code": "LC-TRANSFER-008",
    "keywords": [
      "客户机磁盘空间不足",
      "临时目录空间不足",
      "写入本地文件失败",
      "%TEMP%"
    ]
  },
  {
    "code": "LC-ENV-004",
    "keywords": [
      "/sf/data/local",
      "磁盘空间不足",
      "可用空间"
    ]
  },
  {
    "code": "LC-ENV-006",
    "keywords": [
      "SSH 会话不存在",
      "ssh_connect",
      "需重新连接节点",
      "重新连接 SSH",
      "交付需要重新认证"
    ]
  },
  {
    "code": "LC-ENV-007",
    "keywords": [
      "SSH 认证失败"
    ]
  },
  {
    "code": "LC-ENV-008",
    "keywords": [
      "节点离线",
      "host offline"
    ]
  },
  {
    "code": "LC-ENV-005",
    "keywords": [
      "日志服务器不可达"
    ]
  },
  {
    "code": "LC-EXEC-003",
    "keywords": [
      "acli: command not found",
      "未知的命令或者命名空间",
      "plugin: no such file",
      "命令返回非零"
    ]
  },
  {
    "code": "LC-EXEC-002",
    "keywords": [
      "命令执行超时",
      "超时"
    ]
  },
  {
    "code": "LC-EXEC-005",
    "keywords": [
      "SSH 等待结果失败"
    ]
  },
  {
    "code": "LC-INSTALL-002",
    "keywords": [
      "下载安装包失败"
    ]
  },
  {
    "code": "LC-INSTALL-004",
    "keywords": [
      "安装包上传到 HCI 失败"
    ]
  },
  {
    "code": "LC-INSTALL-005",
    "keywords": [
      "acli 安装命令失败"
    ]
  },
  {
    "code": "LC-INSTALL-006",
    "keywords": [
      "安装后版本验证失败"
    ]
  },
  {
    "code": "LC-INSTALL-007",
    "keywords": [
      "落盘校验失败"
    ]
  },
  {
    "code": "LC-PLUGIN-007",
    "keywords": [
      "已有进行中的任务",
      "正在执行",
      "已有采集在跑"
    ]
  },
  {
    "code": "LC-PLUGIN-008",
    "keywords": [
      "节点上已无任务",
      "任务不存在",
      "未找到对应的日志收集任务"
    ]
  },
  {
    "code": "LC-PLUGIN-010",
    "keywords": [
      "采集未完成",
      "日志采集执行失败"
    ]
  },
  {
    "code": "LC-PLUGIN-011",
    "keywords": [
      "取消采集失败"
    ]
  },
  {
    "code": "LC-PLUGIN-012",
    "keywords": [
      "部分主机采集失败"
    ]
  },
  {
    "code": "LC-TRANSFER-010",
    "keywords": [
      "源包已变化",
      "源包仍在写入",
      "仍未写完",
      "会话已被新代替代",
      "上传会话大小不一致",
      "按新基线重建"
    ]
  },
  {
    "code": "LC-TRANSFER-001",
    "keywords": [
      "压缩包不存在",
      "源包可能已被清理",
      "本地副本不存在",
      "未找到采集产物"
    ]
  },
  {
    "code": "LC-TRANSFER-002",
    "keywords": [
      "校验和不一致"
    ]
  },
  {
    "code": "LC-TRANSFER-003",
    "keywords": [
      "上传中断",
      "分块上传失败",
      "片）",
      "归档到日志服务器失败"
    ]
  },
  {
    "code": "LC-TRANSFER-006",
    "keywords": [
      "无法确认日志包"
    ]
  },
  {
    "code": "LC-TRANSFER-007",
    "keywords": [
      "归档核验未通过",
      "直传归档核验"
    ]
  },
  {
    "code": "LC-PLATFORM-010",
    "keywords": [
      "与 terminal_bridge 的连接已断开",
      "bridge 连接已断开"
    ]
  }
] as const

export const FAULT_ATTRIBUTION = {
  "api_field_case": "snake_case",
  "customer_format": "{component_label}：{problem}；{action}",
  "default_code": "LC-HTP-002",
  "fields": [
    "code",
    "scope",
    "scope_label",
    "component",
    "component_label",
    "component_tag",
    "stage",
    "stage_label",
    "problem",
    "action",
    "customer_text",
    "admin_detail",
    "evidence",
    "trace_id"
  ],
  "match_policy": "longest_keyword",
  "note": "首个异常点按 stage_order 取第一个 failed/waiting 阶段；结论本身不是异常点。归类取最长命中关键词，泛词不得抢走更具体的证据；前后端 API 字段一律 snake_case。",
  "stage_order": [
    "S1",
    "S2",
    "S3",
    "S4",
    "S5",
    "S6",
    "S7",
    "S8"
  ]
} as const

export const FAULT_DEFAULT_CODE = "LC-HTP-002" as const
export const ARCHIVE_VERIFY_WAIT_MINUTES = 10

export type ScopeKey = keyof typeof SCOPE_LABELS
export type ComponentKey = keyof typeof COMPONENTS
export type StageKey = keyof typeof STAGES
export type FaultCode = keyof typeof FAULT_CATALOG


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

/** 客户侧错误文案：组件标签：问题描述；建议动作。 */
export function formatFaultText(code: string): string {
  const item = FAULT_CATALOG[code as FaultCode] ?? FAULT_CATALOG[FAULT_DEFAULT_CODE]
  const component = COMPONENTS[item.component as ComponentKey]
  return `${component.label}：${item.problem}；${item.action}`
}

/** 把任意错误载荷规整为可匹配文本（与后端 _fault_text 行为对齐）。 */
function faultText(message: unknown): string {
  if (Array.isArray(message)) return message.slice(0, 8).map((part) => String(part)).join(' ')
  if (message && typeof message === 'object') {
    const record = message as Record<string, unknown>
    return ['error', 'message', 'reason', 'detail']
      .map((key) => record[key])
      .filter((part) => part !== undefined && part !== null && part !== '')
      .map((part) => String(part))
      .join(' ')
  }
  return String(message ?? '')
}

/** 把原始错误文本归类到组件错误码，取最长命中关键词（与后端 classify_fault 行为对齐）。 */
export function classifyFault(message: unknown): FaultCode {
  const text = faultText(message).toLowerCase()
  let bestCode = FAULT_DEFAULT_CODE as FaultCode
  let bestLength = 0
  for (const matcher of FAULT_MATCHERS) {
    let hit = 0
    for (const keyword of matcher.keywords) {
      if (text.includes(keyword.toLowerCase())) hit = Math.max(hit, keyword.length)
    }
    if (hit > bestLength) {
      bestLength = hit
      bestCode = matcher.code as FaultCode
    }
  }
  return bestCode
}

/** 首个异常点的完整归因；字段名与后端 API 一致，统一 snake_case。 */
export function faultDetail(code: string, traceId?: string | null) {
  const resolved: FaultCode = (code in FAULT_CATALOG ? code : FAULT_DEFAULT_CODE) as FaultCode
  const item = FAULT_CATALOG[resolved]
  const component = COMPONENTS[item.component as ComponentKey]
  const stage = STAGES[item.stage as StageKey]
  return {
    code: resolved,
    scope: component.scope,
    scope_label: SCOPE_LABELS[component.scope as ScopeKey] ?? SCOPE_LABELS.cloud,
    component: item.component,
    component_label: component.label,
    component_tag: component.tag,
    stage: item.stage,
    stage_label: stage.label,
    problem: item.problem,
    action: item.action,
    customer_text: formatFaultText(resolved),
    admin_detail: item.admin_detail,
    evidence: item.evidence,
    trace_id: traceId ?? null,
  }
}
