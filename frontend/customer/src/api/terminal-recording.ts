/**
 * terminal-recording.ts
 * 终端操作录制工具模块
 * Task 42: 终端操作录制功能
 *
 * 功能：
 * - 序号维护（sessionStorage 缓存）
 * - ANSI 码剔除（用于搜索）
 * - 批量上传（debounced）
 * - 操作记录写入 API
 */

import { ref, type Ref } from 'vue'

// ============================================================
// 类型定义
// ============================================================

export type OperationDirection = 'input' | 'output'

export interface TerminalOperationCreate {
  case_id: string
  conversation_id?: string
  session_id?: string
  seq_number: number
  direction: OperationDirection
  command?: string
  content: string
  content_clean?: string
  exit_code?: number
  diagnostic_stage?: string
}

export interface TerminalOperationResponse {
  id: number
  case_id: string
  seq_number: number
  direction: OperationDirection
  command?: string
  content: string
  exit_code?: number
  diagnostic_stage?: string
  created_at: string
}

export interface TerminalOperationListResponse {
  total: number
  operations: TerminalOperationResponse[]
}

// ============================================================
// 配置常量
// ============================================================

const API_BASE = import.meta.env.VITE_API_BASE_URL || ''
const MAX_OUTPUT_SIZE = 64 * 1024  // 64KB 单条输出上限
const UPLOAD_DELAY_MS = 500        // 批量上传延迟

// ============================================================
// 序号管理
// ============================================================

const SEQ_STORAGE_KEY = 'hci_terminal_seq'

/**
 * 获取下一个序号（sessionStorage 缓存）
 */
export function getNextSeqNumber(caseId: string): number {
  const storageKey = `${SEQ_STORAGE_KEY}_${caseId}`
  const currentSeq = parseInt(sessionStorage.getItem(storageKey) || '0', 10)
  const nextSeq = currentSeq + 1
  sessionStorage.setItem(storageKey, String(nextSeq))
  return nextSeq
}

/**
 * 重置序号（页面刷新后从后端恢复）
 */
export function resetSeqNumber(caseId: string, latestSeq: number): void {
  const storageKey = `${SEQ_STORAGE_KEY}_${caseId}`
  sessionStorage.setItem(storageKey, String(latestSeq))
}

/**
 * 清除序号缓存
 */
export function clearSeqCache(caseId: string): void {
  const storageKey = `${SEQ_STORAGE_KEY}_${caseId}`
  sessionStorage.removeItem(storageKey)
}

// ============================================================
// ANSI 码处理
// ============================================================

/**
 * 剔除 ANSI 转义码（用于搜索索引）
 * 保留原始 ANSI 用于回放渲染
 */
export function stripAnsi(text: string): string {
  if (!text) return ''

  // 剔除所有 ANSI/VT100 转义序列
  let cleaned = text
    // OSC 序列: \x1b] ... (\x07 或 \x1b\\)
    .replace(/\x1b\][^\x07\x1b]*(\x07|\x1b\\)/g, '')
    // CSI 序列: \x1b[ ... (@-~)
    .replace(/\x1b\[[0-?]*[ -/]*[@-~]/g, '')
    // 其他控制序列
    .replace(/\x1b[@-Z\\-_]/g, '')

  // 剔除不可见控制字符（保留换行/回车/制表符）
  cleaned = cleaned
    .split('')
    .filter((ch) => {
      const code = ch.charCodeAt(0)
      return ch === '\n' || ch === '\r' || ch === '\t' || code >= 0x20
    })
    .join('')

  return cleaned.trim()
}

/**
 * 截断大输出（超过 64KB）
 */
export function truncateOutput(content: string): string {
  if (content.length <= MAX_OUTPUT_SIZE) {
    return content
  }
  return content.slice(0, MAX_OUTPUT_SIZE) + '\n... [输出过长已截断]'
}

// ============================================================
// API 调用
// ============================================================

/**
 * 写入单条操作记录
 */
export async function createOperation(
  payload: TerminalOperationCreate
): Promise<TerminalOperationResponse> {
  const response = await fetch(`${API_BASE}/api/terminal/operations`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(payload),
  })

  if (!response.ok) {
    const error = await response.text()
    throw new Error(`创建操作记录失败: ${error}`)
  }

  return response.json()
}

/**
 * 查询操作记录列表
 */
export async function listOperations(
  caseId: string,
  options?: {
    stage?: string
    search?: string
    direction?: OperationDirection
    order?: 'asc' | 'desc'
    limit?: number
    offset?: number
  }
): Promise<TerminalOperationListResponse> {
  const params = new URLSearchParams({ case_id: caseId })

  if (options?.stage) params.set('stage', options.stage)
  if (options?.search) params.set('search', options.search)
  if (options?.direction) params.set('direction', options.direction)
  if (options?.order) params.set('order', options.order)
  if (options?.limit) params.set('limit', String(options.limit))
  if (options?.offset) params.set('offset', String(options.offset))

  const response = await fetch(`${API_BASE}/api/terminal/operations?${params}`)

  if (!response.ok) {
    const error = await response.text()
    throw new Error(`查询操作记录失败: ${error}`)
  }

  return response.json()
}

/**
 * 获取最新序号（用于页面刷新恢复）
 */
export async function getLatestSeqNumber(caseId: string): Promise<number> {
  const response = await fetch(`${API_BASE}/api/terminal/operations/latest-seq?case_id=${caseId}`)

  if (!response.ok) {
    // 如果接口不存在，返回 0
    return 0
  }

  const data = await response.json()
  return data.latest_seq_number || 0
}

// ============================================================
// 批量上传管理
// ============================================================

export interface RecordingState {
  caseId: Ref<string>
  conversationId: Ref<string | undefined>
  sessionId: Ref<string | undefined>
  diagnosticStage: Ref<string | undefined>
  outputBuffer: Ref<string>
  currentSeq: Ref<number>
  isRecording: Ref<boolean>
}

/**
 * 创建录制状态管理器
 */
export function createRecordingState(): RecordingState {
  return {
    caseId: ref(''),
    conversationId: ref(undefined),
    sessionId: ref(undefined),
    diagnosticStage: ref(undefined),
    outputBuffer: ref(''),
    currentSeq: ref(0),
    isRecording: ref(false),
  }
}

/**
 * Debounce 批量上传队列
 */
let uploadQueue: TerminalOperationCreate[] = []
let uploadTimer: ReturnType<typeof setTimeout> | null = null

/**
 * 记录输入命令
 */
export async function recordInput(
  state: RecordingState,
  command: string
): Promise<void> {
  if (!state.caseId.value || !state.isRecording.value) return

  const seqNumber = getNextSeqNumber(state.caseId.value)
  state.currentSeq.value = seqNumber

  await createOperation({
    case_id: state.caseId.value,
    conversation_id: state.conversationId.value,
    session_id: state.sessionId.value,
    seq_number: seqNumber,
    direction: 'input',
    command: command,
    content: command,
    content_clean: stripAnsi(command),
    diagnostic_stage: state.diagnosticStage.value,
  })
}

/**
 * 记录输出（累积后批量上传）
 */
export function recordOutput(
  state: RecordingState,
  output: string
): void {
  if (!state.caseId.value || !state.isRecording.value) return

  state.outputBuffer.value += output

  // Debounced 上传
  if (!uploadTimer) {
    uploadTimer = setTimeout(() => {
      flushOutputBuffer(state)
      uploadTimer = null
    }, UPLOAD_DELAY_MS)
  }
}

/**
 * 刷新输出缓冲区（批量上传）
 */
async function flushOutputBuffer(state: RecordingState): Promise<void> {
  const buffer = state.outputBuffer.value
  if (!buffer.trim()) return

  state.outputBuffer.value = ''

  const seqNumber = getNextSeqNumber(state.caseId.value)

  try {
    await createOperation({
      case_id: state.caseId.value,
      conversation_id: state.conversationId.value,
      session_id: state.sessionId.value,
      seq_number: seqNumber,
      direction: 'output',
      content: truncateOutput(buffer),
      content_clean: truncateOutput(stripAnsi(buffer)),
      diagnostic_stage: state.diagnosticStage.value,
    })
  } catch (e) {
    console.error('[TerminalRecording] 上传输出失败:', e)
  }
}

/**
 * 强制刷新（用于命令完成时）
 */
export async function forceFlushOutput(state: RecordingState): Promise<void> {
  if (uploadTimer) {
    clearTimeout(uploadTimer)
    uploadTimer = null
  }
  await flushOutputBuffer(state)
}

/**
 * 开始录制
 */
export function startRecording(
  state: RecordingState,
  caseId: string,
  options?: {
    conversationId?: string
    sessionId?: string
    diagnosticStage?: string
  }
): void {
  state.caseId.value = caseId
  state.conversationId.value = options?.conversationId
  state.sessionId.value = options?.sessionId
  state.diagnosticStage.value = options?.diagnosticStage
  state.outputBuffer.value = ''
  state.isRecording.value = true

  // 恢复序号（从 sessionStorage）
  const cachedSeq = parseInt(
    sessionStorage.getItem(`${SEQ_STORAGE_KEY}_${caseId}`) || '0',
    10
  )
  state.currentSeq.value = cachedSeq

  console.log('[TerminalRecording] 开始录制', { caseId, seq: cachedSeq })
}

/**
 * 停止录制
 */
export async function stopRecording(state: RecordingState): Promise<void> {
  if (!state.isRecording.value) return

  // 强制刷新剩余输出
  await forceFlushOutput(state)

  state.isRecording.value = false
  console.log('[TerminalRecording] 停止录制', { seq: state.currentSeq.value })
}

// ============================================================
// 导出完整模块
// ============================================================

export const TerminalRecording = {
  getNextSeqNumber,
  resetSeqNumber,
  clearSeqCache,
  stripAnsi,
  truncateOutput,
  createOperation,
  listOperations,
  getLatestSeqNumber,
  createRecordingState,
  recordInput,
  recordOutput,
  forceFlushOutput,
  startRecording,
  stopRecording,
}

export default TerminalRecording