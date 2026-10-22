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

export interface LogCollectTaskCreate {
  case_id: string
  ticket_no: string
  origin_node_ip: string
  modules: LogCollectModule[]
  date_from: string
  date_to: string
  ips: string[]
  extra_paths?: string[]
  remote_url?: string
  trace_id?: string
}

export interface LogCollectTask {
  task_id: string
  case_id: string
  ticket_no: string
  origin_node_ip: string
  modules: string[]
  date_from: string
  date_to: string
  ips: string[]
  status: LogCollectStatus
  progress: number
  archive_path?: string | null
  archive_size?: number | null
  archive_sha256?: string | null
  cleanup_state: 'none' | 'required' | 'acked'
  cleanup_reason?: string | null
  archive_deleted: boolean
  relay_status: RelayStatus
  relay_chunk_index?: number | null
  relay_chunk_total?: number | null
  relay_restarts: number
  error_code?: string | null
  error_message?: string | null
}

export interface UploadInitResult {
  upload_id: string
  upload_token: string
  chunk_size: number
  part_count: number
  total_size: number
  uploaded_parts: number[]
  missing_parts: number[]
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
  relay_updated_at?: string | null
  archive_path?: string | null
}

export const logCollectApi = {
  async createTask(payload: LogCollectTaskCreate): Promise<LogCollectTask> {
    const { data } = await client.post('/log-collect/tasks', payload)
    return data
  },

  async listTasks(caseId: string, limit = 20): Promise<{ total: number; items: LogCollectTask[] }> {
    const { data } = await client.get('/log-collect/tasks', { params: { case_id: caseId, limit } })
    return data
  },

  async getTask(taskId: string): Promise<LogCollectTask> {
    const { data } = await client.get(`/log-collect/tasks/${taskId}`)
    return data
  },

  async patchTask(taskId: string, patch: Partial<LogCollectTask>): Promise<LogCollectTask> {
    const { data } = await client.patch(`/log-collect/tasks/${taskId}`, patch)
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
