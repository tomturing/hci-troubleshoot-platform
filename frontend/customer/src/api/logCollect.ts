/**
 * 日志收集（acli log_collect）控制面 API。
 *
 * 所有请求经 API Gateway，身份由同源身份层注入（与服务端 `get_client_id` 一致）。
 * 数据面分片上传**不在这里**：20 GiB 分片由 terminal_bridge 直连
 * `/api/direct/log-collect-uploads/...`，前端只负责 init/complete 与进度展示。
 */
import { createApiClient } from '@hci/shared'

const client = createApiClient('/api')

export type LogCollectModule =
  | 'other'
  | 'system'
  | 'slowdisk'
  | 'network'
  | 'storage'
  | 'vm_start'
  | 'vm_suspend'
  | 'host_offline'
  | 'performance'
  | 'core'

export type LogCollectStatus =
  | 'pending'
  | 'probing'
  | 'installing'
  | 'collecting'
  | 'packaging'
  | 'uploading'
  | 'completed'
  | 'failed'
  | 'cancelled'

export type RelayStatus = 'none' | 'pending' | 'running' | 'success' | 'failed'

/** 默认场景清单只读预览：客户侧只展示路径，不下发可执行条目。 */
export interface LogCollectDefaultManifest {
  module: string
  label: string
  customized: boolean
  revision: number
  item_count: number
  sha256: string
  preview: Array<{ path: string; category: string; note?: string | null }>
}

/** 探测历史条目（bridge 事件 extra 为自由结构，按需读取已知字段）。 */
export interface ProbeHistoryItem {
  created_at: string
  event: string
  level: string
  extra: Record<string, unknown>
}

/** 平台侧产物索引：object_key 为空即"平台没留副本"（P1）。 */
export interface LogCollectArtifact {
  task_id: string
  upload_mode: string | null
  object_key: string | null
  object_size: number | null
  object_sha256: string | null
  archive_size: number | null
  delivery_status: string
  created_at: string
}

/**
 * 交付状态：描述「产物是否真的从节点到达了日志服务器」。
 *
 * 与采集 `status` 正交，不能互相替代：`status=completed` 只说明 acli 报采集结束，
 * 平台可能一个字节都没收到（2026-10-27 现场即 completed + 零产物）。
 */
export type DeliveryStatus =
  | 'none'
  | 'queued'
  | 'pulling'
  | 'uploading'
  | 'relaying'
  | 'delivered'
  | 'failed'

export interface LogCollectTaskCreate {
  case_id: string
  /** 工单号只有一套：省略时服务端自动取 case_id；传了不一致的值会被 422 拒绝 */
  ticket_no?: string
  origin_node_ip: string
  modules: LogCollectModule[]
  date_from: string
  date_to: string
  ips: string[]
  extra_paths?: string[]
  /**
   * 选用的自定义场景 ID（acli `-c`）。
   * 服务端按它取最新 revision、展开 `{date}`、合并 extra_paths 后生成配置；
   * 前端不构造 config，只把创建响应里的 config_json 透传给 bridge。
   */
  custom_scenario_id?: string
  remote_url?: string
  /**
   * 交付路径（创建时确定，创建即落库）：
   * - `acli_direct`（P1）：acli 带 `-u` 直传日志服务器，必须同时给 `remote_url`；
   * - `bridge_relay`（P2）：由客户机经平台中转，不得给 `remote_url`。
   *
   * 省略时服务端按 `remote_url` 是否提供推断（兼容未升级客户端）。
   */
  upload_mode?: 'acli_direct' | 'bridge_relay'
  /**
   * 下发前锚定的节点报告指纹。
   *
   * acli 的 progress 是 ticket 维度：同一工单重复 create 时，头几次仍会打印
   * 上一次任务的报告。把"下发前那一份"存进任务，之后比对即可判断报告归属，
   * 无需依赖压缩包是否还存在（P1 成功后插件会自删包）。
   */
  baseline_report_hash?: string
  trace_id?: string
  /** 发起节点架构（probe 实测）：随任务落库，失败后可自证运行环境 */
  arch?: string
  /** 发起节点 acli 版本（probe 实测）：与 arch 一同回答「哪版 acli 失败」 */
  acli_version?: string
  /**
   * 下发前的探测快照。
   *
   * 失败任务事后要能自证"当时那台机器是什么状态"：节点清单为什么是空的、
   * 日志服务器通不通、命中的哪份 acli。只放在前端内存里，关面板或换端即丢失。
   */
  probe?: {
    acli_version?: string
    acli_build_time?: string
    acli_binary?: string
    architecture?: string
    acli_installed?: boolean
    hosts_count?: number
    modules_count?: number
    catalog_source?: string
    catalog_note?: string
    log_server_reachable?: boolean
    http_code?: number
    /**
     * 节点 acli 是否支持注入上传请求头（`-L|--url-header`）。
     *
     * 这是 P1 能否带凭据上传的节点侧前提：不支持就只能走 P2。
     * 判据取 acli help 原文而非版本号——同版本号的不同构建参数集并不一致。
     */
    acli_supports_upload_header?: boolean
    note?: string
  }
}

/** P1 归档核验结论：由平台回查日志服务器得出，不是节点自述。 */
export interface P1ArchiveVerifyResult {
  task_id: string
  verified: boolean
  /** verified=false 时给出原因：archive_missing / size_mismatch / stale_archive 等 */
  reason: string
  filename: string
  expected_size?: number | null
  archived_size?: number | null
  archived_at?: string | null
  target_path?: string | null
  detail?: Record<string, unknown>
}

/** 三段五组件里的单个组件（标签与段来自契约）。 */
export interface LogCollectStageComponentView {
  key: string
  label: string
  tag: string
  scope: string
  scope_label: string
}

/** S1-S8 阶段时间线：与横幅、管理端详情同源。 */
export interface LogCollectStageView {
  key: string
  label: string
  scopes: string[]
  scope_label: string
  components: LogCollectStageComponentView[]
  status: string
  status_label: string
  criteria: string
  fault_code: string | null
  customer_text: string | null
}

/** 首个异常点：客户只看 customer_text，其余字段供管理端判读。 */
export interface LogCollectFaultView {
  code: string
  scope: string
  scope_label: string
  component: string
  component_label: string
  component_tag: string
  stage: string
  stage_label: string
  problem: string
  action: string
  customer_text: string
  admin_detail: string
  evidence: string
  trace_id: string | null
}

/** 平台派生的三维状态：界面只读，不得自行用 status/progress 拼结论。 */
export interface LogCollectDisplayView {
  phase: string
  phase_label: string
  conclusion: string
  conclusion_label: string
  conclusion_tone: string
  conditions: Array<{ key: string; label: string }>
  progress_percent: number | null
  upload_mode_label?: string | null
  stages?: LogCollectStageView[]
  fault_attribution?: LogCollectFaultView | null
  /** 唯一调用链：客户截图反馈时随文案一起给出 */
  trace_id?: string | null
}

export interface ArtifactEvidence {
  quality: string
  quality_label: string
  reason: string
  reason_text: string
  size: number | null
  size_text: string
  entry_count: number | null
  entry_total_bytes: number | null
  entry_total_text: string | null
  source: string
}

export interface TransferEvidence {
  stage: string | null
  stage_label: string | null
  bytes_done: number | null
  bytes_total: number | null
  percent: number | null
  elapsed_ms: number | null
  rate_bps: number | null
  rate_text: string | null
  eta_sec: number | null
  stalled: boolean
  stalled_seconds: number | null
  stall_threshold_seconds: number
  updated_at: string | null
  source: string | null
}

export interface NetworkEvidence {
  grade: string
  grade_label: string
  relevant_target: string | null
  targets: Array<{
    target: string
    grade: string
    grade_label: string
    reachable: boolean | null
    rtt_avg_ms: number | null
    jitter_ms: number | null
    loss_rate: number | null
    up_text: string | null
    down_text: string | null
    advice: string | null
    checked_at: string | null
  }>
  checked_at: string | null
}

/** 节点观测事实（与结论分离）：平台按证据推导 status/结论。 */
export interface NodeObservedFacts {
  status?: LogCollectStatus
  progress?: number
  arch?: string
  acli_version?: string
  archive_path?: string
  archive_size?: number
  archive_sha256?: string
  error_code?: string
  error_message?: string
  detail?: Record<string, unknown>
  config_path?: string
  preflight_missing?: string[]
  spc_slot?: string
  spc_sha256?: string
  spc_hosts?: Array<{ ip: string; backup?: string; error?: string }>
  spc_slots?: string[]
  restore_status?: string
}

export type LogCollectTaskPatch = Partial<LogCollectTask> & { node_observed?: NodeObservedFacts }

export interface LogCollectTask {
  task_id: string
  case_id: string
  origin_node_ip: string
  modules: string[]
  date_from: string
  date_to: string
  ips: string[]
  /**
   * 额外收集路径（acli `-p`）。
   *
   * 平台在 create 时落库，接管历史任务与「重新收集」都要靠它还原
   * "这次到底多收了什么"；缺失时按空数组处理（老任务没有这个字段）。
   */
  extra_paths?: string[] | null
  /**
   * 自定义场景（acli -c）事实。config_json 只在创建响应里出现，
   * 平台不留副本；bridge 写入节点后由 create 回执补 config_path 与预检结论。
   */
  custom_scenario_id?: string | null
  custom_scenario_revision?: number | null
  config_json?: string | null
  config_sha256?: string | null
  path_count?: number | null
  config_path?: string | null
  preflight_missing?: string[] | null
  /** spc 场景：原文只在创建响应出现；回执补 slot/sha/hosts/restore。 */
  spc_json?: string | null
  spc_sha256?: string | null
  spc_slot?: string | null
  spc_hosts?: Array<{ ip: string; backup?: string; error?: string }> | null
  /** 默认场景多槽位：原文只在创建响应出现，槽位集合用于兜底还原。 */
  spc_files?: Array<{ slot: string; content: string; sha256: string }> | null
  spc_slots?: string[] | null
  restore_status?: string | null
  status: LogCollectStatus
  progress: number
  /** P1 直传时创建即带上传地址；老记录没有 upload_mode，用它兜底判定交付路径 */
  remote_url?: string | null
  arch?: string | null
  acli_version?: string | null
  /** 交付路径：acli_direct = P1 直传日志服务器；bridge_relay = P2 经平台中转 */
  upload_mode?: 'acli_direct' | 'bridge_relay' | 'none' | null
  archive_path?: string | null
  /**
   * 节点上采集包的绝对路径，仅当服务端在桥日志里核到节点侧证据时才返回。
   * P2 下 `archive_path` 是平台对象 key，**不能**用于清理命令。
   */
  node_archive_path?: string | null
  archive_size?: number | null
  archive_sha256?: string | null
  /** 端到端完整性：verified = 源包摘要与平台副本一致；unverified = 无基线，仅传输层校验 */
  integrity_state?: 'verified' | 'unverified'
  cleanup_state: 'none' | 'required' | 'acked'
  cleanup_reason?: string | null
  archive_deleted: boolean
  relay_status: RelayStatus
  /** 交付状态（与 status 正交）：判断"采完了但没传上去"的唯一权威字段 */
  delivery_status?: DeliveryStatus | null
  delivery_error?: string | null
  /**
   * P1 直传票据：只在创建响应里下发，且只在平台已配置票据密钥时存在。
   *
   * 它是 per-task 短期凭据，必须透传给 bridge 注入成 acli 的
   * `-L 0:Authorization:Bearer <ticket>`。为空说明这次直传带不出凭据，
   * 应当走 P2——不要用空票据去发 P1，那只会换来一次 401。
   */
  p1_ticket?: string | null
  /** 票据过期时间（Unix 秒）；过期后需要重新创建任务才能再走 P1 */
  p1_ticket_expires_at?: number | null
  relay_chunk_index?: number | null
  relay_chunk_total?: number | null
  relay_restarts: number
  error_code?: string | null
  error_message?: string | null
  /** 任务附带的结构化信息，含 baseline_report_hash（下发锚定的报告指纹） */
  detail?: Record<string, unknown> | null
  created_at?: string | null
  updated_at?: string | null
  /** 终态落库时间：完成态展示"什么时候收完的"必须用它，不能用本地内存时间 */
  finished_at?: string | null
  /** 以下四项均由平台派生，界面不得自行用 status/progress/archive_size 重算。 */
  display?: LogCollectDisplayView | null
  artifact?: ArtifactEvidence | null
  transfer?: TransferEvidence | null
  network?: NetworkEvidence | null
  /** 只在创建响应出现：透传给 bridge 用于上报证据，绑定 task_id、24h。 */
  evidence_ticket?: string | null
}

export interface UploadInitResult {
  upload_id: string
  upload_token: string
  chunk_size: number
  part_count: number
  total_size: number
  /** 会话代际：服务端签发时写入，旧代令牌在新会话里自动失效。 */
  delivery_epoch?: number
  uploaded_parts: number[]
  missing_parts: number[]
}

/**
 * 补交付的签发结果。
 *
 * `already_delivered` 必须单独暴露：服务端对已交付任务做幂等短路时不会再开会话，
 * 前端若只看 HTTP 200 就会误报"上传成功"，而用户真正想问的是"这次到底传没传"。
 */
export interface DeliverResult {
  already_delivered: boolean
  delivery_status: string
  delivery_attempts: number
  upload: UploadInitResult
  source_path?: string | null
}

export interface UploadCompleteResult {
  upload_id: string
  object_key: string
  size: number
  sha256: string
  part_count: number
}

export interface RelaySnapshot {
  task_id: string
  relay_status: RelayStatus
  relay_upload_id?: string | null
  relay_chunk_size?: number | null
  relay_chunk_index?: number | null
  relay_chunk_total?: number | null
  relay_restarts: number
  /** 当前（或最近一次）整轮尝试序号；>1 表示已经重试过。 */
  relay_attempt?: number | null
  /** 最近一次失败原因（含瞬时失败）；成功后后端会清空。 */
  relay_last_error?: string | null
  relay_last_error_at?: string | null
  relay_updated_at?: string | null
  /** running 且心跳过期：说明执行进程已死（发布/重启），可强制续跑。 */
  stale?: boolean
  archive_path?: string | null
}

export interface ResolveCaseResult {
  case_id: string
  created: boolean
  /** 该工单是否属于当前身份；false 时日志回传与录像回放会被归属校验拒绝 */
  owned?: boolean
  /** 归属不同但可显式认领（日志收集壳工单：无会话无消息、无在途交付、原身份空闲） */
  claimable?: boolean
  /** 不可认领的原因码，见 CLAIM_BLOCKED_REASON_TEXT */
  claim_blocked_reason?: string | null
  /** 原身份最后一次采集活动距今秒数 */
  owner_active_seconds?: number | null
}

export interface ClaimCaseResult {
  case_id: string
  owned: boolean
  claimed: boolean
  already_owned: boolean
  /** 随工单一起转移到当前身份的采集任务数 */
  tasks_moved: number
}



/** 409 时服务端附带的活跃任务摘要（跨客户端已脱敏） */
export interface ActiveTaskSummary {
  task_id: string
  status: LogCollectStatus
  progress: number
  created_by_me: boolean
  created_at: string | null
}

/**
 * 「故障场景」字典项。
 *
 * 为什么要单独一层：acli 只告诉我们 `-m` 支持哪些枚举值，不解释每个专项到底收什么；
 * 只靠短名（"中台专项"、"卡慢盘专项"），用户无法判断该选哪个也不了解多选含义。
 * 字典只补展示层事实，**不改变能力边界**——能不能选，仍然取决于集群 acli 的枚举。
 */
/**
 * 客户可见的自定义场景：**只有元数据**。
 *
 * 平台刻意不下发 items（路径清单）：它既是客户目录结构的信息泄露面，
 * 也是前端可篡改的注入面。客户判断"该不该选"靠名称、释义与条数即可。
 */
export interface LogCollectCustomScenarioItem {
  scenario_id: string
  revision: number
  label: string
  summary: string | null
  item_count: number
}

export interface LogCollectScenarioItem {
  value: string
  label: string
  /** 一句话释义，直接显示在勾选项第二行 */
  summary?: string | null
  /** 该专项实际采集的内容/范围 */
  collects?: string | null
  /** 典型症状，帮用户按现象反选 */
  typical_symptoms?: string | null
  /** 耗时提示（不同专项差异大） */
  duration_hint?: string | null
  /** 与其他场景互斥（字典能力，当前无项启用）：true 时客户端把其余选项双向置灰 */
  exclusive: boolean
  sort_order: number
  enabled: boolean
}

export const logCollectApi = {
  /**
   * 按工单号解析日志收集的目标工单：已存在则复用，不存在则按该号建档。
   * 工单格式由服务端强校验（Q + 8 位日期 + 5 位序号）。
   */
  async resolveCase(caseId: string): Promise<ResolveCaseResult> {
    const { data } = await client.post('/cases/resolve', { case_id: caseId })
    return data
  },

  /**
   * 认领日志收集壳工单：把工单归属与名下采集任务转到当前身份。
   *
   * 为什么是显式动作而不是自动接管：client_id 是浏览器会话身份而非人，自动转移
   * 等于"知道工单号就拥有它"，既无审计也绕过越权边界。认领是用户明确表达的意图。
   */
  async claimCase(caseId: string): Promise<ClaimCaseResult> {
    const { data } = await client.post(`/cases/${caseId}/claim`)
    return data
  },

  async createTask(payload: LogCollectTaskCreate): Promise<LogCollectTask> {
    const { data } = await client.post('/log-collect/tasks', payload)
    return data
  },

  /**
   * 拉取「故障场景」字典（只含已启用项）。
   *
   * 失败必须由调用方降级：字典挂了不影响探测与采集，面板回落到 bridge 短名。
   */
  async listScenarios(): Promise<LogCollectScenarioItem[]> {
    const { data } = await client.get('/log-collect/scenarios')
    return data?.scenarios ?? []
  },

  /**
   * 自定义场景列表（只含已启用项与元数据）。
   * 失败时必须降级为空数组：字典服务挂了不应挡住默认场景采集。
   */
  async listCustomScenarios(): Promise<LogCollectCustomScenarioItem[]> {
    const { data } = await client.get('/log-collect/custom-scenarios')
    return data?.scenarios ?? []
  },

  /**
   * 默认场景清单只读预览。
   * 失败必须降级为空：清单服务挂了不应挡住原生默认场景采集。
   */
  async listDefaultManifests(): Promise<LogCollectDefaultManifest[]> {
    try {
      const { data } = await client.get('/log-collect/default-scenario-manifests')
      return data?.manifests ?? []
    } catch {
      return []
    }
  },

  async listTasks(caseId: string, limit = 20): Promise<{ total: number; items: LogCollectTask[] }> {
    const { data } = await client.get('/log-collect/tasks', { params: { case_id: caseId, limit } })
    return data
  },

  async getTask(taskId: string): Promise<LogCollectTask> {
    const { data } = await client.get(`/log-collect/tasks/${taskId}`)
    return data
  },

  /** 探测历史：节点清单/日志服务器失败原因可回看（数据源是 bridge 事件表）。 */
  async listProbes(caseId: string, limit = 10): Promise<{ items: ProbeHistoryItem[] }> {
    const { data } = await client.get('/log-collect/tasks/probes', {
      params: { case_id: caseId, limit },
    })
    return data
  },

  /** 平台产物索引：判断"这单平台有没有留包"（P1 无对象，P2 才有）。 */
  async listArtifacts(caseId: string, limit = 20): Promise<{ items: LogCollectArtifact[] }> {
    const { data } = await client.get('/log-collect/tasks/artifacts', {
      params: { case_id: caseId, limit },
    })
    return data
  },

  async patchTask(taskId: string, patch: LogCollectTaskPatch): Promise<LogCollectTask> {
    const { data } = await client.patch(`/log-collect/tasks/${taskId}`, patch)
    return data
  },

  /**
   * 核验 P1 直传是否真的落到日志服务器。
   *
   * 为什么必须由服务端做：P1 链路上平台没有源包副本，节点报"发送成功"是唯一
   * 证据；而插件在上传成功后会自删压缩包，一旦判错就永久不可恢复。
   * 归档件是否存在、多大、什么时候落地，只能在落盘侧回答。
   */
  async verifyP1Archive(
    taskId: string,
    archiveSize?: number | null,
  ): Promise<P1ArchiveVerifyResult> {
    const { data } = await client.post(`/log-collect/tasks/${taskId}/verify-archive`, {
      archive_size: archiveSize ?? null,
    })
    return data
  },

  /**
   * 登记「节点报告 P1 直传成功」（弱证据）：由服务端判定 upload_state 后权威落库。
   *
   * 为什么不直接 PATCH delivery_status：交付终态是单一权威字段，公共 PATCH 里的
   * 该字段会被服务端静默忽略（single-writer，Q2026110601054）。P1 的包在上传成功后
   * 被插件自删，判成"未交付"会让用户点重传必然失败（Q2026102701193），
   * 所以弱证据必须有服务端受理入口，并打上 acli_self_reported 供事后区分。
   */
  async attestP1Delivered(
    taskId: string,
    body: { upload_state?: string | null; archive_size?: number | null },
  ): Promise<LogCollectTask> {
    const { data } = await client.post(`/log-collect/tasks/${taskId}/p1-delivered`, {
      upload_state: body.upload_state ?? null,
      archive_size: body.archive_size ?? null,
    })
    return data
  },

  async cancelTask(taskId: string, reason?: string): Promise<LogCollectTask> {
    const { data } = await client.post(`/log-collect/tasks/${taskId}/cancel`, null, {
      params: reason ? { reason } : undefined,
    })
    return data
  },

  async initUpload(taskId: string, body: { total_size: number; expected_sha256?: string }): Promise<UploadInitResult> {
    const { data } = await client.post(`/log-collect/tasks/${taskId}/uploads`, body)
    return data
  },

  /**
   * 补交付：为节点上已有的包签发上传会话。
   *
   * 与 `initUpload` 的差别在于它**不要求任务处于某个采集状态**，且由服务端负责
   * 处理会话大小不一致（自动 abort 重建）。前端只需要给出"包多大、在哪"这两个
   * 物理事实——它们来自 bridge 的 stat，平台无法自行获知。
   */
  async deliverTask(
    taskId: string,
    body: {
      total_size: number
      source_path?: string
      expected_sha256?: string
      force?: boolean
      /** 内容指纹：识别"同名包被重新采集覆盖"，服务端据此自动重建会话。 */
      archive_fingerprint?: string
    },
  ): Promise<DeliverResult> {
    const { data } = await client.post(`/log-collect/tasks/${taskId}/deliver`, body)
    return data
  },

  /**
   * 查询上传会话（断点续传/重试前刷新用）。
   *
   * 返回体包含**重新签发**的 `upload_token`：续传重提时必须带上它。
   * 该字段曾经缺失，导致"证书失败→确认信任→重试"这条路必然带空令牌提交
   * （bridge 回 `task_id 与 upload_token 必填`），现已由服务端补齐。
   */
  async getUploadStatus(taskId: string): Promise<UploadInitResult> {
    const { data } = await client.get(`/log-collect/tasks/${taskId}/uploads`)
    return data
  },

  async completeUpload(
    taskId: string,
    body: { parts?: number[]; sha256?: string; size?: number },
  ): Promise<UploadCompleteResult> {
    // 合并 20 GiB 需要读全部分片，30s 默认超时不够。
    const { data } = await client.post(`/log-collect/tasks/${taskId}/uploads/complete`, body, { timeout: 600000 })
    return data
  },

  async abortUpload(taskId: string): Promise<void> {
    await client.post(`/log-collect/tasks/${taskId}/uploads/abort`)
  },

  async startRelay(taskId: string, force = false): Promise<{ started: boolean; relay_status: RelayStatus }> {
    const { data } = await client.post(`/log-collect/tasks/${taskId}/relay`, null, { params: { force } })
    return data
  },

  async getRelay(taskId: string): Promise<RelaySnapshot> {
    const { data } = await client.get(`/log-collect/tasks/${taskId}/relay`)
    return data
  },
}
