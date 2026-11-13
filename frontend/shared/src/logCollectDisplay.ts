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
    "component": "log_server",
    "evidence": "upload_server.log 鉴权失败 / [P1] 票据通过",
    "problem": "上传票据校验失败(401)",
    "stage": "S5",
    "tone": "error"
  },
  "LC-5888-002": {
    "action": "请检查节点到5888的网络，或改用P2平台中转",
    "admin_detail": "probe.log_server_reachable=false 或 network_probe target=log_server reachable=false；注意 P2 代传落点仍是同一个 5888。",
    "component": "log_server",
    "evidence": "detail.probe.log_server_reachable、log_collect_network_probe",
    "problem": "日志服务器不可达",
    "stage": "S5",
    "tone": "error"
  },
  "LC-5888-003": {
    "action": "请联系管理员按上传ID核对，不要直接覆盖归档件",
    "admin_detail": "verify-archive reason=size_mismatch / stale_archive；同工单目录可能留有旧包，规范名=最新包、旧包带时间戳。",
    "component": "log_server",
    "evidence": "p1_archive_check.expected_size / archived_size / stored_name",
    "problem": "归档件大小或时间不一致",
    "stage": "S6",
    "tone": "warning"
  },
  "LC-5888-004": {
    "action": "请截图本页并联系管理员核对日志服务器，不要重复收集",
    "admin_detail": "p1_archive_check.reason=archive_missing 且 checked_at 已超过 archive_verify_wait_minutes；先查 5888 upload_server.log 的 UploadComplete 与 /sf/log 规范归档名，再决定重传还是补归档，禁止直接重新采集。",
    "component": "log_server",
    "evidence": "detail.p1_archive_check.checked_at / reason、5888 upload_server.log",
    "problem": "等待10分钟后日志服务器仍未发布归档件",
    "stage": "S6",
    "tone": "error"
  },
  "LC-ACLI-001": {
    "action": "请在HCI后台执行acli --version确认acli版本",
    "admin_detail": "acli 1.0.0 不同 build 的子命令集合不同；比对 detail.probe.acli_build_time 与 log_collect.create.command / output_preview，确认下发命令是否被该 build 支持。",
    "component": "acli",
    "evidence": "log_collect.create.output_preview、detail.probe.acli_build_time",
    "problem": "未知的命令或者命名空间(log collect create)",
    "stage": "S3",
    "tone": "error"
  },
  "LC-ACLI-002": {
    "action": "请联系管理员升级acli后重试，不要重复下发",
    "admin_detail": "看 acli_sync.version_outdated / install_done；旧 build 可能不支持 -m other 或 -H 票据头。",
    "component": "acli",
    "evidence": "acli_sync.version_check.current_build / remote_build",
    "problem": "acli版本过低或缺少日志收集插件",
    "stage": "S2",
    "tone": "error"
  },
  "LC-ACLI-003": {
    "action": "请联系管理员更新桥接端解析规则，暂不要重复下发",
    "admin_detail": "log_collect.progress.parse_ok=false；中英文输出差异指纹为 catalog_source=platform_default + parse_ok=false。",
    "component": "acli",
    "evidence": "parse_ok、status_text、catalog_source",
    "problem": "采集进度无法解析",
    "stage": "S4",
    "tone": "warning"
  },
  "LC-ACLI-004": {
    "action": "请核对采集时间范围与场景后重新收集",
    "admin_detail": "archive_size < min_archive_bytes 或 manifest 仅含配置条目；608 字节空壳包先例说明 sha256 一致不等于内容有效。",
    "component": "acli",
    "evidence": "archive_size、artifact_manifest.entry_count / entry_total_bytes",
    "problem": "压缩包体积异常偏小",
    "stage": "S4",
    "tone": "error"
  },
  "LC-BRIDGE-001": {
    "action": "请确认本地桥接端已启动，刷新页面后重试",
    "admin_detail": "本地 WS 未监听或 exe 过旧；查 bridge_startup.service.version 与 capabilities.log_collect_evidence。",
    "component": "bridge",
    "evidence": "openSocket 错误、BRIDGE_CHANNEL_LOST",
    "problem": "无法连接terminal_bridge",
    "stage": "S3",
    "tone": "error"
  },
  "LC-BRIDGE-002": {
    "action": "日志已交付，请联系管理员更新桥接端后补传证据",
    "admin_detail": "4443 自签 CA 不在客户机系统信任库；create 的 tls_insecure 依赖浏览器会话显式授权，P1 场景永不触发，证据通道需遇 x509 自动放宽一次并留痕（只放宽证书校验，不动票据鉴权）。",
    "component": "bridge",
    "evidence": "log_collect.network_probe_failed.error、log_collect.node_manifest_failed.error",
    "problem": "证据上报失败：平台证书不受信",
    "stage": "S7",
    "tone": "warning"
  },
  "LC-BRIDGE-003": {
    "action": "请联系管理员更新桥接端exe，日志本身不受影响",
    "admin_detail": "capabilities.log_collect_evidence 缺失；四张证据表 0 行是能力缺失，不是采集失败，界面禁止显示 0 或空白。",
    "component": "bridge",
    "evidence": "log_collect.capabilities、log_collect.evidence_enabled",
    "problem": "桥接端版本较旧，暂无该数据",
    "stage": "S7",
    "tone": "warning"
  },
  "LC-BRIDGE-004": {
    "action": "请点「上传到平台」重试，不需要重新采集",
    "admin_detail": "看 relay_status / relay_last_error；分片已落平台可续传，failed 后需人工点一次。",
    "component": "bridge",
    "evidence": "delivery.job_finished、relay_last_error",
    "problem": "P2平台中转失败",
    "stage": "S5",
    "tone": "error"
  },
  "LC-BRIDGE-005": {
    "action": "日志包已到日志服务器，请联系管理员补传证据，不要重新收集",
    "admin_detail": "artifact_manifest 缺行使 quality=unknown，结论只能是 partial「已交付 · 有疑点」；引入方是 S7 证据回传（x509 或旧桥），不是采集失败，也不是平台损坏。先查四张证据表与 *_failed 事件，别让用户重采。",
    "component": "bridge",
    "evidence": "artifact.quality=unknown、log_collect_artifact_manifest 行数、log_collect.node_manifest_failed.error",
    "problem": "已交付但包内容清单未回传",
    "stage": "S7",
    "tone": "warning"
  },
  "LC-BRIDGE-006": {
    "action": "请查看下方诊断并截图联系管理员，不要重复下发",
    "admin_detail": "create 阶段 create_ack 非 confirmed 或 exit_code 非 0；比对 detail.create_output 与 probe.acli_build_time，重复下发会在节点上叠加多个采集任务。",
    "component": "bridge",
    "evidence": "log_collect.create.output_preview、create_ack、exit_code",
    "problem": "任务下发失败",
    "stage": "S3",
    "tone": "error"
  },
  "LC-HCI-001": {
    "action": "请尝试其他终端连接HCI验证连接是否正常",
    "admin_detail": "检查 bridge_execution_logs 的 ssh.connected / ssh.disconnected(reason) / conn.role，并核对节点 22 端口与凭据；内网主机 SSH 端口可能是 22468。",
    "component": "hci",
    "evidence": "ssh.disconnected.reason、ssh.output.output_preview",
    "problem": "SSH连接失败",
    "stage": "S1",
    "tone": "error"
  },
  "LC-HCI-002": {
    "action": "请联系管理员确认/sf可用空间，不要自行删除文件",
    "admin_detail": "用 probe.available_mb 与 archive_size 比对；磁盘压力会触发 Pod 驱逐并造成全栈中断，禁止在界面给出删除命令。",
    "component": "hci",
    "evidence": "log_collect.probe.available_mb、archive_size_probe.diag",
    "problem": "HCI磁盘空间不足",
    "stage": "S4",
    "tone": "error"
  },
  "LC-HCI-003": {
    "action": "请重新连接SSH；已完成的日志包无需重新收集",
    "admin_detail": "TMOUT auto-logout 后 session.shell_respawned 自动重生；核对断连时间是否晚于 5888 UploadComplete，晚于则不影响交付。",
    "component": "hci",
    "evidence": "ssh.output.output_preview、session.shell_respawned.respawns",
    "problem": "HCI会话超时断开",
    "stage": "S4",
    "tone": "warning"
  },
  "LC-HTP-001": {
    "action": "请等待最多10分钟或点「重新核验归档」，不要重新收集",
    "admin_detail": "P1 UploadComplete 与 5888 发布规范归档名之间存在竞态（现场实测约 31 秒）；verify-archive 返回 archive_missing 只写快照不写终态，等待窗口内结论应为 pending。",
    "component": "htp",
    "evidence": "detail.p1_archive_check.reason=archive_missing / checked_at、case-service P1 归档核验日志",
    "problem": "归档核验未完成，正在等待日志服务器发布归档名",
    "stage": "S6",
    "tone": "info"
  },
  "LC-HTP-002": {
    "action": "请截图本页并联系管理员，附上调用链ID",
    "admin_detail": "error_code / error_message 为空；按 trace_id 串 bridge_execution_logs 与 case-service 日志，禁止编造错误码。",
    "component": "htp",
    "evidence": "trace_id、detail.write_audit",
    "problem": "平台未给出可识别的错误码",
    "stage": "S8",
    "tone": "warning"
  },
  "LC-HTP-003": {
    "action": "请刷新页面后重试；若持续失败请截图本页并联系管理员",
    "admin_detail": "客户端 GET /log-collect/tasks 或 /progress 失败；按 trace_id 查 case-service 与网关日志，重点排除 502/503/504「结果未知」，不要把一次轮询失败写成任务终态。",
    "component": "htp",
    "evidence": "case-service 访问日志、网关状态码、trace_id",
    "problem": "任务状态或进度查询失败",
    "stage": "S8",
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
