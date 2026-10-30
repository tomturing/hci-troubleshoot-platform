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

export type SyncPhase =
  | 'queued'
  | 'fetching'
  | 'extracting'
  | 'ready'
  | 'failed'
  | 'cancelled'
  // 服务重启导致任务中途消失。与 failed 区分：重新触发通常就能成功，
  // 不应提示用户去排查日志源。
  | 'interrupted'

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

export interface SyncArtifact {
  ticketId: string
  phase: SyncPhase
  directoryReady: boolean
  directoryUpdatedAt: string | null
  job: SyncJob | null
}

export const PHASE_TEXT: Record<SyncPhase, string> = {
  queued: '排队中',
  fetching: '拉取中',
  extracting: '解压中',
  ready: '已就绪',
  failed: '失败',
  cancelled: '已取消',
  // 服务重启导致任务中途消失。文案刻意与"失败"区分：重新触发通常就能成功，
  // 不该让用户去排查日志源。
  interrupted: '已中断',
}

/** 阶段文案。未知阶段回退为原始值，避免后端新增阶段时页面出现空白标签。 */
export function phaseText(phase: SyncPhase): string {
  return PHASE_TEXT[phase] ?? phase
}

// 错误码到用户可读文案的映射，与服务端文档第 8 章一一对应。
// quota/depth 两码此前写作"体积异常""层级异常"，把服务自身的阈值说成了
// 数据问题，用户会误以为包坏了。这里改为"超出服务上限"，具体数值由
// 服务端 message 补充（见 errorText 的拼接逻辑）。
const ERROR_TEXT: Record<string, string> = {
  'invalid-ticket-id': '工单号格式错误，应为 Q + 13 位数字',
  'not-found': '该工单在日志源服务器上不存在',
  'too-large': '工单包过大，超过单工单上限',
  'bad-password': '归档密码错误，请联系运维',
  'archive-corrupt': '压缩包已损坏（采集端可能生成了空包）',
  'no-space': '服务器磁盘空间不足',
  'quota-exceeded': '超出服务解压上限',
  'depth-exceeded': '超出服务层级上限',
  'ssh-failed': '无法连接日志源服务器',
  'extract-failed': '解压失败，请查看服务日志',
  'non-log-payload': '该工单包不是日志包（仅含转储文件），已跳过',
}

export function errorText(code: string | null, fallback: string | null = null): string {
  const base = code ? ERROR_TEXT[code] : null
  if (base) {
    // 服务端 message 带具体依据（如"解压后文件数 107185 超过上限 50000"）。
    // 固定文案给结论、message 给依据，拼接而不是互相覆盖，
    // 否则用户永远只能看到一句无法行动的话。
    return fallback && fallback !== base ? `${base}：${fallback}` : base
  }
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

/** 查询本地已同步工单目录及自动/手动任务状态。 */
export async function fetchSyncArtifacts(): Promise<SyncArtifact[]> {
  const res = await client.get('/artifacts')
  return (res.data?.items ?? []) as SyncArtifact[]
}

/** 取消任务。已结束的任务服务端幂等返回 204。 */
export async function cancelSyncJob(ticketId: string): Promise<void> {
  await client.delete(`/jobs/${ticketId}`)
}
