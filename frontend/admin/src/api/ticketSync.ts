/**
 * 工单日志同步服务的调用封装。
 *
 * 服务跑在宿主机（systemd + uvicorn :18080），由同源 nginx 的 /ticket-sync/
 * 反代过来，因此这里沿用管理端 JWT：服务本身不校验，但保持与其他管理接口
 * 一致的请求形态，nginx 侧也无需为它开特例。
 */

import { createApiClient } from '@hci/shared'
import { getAccessToken } from '@/utils/auth'

// 与服务端 ^Q\d{13}$ 完全一致：Q + YYYYMMDD + 5 位当天序号，共 14 字符。
export const TICKET_ID_PATTERN = /^Q\d{13}$/

export type SyncPhase = 'queued' | 'fetching' | 'extracting' | 'ready' | 'failed' | 'cancelled'

/** 仍在推进的阶段：轮询与「取消」按钮只在此时有意义。 */
export const RUNNING_PHASES: readonly SyncPhase[] = ['queued', 'fetching', 'extracting']

export interface SyncJob {
  ticketId: string
  phase: SyncPhase
  receivedBytes: number
  totalBytes: number
  extractedBytes: number
  fileCount: number
  currentArchive: string | null
  errorCode: string | null
  message: string | null
  acceptedAt: string
  startedAt: string | null
  updatedAt: string
}

export const PHASE_TEXT: Record<SyncPhase, string> = {
  queued: '排队中',
  fetching: '拉取中',
  extracting: '解压中',
  ready: '已就绪',
  failed: '失败',
  cancelled: '已取消',
}

// 错误码到用户可读文案的映射，与服务端文档第 8 章一一对应。
const ERROR_TEXT: Record<string, string> = {
  'invalid-ticket-id': '工单号格式错误，应为 Q + 13 位数字',
  'not-found': '该工单在日志源服务器上不存在',
  'too-large': '工单包过大，超过单工单上限',
  'bad-password': '归档密码错误，请联系运维',
  'archive-corrupt': '压缩包已损坏',
  'no-space': '服务器磁盘空间不足',
  'quota-exceeded': '解压体积异常，已中止',
  'depth-exceeded': '归档嵌套层级异常',
  'ssh-failed': '无法连接日志源服务器',
  'extract-failed': '解压失败，请查看服务日志',
}

export function errorText(code: string | null, fallback: string | null = null): string {
  if (code && ERROR_TEXT[code]) return ERROR_TEXT[code]
  return fallback || '同步失败，请稍后重试'
}

export function isRunning(phase: SyncPhase | undefined): boolean {
  return phase !== undefined && RUNNING_PHASES.includes(phase)
}

/** 拉取进度百分比；总字节数未知时返回 0，由调用方决定是否展示进度条。 */
export function fetchPercent(job: SyncJob): number {
  if (job.totalBytes <= 0) return 0
  return Math.min(100, Math.round((job.receivedBytes / job.totalBytes) * 100))
}

export function mib(bytes: number): string {
  return `${(bytes / 1024 / 1024).toFixed(1)} MiB`
}

const client = createApiClient('/ticket-sync', undefined, getAccessToken)

/** 受理同步任务。400/409 由调用方按 error 字段转成提示。 */
export async function createSyncJob(ticketId: string): Promise<void> {
  await client.post('/jobs', { ticketId })
}

/** 查询任务；服务重启后任务表为空，404 视为「无任务」而不是错误。 */
export async function fetchSyncJob(ticketId: string): Promise<SyncJob | null> {
  try {
    const res = await client.get(`/jobs/${ticketId}`)
    return res.data as SyncJob
  } catch (error: any) {
    if (error?.response?.status === 404) return null
    throw error
  }
}

/** 取消任务。已结束的任务服务端幂等返回 204。 */
export async function cancelSyncJob(ticketId: string): Promise<void> {
  await client.delete(`/jobs/${ticketId}`)
}
