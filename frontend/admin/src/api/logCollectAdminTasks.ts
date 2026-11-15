/**
 * 管理端「日志上传」只读 API。
 *
 * 两条硬规则：
 * 1. 列表接口不查 5888，真实进度走单独的 destination-progress 端点；
 *    前端只对「进行中」的行以 30s 轮询，避免 N×轮询打爆单进程日志服务器。
 * 2. 全只读：本文件不导出任何写方法，管理端不允许重新核验/终止/重传/导出。
 *
 * 结论口径：display/artifact/transfer/network 全部由 case-service 派生，
 * 前端不重新计算 status，也不自己拼「已完成」。缺失项一律显示「—」或
 * 「桥接端版本较旧」，绝不拿 0/100 顶替。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

const client = createApiClient('/api/v1', undefined, getAccessToken)
const BASE = '/log-collect/admin/tasks'

export interface AdminTaskFilters {
  page?: number
  page_size?: number
  days?: number
  case_id?: string
  node_ip?: string
  upload_mode?: string
  status?: string
  delivery_status?: string
}

/** 五桶之和必须等于 total（后端模型校验器已强制）；未知口径时桶为空对象。 */
export interface ConclusionBuckets {
  pending?: number
  success?: number
  partial?: number
  /** 交付已登记但平台零字节：可续传的中间态，既不是失败也不是已交付 */
  unfinished?: number
  failed?: number
  cancelled?: number
}

export interface PathSummary {
  total: number
  transferring: number
  delivered: number
  unverified: number
  failed: number
  by_conclusion?: ConclusionBuckets
}

export interface AdminTaskSummary extends PathSummary {
  p1: PathSummary
  p2: PathSummary
}

export interface DisplayCondition {
  key: string
  label: string
}

/** 三段五组件里的单个组件；标签与段来自契约生成物。 */
export interface AdminStageComponent {
  key: string
  label: string
  tag: string
  scope: string
  scope_label: string
}

/** S1-S8 阶段：与客户横幅、阶段条同源。 */
export interface AdminStageView {
  key: string
  label: string
  scopes: string[]
  scope_label: string
  components: AdminStageComponent[]
  status: string
  status_label: string
  criteria: string
  fault_code: string | null
  customer_text: string | null
}

/** 首个异常点：管理员用来判断真实链路问题出在哪一段。 */
export interface AdminFaultAttribution {
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

export interface LogCollectDisplay {
  phase: string
  phase_label: string
  conclusion: string
  conclusion_label: string
  conclusion_tone: string
  conditions: DisplayCondition[]
  progress_percent: number | null
  upload_mode_label?: string | null
  stages?: AdminStageView[]
  fault_attribution?: AdminFaultAttribution | null
  trace_id?: string | null
}

export interface ArtifactInfo {
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

export interface TransferInfo {
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

export interface NetworkTarget {
  target: string
  reachable: boolean | null
  grade: string
  grade_label: string
  rtt_avg_ms: number | null
  jitter_ms: number | null
  loss_rate: number | null
  http_code: number | null
  up_bps: number | null
  down_bps: number | null
  up_text: string | null
  down_text: string | null
  advice: string | null
  checked_at: string | null
}

export interface NetworkInfo {
  grade: string
  grade_label: string
  relevant_target: string | null
  targets: NetworkTarget[]
  checked_at: string | null
}

export interface AdminLogCollectTask {
  task_id: string
  case_id: string
  client_id: string | null
  /** 来自 case.customer_id → customer.short_name/name；未关联客户时为 null。 */
  customer_name: string | null
  origin_node_ip: string | null
  upload_mode: string | null
  status: string
  progress: number
  archive_size: number | null
  object_size: number | null
  delivery_status: string
  relay_status: string
  relay_upload_id: string | null
  integrity_state: string
  archive_check: Record<string, unknown> | null
  error_code: string | null
  error_message: string | null
  trace_id: string | null
  created_at: string | null
  finished_at: string | null
  delivery_finished_at: string | null
  /** 结构化原文（逐台明细、归因来源、写审计），只在详情接口返回。 */
  detail?: Record<string, unknown> | null
  /** 老后端/未升级 bridge 可能没有这些字段，界面必须能降级。 */
  display?: LogCollectDisplay | null
  artifact?: ArtifactInfo | null
  /** 本页批量取回的最新样本；老 bridge 不上报时为 null。 */
  transfer?: TransferInfo | null
  network?: NetworkInfo | null
}

export interface AdminTaskDetail extends AdminLogCollectTask {
  transfer?: TransferInfo | null
  network?: NetworkInfo | null
  archive_path?: string | null
  archive_sha256?: string | null
  acli_version?: string | null
  arch?: string | null
}

export interface AdminTaskList {
  items: AdminLogCollectTask[]
  total: number
  page: number
  page_size: number
  summary: AdminTaskSummary
}

/** state: done/uploading/no_record/unsupported/unavailable；「查不到」不是失败。 */
export interface DestinationProgress {
  task_id: string
  path: string
  state: string
  progress: number | null
  uploaded_size: number | null
  file_size: number | null
  received_chunks: number | null
  chunk_total: number | null
  target_path: string | null
  sha256: string | null
  finished_at: string | null
  checked_at: string
  reason: string | null
}

export interface TransferMetricsResponse {
  task_id: string
  items: TransferInfo[]
  latest: TransferInfo | null
}

export interface NetworkProbeResponse {
  task_id: string
  network: NetworkInfo | null
}

export interface ProcessLogItem {
  log_id: string
  host: string
  path: string
  size_bytes: number | null
  size_text: string | null
  sha256: string | null
  truncated: boolean
  status: string
  error: string | null
  collected_at: string | null
}

export interface ProcessLogsResponse {
  task_id: string
  items: ProcessLogItem[]
}

export interface ProcessLogContent {
  log_id: string
  host: string
  path: string
  content: string
  truncated: boolean
  size_bytes: number | null
  collected_at: string | null
}

export interface ArtifactManifestItem {
  source: string
  archive_size: number | null
  archive_size_text: string | null
  archive_sha256: string | null
  entry_count: number | null
  entry_total_bytes: number | null
  entry_total_text: string | null
  has_encrypted: boolean | null
  quality: string
  quality_label: string
  reason: string | null
  reason_text: string | null
  entries: Array<Record<string, unknown>> | null
  entries_truncated: boolean
  inspected_at: string | null
}

export interface ArtifactManifestResponse {
  task_id: string
  items: ArtifactManifestItem[]
}

function queryOf(filters: AdminTaskFilters): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  }
  return params.toString()
}

function taskPath(taskId: string, suffix = ''): string {
  return `${BASE}/${encodeURIComponent(taskId)}${suffix}`
}

export async function listAdminTasks(filters: AdminTaskFilters = {}): Promise<AdminTaskList> {
  const query = queryOf(filters)
  const { data } = await client.get(query ? `${BASE}?${query}` : BASE)
  return data
}

export async function getDestinationProgress(taskId: string): Promise<DestinationProgress> {
  const { data } = await client.get(taskPath(taskId, '/destination-progress'))
  return data
}

export async function getAdminTask(taskId: string): Promise<AdminTaskDetail> {
  const { data } = await client.get(taskPath(taskId))
  return data
}

export async function getTransferMetrics(taskId: string, limit = 120): Promise<TransferMetricsResponse> {
  const { data } = await client.get(taskPath(taskId, `/transfer-metrics?limit=${limit}`))
  return data
}

export async function getNetworkProbe(taskId: string): Promise<NetworkProbeResponse> {
  const { data } = await client.get(taskPath(taskId, '/network-probe'))
  return data
}

export async function getProcessLogs(taskId: string): Promise<ProcessLogsResponse> {
  const { data } = await client.get(taskPath(taskId, '/process-logs'))
  return data
}

export async function getProcessLogContent(taskId: string, logId: string): Promise<ProcessLogContent> {
  const { data } = await client.get(taskPath(taskId, `/process-logs/${encodeURIComponent(logId)}/content`))
  return data
}

export async function getArtifactManifest(taskId: string): Promise<ArtifactManifestResponse> {
  const { data } = await client.get(taskPath(taskId, '/artifact-manifest'))
  return data
}
