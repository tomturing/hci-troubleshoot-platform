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
  /** 工单号只有一套：省略时服务端自动取 case_id；传了不一致的值会被 422 拒绝 */
  ticket_no?: string
  origin_node_ip: string
  modules: LogCollectModule[]
  date_from: string
  date_to: string
  ips: string[]
  extra_paths?: string[]
  remote_url?: string
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
}

export interface LogCollectTask {
  task_id: string
  case_id: string
  origin_node_ip: string
  modules: string[]
  date_from: string
  date_to: string
  ips: string[]
  status: LogCollectStatus
  progress: number
  /** P1 直传时创建即带上传地址；老记录没有 upload_mode，用它兜底判定交付路径 */
  remote_url?: string | null
  arch?: string | null
  acli_version?: string | null
  /** 交付路径：acli_direct = P1 直传日志服务器；bridge_relay = P2 经平台中转 */
  upload_mode?: 'acli_direct' | 'bridge_relay' | 'none' | null
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
  /** 任务附带的结构化信息，含 baseline_report_hash（下发锚定的报告指纹） */
  detail?: Record<string, unknown> | null
  created_at?: string | null
  updated_at?: string | null
  /** 终态落库时间：完成态展示"什么时候收完的"必须用它，不能用本地内存时间 */
  finished_at?: string | null
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

export interface ResolveCaseResult {
  case_id: string
  created: boolean
  /** 该工单是否属于当前身份；false 时日志回传与录像回放会被归属校验拒绝 */
  owned?: boolean
}

/** 409 时服务端附带的活跃任务摘要（跨客户端已脱敏） */
export interface ActiveTaskSummary {
  task_id: string
  status: LogCollectStatus
  progress: number
  created_by_me: boolean
  created_at: string | null
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
