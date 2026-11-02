/**
 * 工单详情的「证据聚合」API。
 *
 * 与 `caseApi.getByIdAdmin` 的分工：
 *  - 工单表回答「这单是什么」（状态/标题/客户）；
 *  - 这里回答「这单采了什么、AI 怎么诊断的」，数据来自 log_collect_task、
 *    bridge_execution_logs 与 dsh_diagnosis_session 三张表。
 *
 * 类型刻意写得比后端宽松（大量可空）：这些记录跨多个版本，历史任务缺字段是常态，
 * 前端不能因为某个字段为 null 就整块渲染失败。
 */
import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

const client = createApiClient('/api', undefined, getAccessToken)

export interface ScenarioLabel {
  value: string
  label: string
  summary?: string | null
  duration_hint?: string | null
}

export interface LogCollectProbe {
  acli_version?: string | null
  acli_build_time?: string | null
  architecture?: string | null
  hosts_count?: number | null
  catalog_source?: string | null
  log_server_reachable?: boolean | null
  http_code?: number | null
  latency_ms?: number | null
  available_mb?: number | null
  log_server_url?: string | null
  note?: string | null
  truncated?: boolean
  [key: string]: unknown
}

export interface LogCollectTaskEvidence {
  task_id: string
  status: string
  status_label: string
  progress: number
  upload_mode: string | null
  upload_mode_label: string | null
  delivery_status: string | null
  delivery_status_label: string | null
  integrity_state: string | null
  created_at: string | null
  updated_at: string | null
  finished_at: string | null
  created_by: string | null
  trace_id: string | null
  origin_node_ip: string | null
  modules: ScenarioLabel[]
  date_from: string | null
  date_to: string | null
  day_span: number | null
  ips: string[]
  extra_paths: string[]
  remote_url: string | null
  arch: string | null
  acli_version: string | null
  probe: LogCollectProbe | null
  recommended_path: { value: string; label: string; reason: string }
  baseline_report_hash: string | null
  delivery_evidence: string | null
  delivery_evidence_note: string | null
  archive: { path: string | null; size: number | null; sha256: string | null }
  object: { key: string | null; size: number | null; sha256: string | null }
  delivery: {
    attempts: number | null
    started_at: string | null
    finished_at: string | null
    error: string | null
  }
  relay: {
    status: string | null
    chunk_index: number | null
    chunk_total: number | null
    chunk_size: number | null
    restarts: number | null
    attempt: number | null
    last_error: string | null
  }
  cleanup: { state: string | null; reason: string | null }
  error: { code: string | null; message: string | null }
}

export interface LogCollectProbeEvent {
  created_at: string | null
  event: string
  level: string | null
  extra: Record<string, unknown> | null
}

export interface LogCollectCaseEvidence {
  case_id: string
  case_title: string | null
  case_status: string | null
  case_created_at: string | null
  case_trace_id: string | null
  case_source: string | null
  task_total: number
  tasks: LogCollectTaskEvidence[]
  probes: LogCollectProbeEvent[]
}

export interface DshToolCall {
  name: string
  arguments: string
}

export interface DshMessage {
  seq?: number | null
  role: 'user' | 'assistant' | 'tool' | 'system'
  text: string
  name?: string
  is_error?: boolean
  tool_calls?: DshToolCall[]
  time?: string | null
}

export interface DshSession {
  session_id: string
  case_id: string | null
  title: string | null
  workspace: string | null
  agent_preset: string | null
  started_at: string | null
  last_activity_at: string | null
  message_count: number
  tool_call_count: number
  conclusion_markdown: string | null
  messages: DshMessage[]
  truncated: boolean
  binding_source: string
  source_path: string
  source_size: number | null
  source_mtime: string | null
  ingested_at: string | null
}

export interface DshSessionListResponse {
  case_id: string
  total: number
  ingest: {
    scanned: number
    parsed: number
    skipped: number
    failed: number
    unbounded: number
    root_missing: boolean
  }
  items: DshSession[]
}

/** 日志收集证据（用户选项 + 元数据 + 交付事实）。 */
export async function getCaseLogCollect(caseId: string, limit = 20): Promise<LogCollectCaseEvidence> {
  const { data } = await client.get(`/cases/admin/${encodeURIComponent(caseId)}/log-collect`, {
    params: { limit },
  })
  return data as LogCollectCaseEvidence
}

/**
 * DSH 诊断会话。
 *
 * 超时单列 90s：首次访问会触发服务端增量解压会话文件（多帧 zstd，单会话可达数 MB），
 * 用 axios 默认超时会把「正在解析」误报成失败。
 */
export async function getCaseDshSessions(
  caseId: string,
  options: { limit?: number; refresh?: boolean } = {},
): Promise<DshSessionListResponse> {
  const { data } = await client.get(`/cases/admin/${encodeURIComponent(caseId)}/dsh-sessions`, {
    params: { limit: options.limit ?? 20, refresh: options.refresh ?? true },
    timeout: 90_000,
  })
  return data as DshSessionListResponse
}
