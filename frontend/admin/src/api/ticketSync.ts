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
  // 曾同步成功、产物已按保留期或磁盘水位清理掉。
  // 这是"可以重新拉取"的状态，不是失败——原件仍在日志源服务器上。
  | 'cleaned'

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
  /** 产物被清理的时刻（仅 phase === 'cleaned' 时有值）。 */
  cleanedAt?: string | null
  /** 清理原因标识。具体规则见 GET /cleanup-events 的 kind。 */
  cleanupReason?: string | null
}

/** /artifacts 的响应：root 是服务端真实产物根目录。 */
export interface SyncArtifactList {
  items: SyncArtifact[]
  root: string | null
}

/** 一次自动清理动作的流水记录。 */
export interface CleanupEvent {
  id: number
  kind: string
  ticketId: string | null
  bytes: number
  freeBytes: number | null
  detail: string | null
  occurredAt: string
}

export interface CleanupThresholds {
  evictFreeBytes: number
  minFreeBytes: number
  retentionDays: number
  minKeepDays: number
  maxTotalBytes: number
}

export interface CleanupSummaryItem {
  count: number
  bytes: number
}

export interface CleanupEventList {
  items: CleanupEvent[]
  last24h: Record<string, CleanupSummaryItem>
  thresholds: CleanupThresholds
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
  // 清理不是失败：原件还在日志源服务器上，点一次就能重新同步回来。
  // 文案必须说清这一点，否则用户会以为数据永久丢了。
  cleaned: '已清理',
}

/** 清理动作类型到用户可读文案。未知类型回退为原始值，避免新增类型时页面空白。 */
export const CLEANUP_KIND_TEXT: Record<string, string> = {
  retention: '保留期到期',
  capacity: '磁盘容量淘汰',
  reconcile: '幽灵记账对账',
  'stale-tmp': '半成品临时目录回收',
  trash: '回收站清空',
  waterline: '磁盘水位熔断',
}

export function cleanupKindText(kind: string): string {
  return CLEANUP_KIND_TEXT[kind] ?? kind
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
  // 与 no-space 区分：这是写入**之前**被水位闸门拦下（预防），
  // 不是已经写坏了数据。文案要给出"等清理跑完再点一次"的行动指引。
  'low-disk': '磁盘余量低于安全水位，已暂停写入（自动清理执行中，稍后重试）',
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

/** 查询本地已同步工单目录、进行中任务与已清理工单。 */
export async function fetchSyncArtifacts(): Promise<SyncArtifactList> {
  const res = await client.get('/artifacts')
  return {
    items: (res.data?.items ?? []) as SyncArtifact[],
    // root 由服务端回传：界面上的目录路径必须与真实根目录一致，
    // 硬编码路径会把用户引到错误的位置去清理。
    root: (res.data?.root ?? null) as string | null,
  }
}

/** 查询自动清理动作流水（保留期删除、水位淘汰、幽灵行对账、熔断拒绝）。 */
export async function fetchCleanupEvents(limit = 100): Promise<CleanupEventList> {
  const res = await client.get('/cleanup-events', { params: { limit } })
  return {
    items: (res.data?.items ?? []) as CleanupEvent[],
    last24h: (res.data?.last24h ?? {}) as Record<string, CleanupSummaryItem>,
    thresholds: res.data?.thresholds as CleanupThresholds,
  }
}

/** 取消任务。已结束的任务服务端幂等返回 204。 */
export async function cancelSyncJob(ticketId: string): Promise<void> {
  await client.delete(`/jobs/${ticketId}`)
}
