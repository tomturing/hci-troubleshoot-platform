/**
 * 管理端「日志上传」只读 API。
 *
 * 两条硬规则：
 * 1. 列表接口不查 5888，真实进度走单独的 destination-progress 端点；
 *    前端只对「进行中」的行以 30s 轮询，避免 N×轮询打爆单进程日志服务器。
 * 2. 全只读：本文件不导出任何写方法，管理端不允许重新核验/终止/重传/导出。
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

export interface PathSummary {
  total: number
  transferring: number
  delivered: number
  unverified: number
  failed: number
}

export interface AdminTaskSummary extends PathSummary {
  p1: PathSummary
  p2: PathSummary
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

function queryOf(filters: AdminTaskFilters): string {
  const params = new URLSearchParams()
  for (const [key, value] of Object.entries(filters)) {
    if (value !== undefined && value !== null && value !== '') params.set(key, String(value))
  }
  return params.toString()
}

export async function listAdminTasks(filters: AdminTaskFilters = {}): Promise<AdminTaskList> {
  const query = queryOf(filters)
  const { data } = await client.get(query ? `${BASE}?${query}` : BASE)
  return data
}

export async function getDestinationProgress(taskId: string): Promise<DestinationProgress> {
  const { data } = await client.get(`${BASE}/${encodeURIComponent(taskId)}/destination-progress`)
  return data
}
