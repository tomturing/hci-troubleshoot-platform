/**
 * 聊天 Store - 管理对话状态
 */

import { defineStore } from 'pinia'
import { ref, computed, nextTick } from 'vue'
import { createApiClient, createCaseApi, createConversationApi, createAssistantApi, createEnvironmentApi } from '@hci/shared'
import type { CaseResponse, MessageResponse, AssistantInfo, AssistantsResponse, EnvironmentResponse, EnvironmentContextResponse, EnvType } from '@hci/shared'
import { getClientId } from '@/utils/clientId'
import { createEvaluateApi } from '@/api/evaluate'
import { checkBridgeRunning, checkBridgeBeforeOpen, createBridgeSocket, buildConnectMessage, buildInputMessage, buildDisconnectMessage, stripAnsi, parseJsonOutput, buildAgentExecMessage, buildAgentExecProcessMessage, parseAgentExecResult, type BridgeStatus, type TerminalWsMessage } from '@/api/terminal'

// 开发环境专用日志（生产环境自动禁用）
const isDev = import.meta.env.DEV
function devLog(tag: string, message: string, data?: unknown) {
  if (!isDev) return
  // 脱敏敏感字段（仅对对象类型）
  const sanitized = data && typeof data === 'object' && !Array.isArray(data)
    ? sanitizeSensitive(data as Record<string, unknown>)
    : data
  console.log(`[${tag}] ${message}`, sanitized ?? '')
}

// 脱敏敏感信息（password 完全隐藏，host 部分隐藏，output 截断）
function sanitizeSensitive(data: Record<string, unknown>): Record<string, unknown> {
  const result: Record<string, unknown> = {}
  for (const [key, value] of Object.entries(data)) {
    const k = key.toLowerCase()
    if (k.includes('password') || k.includes('passwd')) {
      result[key] = '***'
    } else if (k.includes('host') && typeof value === 'string') {
      result[key] = value.length > 6 ? `${value.substring(0, 3)}...${value.substring(value.length - 3)}` : value
    } else if (k === 'raw' && typeof value === 'string' && value.length > 200) {
      // WebSocket 原始消息截断
      result[key] = value.substring(0, 200) + '...(截断)'
    } else if (k === 'outputpreview' && typeof value === 'string') {
      result[key] = value.substring(0, 30) + '...(截断)'
    } else {
      result[key] = value
    }
  }
  return result
}

/** 前端聊天消息 */
export interface ChatMessage {
  id: string
  role: 'user' | 'assistant' | 'system'
  content: string
  timestamp: Date
  isStreaming?: boolean
  metadata?: Record<string, unknown>
}

/** 工单创建模板 */
export interface CaseTemplate {
  title: string
  description: string
}

/** SSH 创建工单流程日志 */
export interface SshCreationLogEntry {
  id: string
  timestamp: string
  level: 'info' | 'warn' | 'error'
  step: string
  message: string
  data?: Record<string, unknown>
}

export const useChatStore = defineStore('chat', () => {
  const clientId = getClientId()
  const apiClient = createApiClient('/api', clientId)
  const caseApi = createCaseApi(apiClient)
  const conversationApi = createConversationApi(apiClient)
  const assistantApi = createAssistantApi(apiClient)
  const evaluateApi = createEvaluateApi(apiClient)
  const environmentApi = createEnvironmentApi(apiClient)

  // terminal_bridge 日志回采缓冲（批量上报到 /api/bridge-logs，异常重传）
  let bridgeLogBuffer: Record<string, unknown>[] = []
  let bridgeLogTimer: number | null = null
  let bridgeLogRetryCount = 0
  const BRIDGE_LOG_MAX_RETRY = 5
  const BRIDGE_LOG_BASE_DELAY = 500
  // C 修复：保留近期回采日志的环形缓冲，供工单创建后将临时会话（ssh-create-temp）日志迁移到真实工单
  let recentBridgeLogs: Record<string, unknown>[] = []
  const RECENT_BRIDGE_LOGS_CAP = 2000

  async function flushBridgeLogs() {
    bridgeLogTimer = null
    if (bridgeLogBuffer.length === 0) return
    const batch = bridgeLogBuffer
    bridgeLogBuffer = []

    try {
      await apiClient.post('/bridge-logs', { logs: batch })
      bridgeLogRetryCount = 0 // 成功后重置重试计数
    } catch (e) {
      // P1-5: 增强异常处理 - 指数退避重试 + 最大重试次数
      bridgeLogRetryCount++
      if (bridgeLogRetryCount <= BRIDGE_LOG_MAX_RETRY) {
        const delay = BRIDGE_LOG_BASE_DELAY * Math.pow(2, bridgeLogRetryCount - 1)
        console.warn(`日志回采失败（第 ${bridgeLogRetryCount} 次），${delay}ms 后重试`, e)
        bridgeLogBuffer = batch.concat(bridgeLogBuffer)
        if (bridgeLogTimer === null) {
          bridgeLogTimer = window.setTimeout(flushBridgeLogs, delay)
        }
      } else {
        console.error('日志回采失败，已达最大重试次数，丢弃日志', batch.length, '条')
        bridgeLogRetryCount = 0 // 重置计数，下次重新开始
      }
    }
  }

  function forwardBridgeLog(entry: Record<string, unknown>) {
    // 仅回采带工单关联的日志，保证落库后可按工单分析
    if (!entry.case_id) return
    bridgeLogBuffer.push(entry)
    // C 修复：同步写入近期缓冲，供工单创建后将临时会话日志迁移到真实工单
    recentBridgeLogs.push(entry)
    if (recentBridgeLogs.length > RECENT_BRIDGE_LOGS_CAP) {
      recentBridgeLogs.splice(0, recentBridgeLogs.length - RECENT_BRIDGE_LOGS_CAP)
    }
    if (bridgeLogTimer === null) bridgeLogTimer = window.setTimeout(flushBridgeLogs, 500)
  }

  // C 修复：将临时会话（fromCaseId）已采集的回采日志，以真实工单（toCaseId）重新落库，
  // 避免数据归属到 ssh-create-temp 而丢失
  async function importBridgeLogsToCase(fromCaseId: string, toCaseId: string) {
    if (!fromCaseId || !toCaseId || fromCaseId === toCaseId) return
    const migrating = recentBridgeLogs.filter((e) => e.case_id === fromCaseId)
    if (migrating.length === 0) return
    const remapped = migrating.map((e) => ({ ...e, case_id: toCaseId }))
    // 从近期缓冲中移除已迁移项，保证幂等（重复调用不会重复落库）
    recentBridgeLogs = recentBridgeLogs.filter((e) => e.case_id !== fromCaseId)
    try {
      await apiClient.post('/bridge-logs', { logs: remapped })
      console.log(`已将 ${remapped.length} 条临时会话日志迁移到工单 ${toCaseId}`)
    } catch (e) {
      console.error(`临时会话日志迁移到工单 ${toCaseId} 失败`, e)
    }
  }

  // P1-5: 浏览器关闭前保存 pending 日志到 localStorage（断网保护）
  function savePendingLogs() {
    if (bridgeLogBuffer.length > 0) {
      try {
        localStorage.setItem('hci_pending_bridge_logs', JSON.stringify(bridgeLogBuffer))
        console.log('已保存 pending 日志到 localStorage:', bridgeLogBuffer.length, '条')
      } catch (e) {
        console.warn('保存 pending 日志失败:', e)
      }
    }
  }

  // P1-5: 页面加载时恢复 pending 日志
  function restorePendingLogs() {
    try {
      const pending = localStorage.getItem('hci_pending_bridge_logs')
      if (pending) {
        const logs = JSON.parse(pending) as Record<string, unknown>[]
        bridgeLogBuffer = logs.concat(bridgeLogBuffer)
        localStorage.removeItem('hci_pending_bridge_logs')
        console.log('已恢复 pending 日志:', logs.length, '条')
        if (bridgeLogTimer === null) {
          bridgeLogTimer = window.setTimeout(flushBridgeLogs, 1000) // 1s 后重试
        }
      }
    } catch (e) {
      console.warn('恢复 pending 日志失败:', e)
    }
  }

  // 缺陷 6: 前端重连时发送 resume 消息（触发历史日志回放）
  function sendResumeMessage(ws: WebSocket, caseId: string, traceId?: string) {
    if (!caseId) return
    const resumeMsg = JSON.stringify({
      type: 'resume',
      case_id: caseId,
      trace_id: traceId || undefined,
    })
    ws.send(resumeMsg)
    console.log('已发送 resume 消息:', caseId)
  }

  // 监听 WebSocket 重连事件（由 checkBridgeRunning 或其他重连逻辑触发）
  function setupWebSocketResumeHandler(ws: WebSocket) {
    const originalOnOpen = ws.onopen
    ws.onopen = (event) => {
      // 缺陷 6: 重连时自动发送 resume 消息（如果有当前工单）
      if (currentCase.value?.case_id) {
        sendResumeMessage(ws, currentCase.value.case_id)
      }
      // 调用原始的 onopen 处理器（绑定正确的 this 上下文）
      if (originalOnOpen) {
        originalOnOpen.call(ws, event)
      }
    }
  }

  // P1-5: 注册 beforeunload 事件（浏览器关闭/刷新前保存）
  if (typeof window !== 'undefined') {
    window.addEventListener('beforeunload', savePendingLogs)
    // 页面加载时恢复
    restorePendingLogs()
  }

  // 是否显示助手选择器（v2.1：从后端 API 响应获取）
  const showAssistantSelector = ref(false)
  const defaultAssistant = ref<string | null>(null)

  // 状态
  const messages = ref<ChatMessage[]>([])
  const currentCase = ref<CaseResponse | null>(null)
  const conversationId = ref<string | null>(null)
  const isLoading = ref(false)
  const isStreaming = ref(false)
  const existingCases = ref<CaseResponse[]>([])
  const initialized = ref(false)

  // AI 助手列表
  const assistants = ref<AssistantInfo[]>([])
  const selectedAssistant = ref<string>('')

  // 未关闭工单确认流程
  const pendingCase = ref<CaseResponse | null>(null)
  const showPendingDialog = ref(false)

  // 工单创建模板流程
  const showCaseTemplate = ref(false)
  const caseTemplate = ref<CaseTemplate>({ title: '', description: '' })
  const pendingUserMessage = ref('')
  const caseCreateDialogBridgeStatus = ref<'running' | 'not-running' | 'checking'>('checking')
  const sshConnectDialogBridgeStatus = ref<'running' | 'not-running' | 'checking'>('checking')

  // 历史工单查看
  const showHistoryDrawer = ref(false)
  const historyMessages = ref<ChatMessage[]>([])
  const historyCase = ref<CaseResponse | null>(null)
  const historyLoading = ref(false)

  // 评分卡状态
  const showRatingCard = ref(false)
  const ratingConversationId = ref<string | null>(null)

  // 诊断阶段（S0~S6）
  const diagnosticStage = ref<string>('S0')

  // 终端面板状态
  const showTerminalSidebar = ref(false)
  const terminalInputCommand = ref('')
  const assistantDraftText = ref('')

  // === 全局 SSH 连接状态 ===
  const sshWebSocket = ref<WebSocket | null>(null)
  const sshConnectionState = ref<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected')
  const sshCurrentConfig = ref<{
    host: string
    port: number
    username: string
    authType: 'password' | 'key'
    caseId: string
  } | null>(null)
  const sshErrorMessage = ref('')
  const sshOutputBuffer = ref<string>('')
  const sshCommandConsumer = ref<'terminal' | 'collection' | null>(null)
  const sshTerminalOutputEvent = ref<string>('')

  // === Agent 命令执行结果等待队列 ===
  // 用于 waitForExecResult 函数，存储 execId → resolve 回调
  const pendingExecCallbacks = new Map<string, {
    resolve: (result: { output: string; exitCode: number; stdout?: string; stderr?: string }) => void
    reject: (error: Error) => void
    timeoutId: number
  }>()

  // 双通道流式缓冲 (Scheme B)
  const execBuffers = new Map<string, { stdout: string; stderr: string }>()

  // Agent 模式：待确认的高风险操作（confirm_request SSE 事件）
  // T1-2：必须包含 exec_id 与 input_hash，提交确认时回传给后端进行幂等与防篡改校验
  const pendingConfirm = ref<{
    tool_name: string
    tool_args: Record<string, unknown>
    risk_level: 2 | 3
    risk_description: string
    timeout_seconds: number
    exec_id: string
    input_hash: string
  } | null>(null)

  // T-TOOL-18：待确认的命令执行请求（agent_exec_command SSE 事件，risk=2）
  const pendingExecConfirm = ref<{
    execId: string
    command: string
    reason: string
    riskLevel: 1 | 2 | 3
    nodeIp: string
    caseId: string
    convId: string  // conversation ID（用于 POST 结果）
    timestamp: number  // 创建时间戳（用于倒计时）
  } | null>(null)

  // T-E7: ops-agent 交互请求（interactive_request SSE 事件）
  const pendingInteractive = ref<{
    requestId: string
    acpSessionId: string
    kind: string
    title: string
    prompt: string
    options: Array<{ optionId: string; name: string }>
    customInput: boolean
    metadata: Record<string, unknown>
    execId?: string
    inputHash?: string
    expiresAt?: string
  } | null>(null)

  // Bridge 运行状态
  const bridgeStatus = ref<BridgeStatus>('not_running')

  // === 命令自动执行状态 ===
  /** 自动执行模式，持久化到 localStorage */
  const AUTO_EXEC_MODE_KEY = 'hci_auto_execute_mode'
  const AUTO_EXEC_VALID_MODES = ['off', 'safe-only', 'aggressive'] as const
  const _storedMode = localStorage.getItem(AUTO_EXEC_MODE_KEY)
  const autoExecuteMode = ref<'off' | 'safe-only' | 'aggressive'>(
    AUTO_EXEC_VALID_MODES.includes(_storedMode as 'off' | 'safe-only' | 'aggressive')
      ? (_storedMode as 'off' | 'safe-only' | 'aggressive')
      : 'off',
  )
  /** 串行执行锁，保证同一时刻只有一条命令在执行 */
  const isExecutingCommand = ref(false)
  /** 当前正在执行的命令信息（供 CommandBlock UI 展示） */
  const executingCommand = ref<{
    command: string
    startedAt: Date
    blockId: string
  } | null>(null)
  /** 单次会话自动执行计数（防止 Agent Loop） */
  const autoExecCount = ref(0)
  const AUTO_EXEC_MAX = 10
  /** 连续失败计数（熔断保护） */
  const consecutiveAutoFailures = ref(0)
  const AUTO_EXEC_FAILURE_BREAKER = 3
  /** 页面可见性（后台时暂停队列） */
  const pageVisible = ref(true)

  // === 环境数据采集状态 ===
  const collectionState = ref<'idle' | 'collecting' | 'success' | 'error'>('idle')
  const collectionProgress = ref<Record<string, 'pending' | 'collecting' | 'done' | 'empty' | 'error'>>({})
  const environmentData = ref<EnvironmentResponse[]>([])
  const environmentContext = ref<EnvironmentContextResponse | null>(null)

  // 计算属性
  const hasActiveCase = computed(() => {
    return currentCase.value && !['closed', 'cancelled'].includes(currentCase.value.status)
  })

  const isCaseClosed = computed(() => {
    return currentCase.value !== null && !hasActiveCase.value
  })

  /** 获取可用 AI 助手列表（v2.1：从响应获取显示决策）*/
  async function fetchAssistants() {
    try {
      const res = await assistantApi.list()
      const data = res.data as AssistantsResponse

      // 从响应中获取显示决策
      showAssistantSelector.value = data.show_selector ?? false
      defaultAssistant.value = data.default_assistant

      assistants.value = (data.assistants || []).map((item: AssistantInfo) => ({
        type: item.type,
        display_name: item.display_name ?? item.type,
        description: item.description ?? '',
        capabilities: item.capabilities ?? [],
        available: item.available ?? true,
        is_default: item.is_default ?? false,
      }))

      // 选择默认助手或第一个可用助手
      const defaultOrFirst = assistants.value.find(a => a.is_default && a.available)
        || assistants.value.find(a => a.available)
      if (defaultOrFirst) {
        selectedAssistant.value = defaultOrFirst.type
      }
    } catch (e) {
      console.warn('获取助手列表失败，使用默认值', e)
      // 降级响应：单助手，不显示选择器
      showAssistantSelector.value = false
      defaultAssistant.value = 'openclaw'
      assistants.value = [{
        type: 'openclaw',
        display_name: 'OpenClaw (GLM)',
        description: '基于智谱 GLM 模型的 AI 排障助手',
        capabilities: ['troubleshooting'],
        available: true,
        is_default: true,
      }]
      selectedAssistant.value = 'openclaw'
    }
  }

  /** 初始化 */
  async function initialize() {
    if (initialized.value) return
    await fetchAssistants()
    try {
      const res = await caseApi.listByClient(clientId)
      existingCases.value = res.data
      const activeCase = existingCases.value.find(
        (c) => !['closed', 'cancelled'].includes(c.status),
      )
      if (activeCase) {
        pendingCase.value = activeCase
        showPendingDialog.value = true
        // 立即恢复工单绑定的助手选择（不等用户点"续上"，避免刷新后 top-bar 显示错误默认值）
        _restoreAssistantFromCase(activeCase)
      } else {
        addSystemMessage('您好！我是 HCI 故障排查助手。请描述您遇到的问题，我会帮您创建工单并提供解决方案。')
      }
    } catch (e) {
      console.error('初始化失败', e)
      addSystemMessage('您好！我是 HCI 故障排查助手。请描述您遇到的问题。')
    }
    initialized.value = true
    // 初始化页面可见性监听（用于自动执行后台暂停）
    initPageVisibility()
  }

  // 从工单数据恢复助手选择；若助手类型已从列表中移除则降级到默认可用助手
  function _restoreAssistantFromCase(caseData: { assistant_type?: string }) {
    const saved = caseData.assistant_type
    if (!saved) return
    // 只要助手类型存在于列表（不论当前 available 状态），即恢复用户保存的偏好；
    // available 状态是调度层的实时概念，不应影响已绑定工单的助手恢复
    if (assistants.value.some(a => a.type === saved)) {
      selectedAssistant.value = saved
      return
    }
    // 助手类型完全不在列表中（已删除或下线），降级到默认可用助手
    devLog('chat', `助手 ${saved} 不在列表中，回退到默认助手`, { saved })
    const fallback = assistants.value.find(a => a.is_default && a.available)
      || assistants.value.find(a => a.available)
    if (fallback) selectedAssistant.value = fallback.type
  }

  async function resumePendingCase() {
    if (!pendingCase.value) return
    currentCase.value = pendingCase.value
    showPendingDialog.value = false
    const caseId = pendingCase.value.case_id

    // 从工单数据恢复助手选择
    _restoreAssistantFromCase(pendingCase.value)

    await loadConversationHistory(caseId)
    pendingCase.value = null
    // 恢复工单时同步加载环境数据（fire-and-forget，不阻塞对话恢复）
    collectEnvironmentData(caseId).catch(() => { })
  }

  async function closePendingCase() {
    if (!pendingCase.value) return
    try {
      await caseApi.close(pendingCase.value.case_id, { close_reason: 'user_command' })
      addSystemMessage(`旧工单 ${pendingCase.value.case_id} 已关闭。请描述您遇到的新问题。`)
    } catch (e: any) {
      addSystemMessage(`关闭旧工单失败: ${e.response?.data?.detail || e.message}，但您仍可以创建新工单。`)
    }
    showPendingDialog.value = false
    pendingCase.value = null
  }

  async function loadConversationHistory(caseId: string) {
    try {
      const convRes = await apiClient.get(`/conversations/case/${caseId}`)
      const conversations = convRes.data as any[]
      if (conversations.length > 0) {
        const conv = conversations[0]
        conversationId.value = conv.conversation_id
        diagnosticStage.value = conv.diagnostic_stage || 'S0'
        const msgRes = await conversationApi.getMessages(conv.conversation_id)
        const history: MessageResponse[] = msgRes.data
        messages.value = history.map((m) => {
          const meta = (m as any).metadata ?? undefined
          // 向后兼容：旧格式保存的 interactive_request metadata 是扁平结构（无 event 嵌套）
          // 新格式已修复为 { kind, event: {...} }，此处做转换以兼容旧历史数据
          const normalizedMeta =
            meta?.kind === 'interactive_request' && !meta.event
              ? {
                  kind: 'interactive_request',
                  event: {
                    requestId: meta.requestId,
                    acpSessionId: meta.acpSessionId,
                    kind: meta.interactiveKind ?? 'info_request',
                    title: meta.title ?? '',
                    prompt: meta.prompt ?? m.content ?? '',
                    options: meta.options ?? [],
                    customInput: meta.customInput ?? true,
                    metadata: meta.metadata ?? {},
                  },
                }
              : meta
          return {
            id: m.message_id,
            role: m.role as ChatMessage['role'],
            content: m.content,
            timestamp: new Date(m.created_at),
            metadata: normalizedMeta,
          }
        })

        // ── 场景A: ops-agent 生成中途刷新（activePrompt=True，无 interactive_request）──
        // 判断：当前是 ops-agent 助手 + 最后一条消息是用户发送的普通消息 + 后面没有 AI 回复
        // 说明 AI 正在思考/生成，刷新后需自动 resume-stream 接回流
        const isOpsAgent = selectedAssistant.value === 'ops-agent'
          || (conv as any).assistant_type === 'ops-agent'
        // resumeScheduled：防止场景A/B同时满足条件时重复调度 resumeOpsAgentStream
        let resumeScheduled = false
        if (isOpsAgent && messages.value.length > 0) {
          const lastMsg = messages.value[messages.value.length - 1]
          const lastMsgIsUserNormal = lastMsg.role === 'user'
            && (lastMsg.metadata as any)?.kind !== 'interactive_response'
          if (lastMsgIsUserNormal) {
            // AI 思考中刷新：自动重连 resume-stream
            resumeScheduled = true
            nextTick().then(() => resumeOpsAgentStream())
          }
        }

        // ── 场景B: ops-agent 交互请求相关恢复（pendingInteractive 重建 / interactive_response 后续写未到）──
        // 找到最后一条 interactive_request，若其后没有对应的 interactive_response，
        // 说明该交互请求尚未被用户回答，恢复 pendingInteractive 供卡片 UI 正常展示
        const lastIRMsg = [...messages.value].reverse().find(
          m => m.role === 'assistant' && m.metadata?.kind === 'interactive_request' && m.metadata?.event
        )
        if (lastIRMsg?.metadata?.event) {
          const irMsgIdx = messages.value.indexOf(lastIRMsg)
          const hasResponse = messages.value.slice(irMsgIdx + 1).some(
            m => m.role === 'user' && (m.metadata as any)?.kind === 'interactive_response'
          )
          if (!hasResponse) {
            // 未回答的交互请求：恢复 pendingInteractive，确保卡片 UI 显示"可交互"状态
            pendingInteractive.value = lastIRMsg.metadata.event as any
          } else {
            // 用户已提交 interactive_response，检查 ops-agent 续写是否已传回前端
            // 若最后一条 user/interactive_response 之后没有 AI 文字回复，
            // 说明续写事件仍在 ops-agent outbox 中，需要重连事件流
            const msgsAfterIR = messages.value.slice(irMsgIdx + 1)
            const lastIRResponseIdx = msgsAfterIR.map((m, i) => ({ m, i }))
              .filter(({ m }) => m.role === 'user' && (m.metadata as any)?.kind === 'interactive_response')
              .pop()?.i ?? -1
            const hasSubsequentAIReply = lastIRResponseIdx >= 0
              && msgsAfterIR.slice(lastIRResponseIdx + 1).some(
                m => m.role === 'assistant' && m.metadata?.kind !== 'interactive_request'
              )
            if (!hasSubsequentAIReply && lastIRResponseIdx >= 0 && !resumeScheduled) {
              // ops-agent 续写事件尚未到达前端，页面稳定后自动重连（场景A未调度时才触发）
              nextTick().then(() => resumeOpsAgentStream())
            }
          }
        }
      }
      if (messages.value.length === 0) {
        addSystemMessage(`工单 ${caseId} 已恢复。您可以继续描述问题，或输入 /close 关闭工单。`)
      }
    } catch (e) {
      console.error('加载对话历史失败', e)
      addSystemMessage('对话历史加载失败，但您仍可以继续对话。')
    }
  }

  async function sendMessage(content: string, metadata?: any) {
    if (!content.trim() || isStreaming.value) return

    if (content.startsWith('/close')) {
      await handleCloseCase()
      return
    }

    if (isCaseClosed.value) {
      addSystemMessage('当前工单已关闭，请点击「新建工单」开始新的对话。')
      return
    }

    if (!currentCase.value) {
      pendingUserMessage.value = content
      const title = content.length > 50 ? content.substring(0, 50) + '...' : content
      caseTemplate.value = { title, description: content }
      // T-FE-5: 无工单时先进行 Bridge 前置检测
      const bridgeStatus = await checkBridgeBeforeOpen()
      caseCreateDialogBridgeStatus.value = bridgeStatus
      showCaseTemplate.value = true
      return
    }

    addUserMessage(content, metadata)

    if (!conversationId.value) {
      await createConversation()
    }

    await streamAIResponse(content, metadata)
  }

  async function confirmCreateCase(template: CaseTemplate) {
    showCaseTemplate.value = false
    addUserMessage(pendingUserMessage.value)
    isLoading.value = true
    try {
      const res = await caseApi.create({
        client_id: clientId,
        title: template.title,
        description: template.description,
        assistant_type: selectedAssistant.value || undefined,
      })
      currentCase.value = res.data
      addSystemMessage(`工单 ${res.data.case_id} 已创建，AI 正在识别故障类型，请稍候…`)
      // 无 SSH 流程也尝试加载历史环境数据（fire-and-forget）
      collectEnvironmentData(res.data.case_id).catch(() => { })

      await createConversation()
      await streamAIResponse(pendingUserMessage.value)
    } catch (e: any) {
      addSystemMessage(`创建工单失败: ${e.response?.data?.detail || e.message}`)
    } finally {
      isLoading.value = false
      pendingUserMessage.value = ''
    }
  }

  function cancelCreateCase() {
    showCaseTemplate.value = false
    pendingUserMessage.value = ''
  }

  async function createConversation() {
    if (!currentCase.value) {
      console.error('[createConversation] currentCase 为空，无法创建对话')
      throw new Error('currentCase 为空，无法创建对话')
    }
    try {
      const assistantType = currentCase.value.assistant_type || selectedAssistant.value || undefined
      console.log('[createConversation] 开始创建对话', { caseId: currentCase.value.case_id, assistantType })
      const res = await conversationApi.create(currentCase.value.case_id, assistantType)
      conversationId.value = res.data.conversation_id
      console.log('[createConversation] 对话创建成功', { conversationId: conversationId.value })
    } catch (e: any) {
      const errorMsg = `创建对话失败: ${e.response?.data?.detail || e.message}`
      console.error('[createConversation]', errorMsg, e)
      addSystemMessage(errorMsg)
      throw e  // 抛出错误，让调用方知道失败
    }
  }

  async function streamAIResponse(content: string, metadata?: any) {
    if (!conversationId.value || !currentCase.value) {
      const errorMsg = !conversationId.value ? 'conversationId 为空' : 'currentCase 为空'
      console.error('[streamAIResponse]', errorMsg, '无法发送消息')
      throw new Error(`无法发送消息: ${errorMsg}`)
    }

    isStreaming.value = true
    const aiMsgId = `ai-${Date.now()}`
    messages.value.push({
      id: aiMsgId,
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      isStreaming: true,
    })

    // 用索引而非 find()，确保 Vue 响应式能正确追踪数组元素属性变更
    const getAiMsgIndex = () => messages.value.findIndex((m) => m.id === aiMsgId)

    try {
      const response = await fetch(`/api/conversations/${conversationId.value}/message`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Client-ID': clientId,
        },
        body: JSON.stringify({
          case_id: currentCase.value.case_id,
          role: 'user',
          content,
          // 修复：优先使用用户当前选择的助手类型，允许切换助手生效
          assistant_type: selectedAssistant.value || currentCase.value.assistant_type,
          metadata: metadata || undefined,  // 传递 metadata
        }),
      })

      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      let pendingEventType = 'message'
      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            pendingEventType = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            const data = line.slice(6)
            if (data === '[DONE]') {
              pendingEventType = 'message'
              continue
            }
            const idx = getAiMsgIndex()
            if (idx === -1) break
            if (pendingEventType === 'error') {
              // 解析服务端返回的结构化错误信息（H-2：SSE 错误帧标准化）
              let errorMsg = 'AI 响应出现错误，请稍后重试。'
              try {
                const errEvent = JSON.parse(data)
                if (errEvent.message) {
                  errorMsg = errEvent.message
                }
              } catch { }
              if (!messages.value[idx].content) {
                messages.value[idx].content = errorMsg
              }
            } else if (pendingEventType === 'confirm_request') {
              // 高风险操作需要用户确认：弹窗展示
              try {
                const event = JSON.parse(data)
                pendingConfirm.value = {
                  tool_name: event.tool_name,
                  tool_args: event.tool_args,
                  risk_level: event.risk_level,
                  risk_description: event.risk_description,
                  timeout_seconds: event.timeout_seconds ?? 120,
                  // T1-2：捕获后端下发的 exec_id 与 input_hash，作为后续提交的事务标识
                  exec_id: event.exec_id || event.request_id || '',
                  input_hash: event.input_hash || '',
                }
              } catch { }
            } else if (pendingEventType === 'tool_executing') {
              // 工具执行通知：在 AI 消息流后追加提示行
              try {
                const event = JSON.parse(data)
                const idx2 = getAiMsgIndex()
                if (idx2 !== -1) {
                  messages.value[idx2].content += `\n\n> 🔍 正在查询：\`${event.tool}\`…`
                }
              } catch { }
            } else if (pendingEventType === 'thinking') {
              // 推理步骤：追加到 AI 消息（可见调试信息）
              try {
                const event = JSON.parse(data)
                const idx2 = getAiMsgIndex()
                if (idx2 !== -1 && event.message) {
                  messages.value[idx2].content += `\n\n> 🤔 步骤 ${event.step}：${event.message}`
                }
              } catch { }
            } else if (pendingEventType === 'stage_change') {
              // 诊断阶段切换：更新前端进度条状态
              try {
                const event = JSON.parse(data)
                diagnosticStage.value = event.to ?? 'S0'
                devLog('stage_change', '诊断阶段切换', { from: event.from, to: event.to, label: event.label })
              } catch (e) {
                console.warn('[stage_change] 解析失败:', e)
              }
            } else if (pendingEventType === 'metadata') {
              // 消息元数据流式同步：直接更新 AI 消息中的元数据
              try {
                const event = JSON.parse(data)
                const idx2 = getAiMsgIndex()
                if (idx2 !== -1) {
                  messages.value[idx2].metadata = {
                    ...(messages.value[idx2].metadata || {}),
                    ...event,
                  }
                }
              } catch (e) {
                console.warn('[metadata] 解析失败:', e)
              }
            } else if (pendingEventType === 'interactive_request') {
              // T-E7: ops-agent 交互请求（SOP 操作卡 / 信息确认卡）
              // 改为对话气泡展示，不再弹窗
              try {
                const event = JSON.parse(data)
                const irEvent = {
                  requestId: event.requestId,
                  acpSessionId: event.acpSessionId,
                  kind: event.kind ?? 'info_request',
                  title: event.title ?? '',
                  prompt: event.prompt ?? '',
                  options: event.options ?? [],
                  customInput: event.customInput ?? true,
                  metadata: event.metadata ?? {},
                  execId: event.execId,
                  inputHash: event.inputHash,
                  expiresAt: event.expiresAt,
                }
                // 将 interactive_request 作为 assistant 气泡追加到消息列表
                const irMsgId = `ir-${event.requestId ?? Date.now()}`
                messages.value.push({
                  id: irMsgId,
                  role: 'assistant',
                  content: event.prompt ?? event.title ?? '',
                  timestamp: new Date(),
                  metadata: { kind: 'interactive_request', event: irEvent },
                })
                // 同步更新 pendingInteractive 以兼容旧代码路径（已关闭弹窗，但保留状态供外部使用）
                pendingInteractive.value = irEvent
              } catch (e) {
                console.warn('[interactive_request] 解析失败:', e)
              }
            } else if (pendingEventType === 'tool_call') {
              try {
                const event = JSON.parse(data)
                const existingIdx = messages.value.findIndex(m => {
                  const meta = m.metadata as any
                  return meta?.kind === 'tool_call' && meta?.event?.exec_id === event.exec_id
                })
                if (existingIdx !== -1) {
                  const meta = messages.value[existingIdx].metadata as any
                  if (meta) {
                    meta.event = {
                      ...meta.event,
                      ...event
                    }
                  }
                } else {
                  messages.value.push({
                    id: `tc-${event.exec_id || Date.now()}`,
                    role: 'assistant',
                    content: '',
                    timestamp: new Date(),
                    metadata: {
                      kind: 'tool_call',
                      event: event
                    }
                  })
                }

                if (event.status === 'pending') {
                  devLog('tool_call', '服务端要求人工确认，等待用户操作', {
                    exec_id: event.exec_id,
                    risk_level: event.risk_level,
                    auto_execute_mode: autoExecuteMode.value,
                  })
                }
              } catch (e) {
                console.warn('[tool_call] 解析/处理失败:', e)
              }
            } else if (pendingEventType === 'tool_result') {
              try {
                const event = JSON.parse(data)
                const existingIdx = messages.value.findIndex(m => {
                  const meta = m.metadata as any
                  return meta?.kind === 'tool_call' && meta?.event?.exec_id === event.exec_id
                })
                if (existingIdx !== -1) {
                  const meta = messages.value[existingIdx].metadata as any
                  if (meta) {
                    meta.event = {
                      ...meta.event,
                      ...event
                    }
                  }
                } else {
                  messages.value.push({
                    id: `tr-${event.exec_id || Date.now()}`,
                    role: 'assistant',
                    content: '',
                    timestamp: new Date(),
                    metadata: {
                      kind: 'tool_call',
                      event: event
                    }
                  })
                }
              } catch (e) {
                console.warn('[tool_result] 解析/处理失败:', e)
              }
            } else if (pendingEventType === 'agent_exec_command') {
              // T-TOOL-04 + T-TOOL-18: Agent 命令执行请求（SSE → WebSocket → POST 结果）
              try {
                const event = JSON.parse(data)
                const { execId, command, reason, riskLevel, nodeIp, container, caseId, conversationId: convId, traceId } = event

                devLog('agent_exec_command', '收到执行请求', { execId, riskLevel, commandPreview: command.substring(0, 50) })

                // risk=3：直接拒绝（高危操作）
                if (riskLevel >= 3) {
                  devLog('agent_exec_command', '高危操作，直接拒绝', { execId })
                  // 展示拒绝消息气泡
                  messages.value.push({
                    id: `exec-reject-${Date.now()}`,
                    role: 'system',
                    content: `⚠️ 高危命令已自动拒绝：\n\`\`\`\n${command}\n\`\`\`\n`,
                    timestamp: new Date(),
                    metadata: { kind: 'exec_blocked', execId, command, reason },
                  })
                  postExecResult(convId, execId, '高危操作已自动阻止', -1, 'blocked').catch((e) => {
                    console.warn('[agent_exec_command] postExecResult 失败:', e)
                  })
                  continue
                }

                // risk=1：低风险命令，直接执行（安全操作）
                if (riskLevel === 1) {
                  devLog('agent_exec_command', 'risk=1 命令自动执行', { execId, command })
                  // 检查 SSH 连接状态
                  if (sshConnectionState.value !== 'connected' || !sshWebSocket.value) {
                    devLog('agent_exec_command', 'SSH 未连接，无法执行', { execId })
                    postExecResult(convId, execId, 'SSH 未连接', -1, 'ssh_not_connected').catch((e) => {
                      console.warn('[agent_exec_command] postExecResult 失败:', e)
                    })
                    continue
                  }
                  // 通过 terminal_bridge WebSocket 发送命令 (双通道：隔离执行)
                  const wsMsg = buildAgentExecProcessMessage(caseId, execId, command, nodeIp, container, traceId)
                  sshWebSocket.value.send(wsMsg)
                  devLog('agent_exec_command', '命令已发送到 Bridge', { execId, nodeIp, container })
                  // 监听 exec_result（带超时 30s）
                  waitForExecResult(execId, 30_000)
                    .then((result) => {
                      devLog('agent_exec_command', '执行完成', { execId, exitCode: result.exitCode })
                      return postExecResult(convId, execId, result.output, result.exitCode, undefined, result.stdout, result.stderr)
                    })
                    .catch(() => {
                      devLog('agent_exec_command', '执行超时', { execId })
                      return postExecResult(convId, execId, 'timeout', -1, 'timeout')
                    })
                  continue
                }

                // risk=2：中风险命令，展示确认卡片（气泡形式）
                const execConfirmMsgId = `exec-confirm-${execId ?? Date.now()}`
                messages.value.push({
                  id: execConfirmMsgId,
                  role: 'assistant',
                  content: '',
                  timestamp: new Date(),
                  metadata: {
                    kind: 'exec_confirm',
                    event: {
                      execId,
                      command,
                      reason,
                      riskLevel,
                      nodeIp,
                      caseId,
                      convId,
                      timestamp: Date.now(),
                    },
                  },
                })
                pendingExecConfirm.value = {
                  execId,
                  command,
                  reason,
                  riskLevel,
                  nodeIp,
                  caseId,
                  convId,
                  timestamp: Date.now(),
                }
                devLog('agent_exec_command', 'risk=2 等待用户确认', { execId, command, nodeIp })
              } catch (e) {
                console.warn('[agent_exec_command] 处理失败:', e)
              }
            } else {
              try {
                const parsed = JSON.parse(data)
                messages.value[idx].content += parsed.content || ''
              } catch (e) {
                // Fallback for unformatted raw data (backward compatibility)
                messages.value[idx].content += data
              }
            }
            pendingEventType = 'message'
          } else if (line === '') {
            pendingEventType = 'message'
          }
        }
      }
    } catch (e: any) {
      const idx = getAiMsgIndex()
      if (idx !== -1 && !messages.value[idx].content) {
        messages.value[idx].content = `[AI 响应失败: ${e.message}]`
      }
    } finally {
      const idx = getAiMsgIndex()
      if (idx !== -1) {
        // 【方案A修复】先等 Vue 刷完流式阶段最后一帧 DOM（含 content 更新），
        // 再设 isStreaming: false，保证阶段3的 contentSegments 能在稳定 content 上求值。
        // 消除"实时输出停留在阶段2、刷新后才呈现阶段3"的竞态窗口。
        await nextTick()
        messages.value[idx] = { ...messages.value[idx], isStreaming: false }
      }
      // 将「AI 报告气泡」移到消息列表末尾：流式期间诊断步骤（tool_call /
      // interactive_request / agent_exec_command 等）被 push 到它之后，导致报告
      // 气泡（最先创建、报告文本最后才写入）视觉上压在诊断卡片之上，呈现
      // "先报告后诊断"。移到末尾后顺序变为：诊断步骤 → 工具执行 → 最终报告（含 KBD 链接），
      // 符合"先诊断、匹配信号后再提示 KBD 链接"的预期。
      const reportIdx = getAiMsgIndex()
      if (reportIdx !== -1 && reportIdx < messages.value.length - 1) {
        const [reportMsg] = messages.value.splice(reportIdx, 1)
        messages.value.push(reportMsg)
      }
      isStreaming.value = false
    }
  }


  /**
   * 重新消费 ops-agent 续写事件流（不提交新 prompt）。
   *
   * 适用场景：
   * 1. 用户提交 interactive_response 后立即调用，接收 ops-agent 的续写内容
   * 2. 页面刷新后检测到 interactive_response 是最后一条用户消息且无后续 AI 回复时自动调用
   *
   * 若 ops-agent 中 active_prompt=False 或 session 不存在，后端立即返回 [DONE]，不挂起。
   */
  async function resumeOpsAgentStream(): Promise<void> {
    if (!conversationId.value) return
    // 防重入：isStreaming=true 时说明已有流在运行（场景A/B同时触发时去重）
    if (isStreaming.value) return

    isStreaming.value = true
    const aiMsgId = `ai-resume-${Date.now()}`
    messages.value.push({
      id: aiMsgId,
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      isStreaming: true,
    })

    const getAiMsgIndex = () => messages.value.findIndex((m) => m.id === aiMsgId)

    try {
      const response = await fetch(`/api/conversations/${conversationId.value}/resume-stream`, {
        headers: { 'X-Client-ID': clientId },
      })
      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''
      let pendingEventType = 'message'

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('event: ')) {
            pendingEventType = line.slice(7).trim()
          } else if (line.startsWith('data: ')) {
            const data = line.slice(6)
            if (data === '[DONE]') {
              pendingEventType = 'message'
              continue
            }
            if (pendingEventType === 'interactive_request') {
              try {
                const event = JSON.parse(data)
                const irEvent = {
                  requestId: event.requestId,
                  acpSessionId: event.acpSessionId,
                  kind: event.kind,
                  title: event.title ?? '',
                  prompt: event.prompt ?? '',
                  options: event.options ?? [],
                  customInput: event.customInput ?? true,
                  metadata: event.metadata ?? {},
                  execId: event.execId,
                  inputHash: event.inputHash,
                  expiresAt: event.expiresAt,
                }
                const irMsgId = `ir-${event.requestId ?? Date.now()}`
                messages.value.push({
                  id: irMsgId,
                  role: 'assistant',
                  content: event.prompt ?? event.title ?? '',
                  timestamp: new Date(),
                  metadata: { kind: 'interactive_request', event: irEvent },
                })
                pendingInteractive.value = irEvent as any
              } catch (e) {
                console.warn('[resumeOpsAgentStream] interactive_request 解析失败:', e)
              }
            } else if (pendingEventType === 'stage_change') {
              try {
                const event = JSON.parse(data)
                diagnosticStage.value = event.to ?? diagnosticStage.value
              } catch { }
            } else {
              try {
                const parsed = JSON.parse(data)
                const idx2 = getAiMsgIndex()
                if (idx2 !== -1) {
                  messages.value[idx2].content += parsed.content || ''
                }
              } catch { }
            }
            pendingEventType = 'message'
          } else if (line === '') {
            pendingEventType = 'message'
          }
        }
      }
    } catch (e: any) {
      console.warn('[resumeOpsAgentStream] 连接失败:', e)
    } finally {
      const idx = getAiMsgIndex()
      if (idx !== -1) {
        await nextTick()
        // 若续写内容为空（ops-agent 未活跃），移除空占位气泡
        if (!messages.value[idx].content) {
          messages.value.splice(idx, 1)
        } else {
          messages.value[idx] = { ...messages.value[idx], isStreaming: false }
        }
      }
      isStreaming.value = false
    }
  }

  async function handleCloseCase() {
    if (!currentCase.value) {
      addSystemMessage('当前没有活跃的工单。')
      return
    }
    try {
      const res = await caseApi.close(currentCase.value.case_id, { close_reason: 'user_command' })
      currentCase.value = res.data
      addSystemMessage(`工单 ${res.data.case_id} 已关闭。发送新消息开启新工单。`)
      const convId = conversationId.value
      conversationId.value = null
      // 重置诊断阶段 + 清除交互卡片
      diagnosticStage.value = 'S0'
      pendingInteractive.value = null
      if (convId) {
        ratingConversationId.value = convId
        showRatingCard.value = true
      }
    } catch (e: any) {
      addSystemMessage(`关闭工单失败: ${e.response?.data?.detail || e.message}`)
    }
  }

  function startNewConversation() {
    currentCase.value = null
    conversationId.value = null
    messages.value = []
    diagnosticStage.value = 'S0'
    pendingInteractive.value = null
    addSystemMessage('请描述您遇到的新问题，我会帮您创建工单。')
  }

  async function openHistoryDrawer() {
    showHistoryDrawer.value = true
    try {
      const res = await caseApi.listByClient(clientId)
      existingCases.value = res.data
    } catch (e) {
      console.error('加载历史工单列表失败', e)
    }
  }

  function closeHistoryDrawer() {
    showHistoryDrawer.value = false
    historyMessages.value = []
    historyCase.value = null
  }

  async function loadHistoryMessages(caseItem: CaseResponse) {
    historyCase.value = caseItem
    historyLoading.value = true
    historyMessages.value = []
    try {
      const convRes = await apiClient.get(`/conversations/case/${caseItem.case_id}`)
      const conversations = convRes.data as any[]
      if (conversations.length > 0) {
        const conv = conversations[0]
        const msgRes = await conversationApi.getMessages(conv.conversation_id)
        const history: MessageResponse[] = msgRes.data
        historyMessages.value = history.map((m) => ({
          id: m.message_id,
          role: m.role as ChatMessage['role'],
          content: m.content,
          timestamp: new Date(m.created_at),
          metadata: (m as any).metadata ?? undefined,
        }))
      }
      if (historyMessages.value.length === 0) {
        historyMessages.value = [{
          id: 'sys-empty',
          role: 'system',
          content: '该工单没有对话记录。',
          timestamp: new Date(),
        }]
      }
    } catch (e) {
      console.error('加载历史消息失败', e)
      historyMessages.value = [{
        id: 'sys-error',
        role: 'system',
        content: '加载对话记录失败，请稍后重试。',
        timestamp: new Date(),
      }]
    } finally {
      historyLoading.value = false
    }
  }

  async function switchToCase(caseItem: CaseResponse) {
    closeHistoryDrawer()
    if (!['closed', 'cancelled'].includes(caseItem.status)) {
      currentCase.value = caseItem
      conversationId.value = null
      messages.value = []
      pendingInteractive.value = null  // 切换工单时清除旧交互卡片
      environmentContext.value = null  // 先清空，避免展示旧工单的环境数据

      // 从工单数据恢复助手选择
      _restoreAssistantFromCase(caseItem)

      await loadConversationHistory(caseItem.case_id)
      // 切换工单时同步加载对应环境数据（fire-and-forget）
      collectEnvironmentData(caseItem.case_id).catch(() => { })
    }
  }

  function addUserMessage(content: string, metadata?: any) {
    messages.value.push({
      id: `user-${Date.now()}`,
      role: 'user',
      content,
      timestamp: new Date(),
      metadata,
    })
  }

  function addSystemMessage(content: string) {
    messages.value.push({
      id: `sys-${Date.now()}`,
      role: 'system',
      content,
      timestamp: new Date(),
    })
  }

  function closeRatingCard() {
    showRatingCard.value = false
    ratingConversationId.value = null
  }

  async function submitRating(score: number) {
    if (!ratingConversationId.value) {
      closeRatingCard()
      return
    }
    try {
      await evaluateApi.submit(ratingConversationId.value, { score })
    } catch (e) {
      console.warn('评分提交失败:', e)
    } finally {
      closeRatingCard()
    }
  }

  function skipRating() {
    closeRatingCard()
  }

  /** 发送命令到终端（CommandBlock 手动模式调用，仅填充输入框） */
  function sendCommandToTerminal(command: string) {
    terminalInputCommand.value = command
    showTerminalSidebar.value = true
  }

  function clearTerminalInput() {
    terminalInputCommand.value = ''
  }

  /**
   * 设置自动执行模式并持久化
   */
  function setAutoExecuteMode(mode: 'off' | 'safe-only' | 'aggressive') {
    autoExecuteMode.value = mode
    localStorage.setItem(AUTO_EXEC_MODE_KEY, mode)
    // 切换到 off 时重置计数器
    if (mode === 'off') {
      autoExecCount.value = 0
      consecutiveAutoFailures.value = 0
    }
    devLog('AUTO-EXEC', `模式切换为 ${mode}`)
  }

  /**
   * 自动执行命令（通过全局 sshWebSocket + marker 协议）
   * 串行锁保证同一时刻只有一条命令在执行
   * 命令完成后结果作为 role:user 消息注入对话并触发 AI 继续分析
   *
   * @param command 命令内容
   * @param blockId CommandBlock 的唯一 ID（用于 UI 状态绑定）
   * @returns 执行结果（output + exitCode）
   */
  async function executeCommandViaSSH(
    command: string,
    blockId: string,
  ): Promise<{ output: string; exitCode: number }> {
    // === 前提条件检查 ===
    if (sshConnectionState.value !== 'connected' || !sshWebSocket.value) {
      throw new Error('SSH 未连接，无法自动执行命令')
    }
    if (!currentCase.value) {
      throw new Error('无当前工单，无法自动执行命令')
    }
    if (isExecutingCommand.value) {
      throw new Error('有命令正在执行中（串行锁），请稍后重试')
    }

    // === Agent Loop 防护：会话计数上限 ===
    if (autoExecCount.value >= AUTO_EXEC_MAX) {
      autoExecuteMode.value = 'off'
      localStorage.setItem(AUTO_EXEC_MODE_KEY, 'off')
      addSystemMessage(
        `[自动执行] 本次会话已累计自动执行 ${AUTO_EXEC_MAX} 条命令，已自动关闭。如需继续，请手动重新开启。`,
      )
      throw new Error(`已达到单会话自动执行上限 (${AUTO_EXEC_MAX})`)
    }

    const caseId = currentCase.value.case_id

    // === 获取串行锁 ===
    isExecutingCommand.value = true
    executingCommand.value = { command, startedAt: new Date(), blockId }

    try {
      // 构建 marker（用于从输出流中精确识别命令结束）
      const marker = buildBridgeMarker(caseId, 'autoexec', autoExecCount.value)
      const payload = buildBridgeCommandPayload(command, marker)

      devLog('AUTO-EXEC', '发送命令', { command: command.substring(0, 50), marker })

      // 记录发送前的 buffer 偏移，避免从历史输出中误匹配 marker
      const bufferOffsetBeforeSend = sshOutputBuffer.value.length

      // 发送命令到 SSH 对端
      sshWebSocket.value.send(buildInputMessage(caseId, payload))
      autoExecCount.value++

      // === 等待命令执行完成（marker 出现在 sshOutputBuffer 的新部分） ===
      const result = await new Promise<{ output: string; exitCode: number }>((resolve, reject) => {
        const timeoutMs = 30000
        const pollIntervalMs = 200
        let elapsed = 0

        const poll = setInterval(() => {
          elapsed += pollIntervalMs

          // 超时检查
          if (elapsed >= timeoutMs) {
            clearInterval(poll)
            reject(new Error(`命令执行超时（${timeoutMs / 1000}s）`))
            return
          }

          // 仅在发送命令后的新输出中解析 marker，避免历史 buffer 污染
          const newOutput = sshOutputBuffer.value.slice(bufferOffsetBeforeSend)
          const parsed = parseBridgeCommandResult(newOutput, marker)
          if (parsed) {
            clearInterval(poll)
            resolve(parsed)
          }
        }, pollIntervalMs)
      })

      devLog('AUTO-EXEC', '命令执行完成', { exitCode: result.exitCode, outputLen: result.output.length })

      // === 连续失败熔断 ===
      if (result.exitCode !== 0) {
        consecutiveAutoFailures.value++
        if (consecutiveAutoFailures.value >= AUTO_EXEC_FAILURE_BREAKER) {
          autoExecuteMode.value = 'off'
          localStorage.setItem(AUTO_EXEC_MODE_KEY, 'off')
          addSystemMessage(
            `[自动执行] 连续 ${AUTO_EXEC_FAILURE_BREAKER} 次命令失败，已自动关闭。请检查命令后手动继续。`,
          )
        }
      } else {
        consecutiveAutoFailures.value = 0
      }

      // === 结果注入对话上下文 ===
      const MAX_OUTPUT_LINES = 200
      const outputLines = result.output.split('\n')
      const truncated = outputLines.length > MAX_OUTPUT_LINES
      const displayOutput = truncated
        ? outputLines.slice(0, MAX_OUTPUT_LINES).join('\n') +
          `\n（输出已截断，仅显示前 ${MAX_OUTPUT_LINES} 行，完整输出见终端历史）`
        : result.output

      const exitLabel = result.exitCode === 0 ? '' : `\n退出码: ${result.exitCode}`
      const injectContent =
        `[命令自动执行结果] #${autoExecCount.value}\n` +
        `$ ${command}\n` +
        exitLabel +
        `\n\n${displayOutput}`

      // 追加为 role:user 消息（AI 可感知）
      addUserMessage(injectContent)

      // 确保 conversation 存在再触发 AI 分析
      if (!conversationId.value) {
        await createConversation()
      }
      // 触发 AI 继续分析（不 await，让 AI 在后台响应）
      streamAIResponse(injectContent).catch((e) => {
        console.warn('[AUTO-EXEC] streamAIResponse 失败', e)
      })

      return result
    } finally {
      // === 释放串行锁 ===
      isExecutingCommand.value = false
      executingCommand.value = null
    }
  }

  /**
   * 初始化 Page Visibility 监听（页面后台时暂停自动执行命令队列）
   * 在 store 初始化时调用
   */
  function initPageVisibility() {
    document.addEventListener('visibilitychange', () => {
      pageVisible.value = document.visibilityState === 'visible'
      if (pageVisible.value) {
        devLog('AUTO-EXEC', '页面恢复可见')
      } else {
        devLog('AUTO-EXEC', '页面切入后台，自动执行将暂停派发')
      }
    })
  }

  // 终端输出回填到助手输入框
  function setAssistantDraftText(text: string) {
    assistantDraftText.value = text
  }

  /** 提交高风险操作确认结果（由 ConfirmDialog 调用） */
  async function handleConfirmResult(authorized: boolean) {
    if (!pendingConfirm.value || !conversationId.value) {
      pendingConfirm.value = null
      return
    }
    // T1-2：旧实现误用 `/confirm` 路由（后端不存在）且未回传 exec_id/input_hash，
    // 导致 ConfirmDialog 弹窗确认完全无效。改为命中 `interactive-response` 并附带
    // request_id=exec_id + input_hash，与 MessageBubble 的工具卡片确认路径保持一致。
    const { exec_id, input_hash } = pendingConfirm.value
    if (!exec_id) {
      console.warn('提交确认失败：缺少 exec_id，无法定位 ReAct 待确认事务')
      pendingConfirm.value = null
      return
    }
    try {
      await fetch(`/api/conversations/${conversationId.value}/interactive-response`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Client-ID': clientId,
        },
        body: JSON.stringify({
          kind: 'tool_confirm',
          request_id: exec_id,
          acp_session_id: conversationId.value,
          outcome: {
            confirmed: authorized,
            authorized_by: clientId,
            input_hash,
          },
          metadata: { input_hash },
        }),
      })
    } catch (e) {
      console.warn('提交确认结果失败:', e)
    } finally {
      pendingConfirm.value = null
    }
  }

  function clearAssistantDraftText() {
    assistantDraftText.value = ''
  }

  /**
   * 点击「终端」按钮的入口
   * 先检测 Bridge，根据结果决定：直接打开侧边栏 或 提示下载
   */
  async function checkAndOpenTerminal() {
    bridgeStatus.value = 'checking'
    const running = await checkBridgeRunning()
    if (running) {
      bridgeStatus.value = 'running'
      showTerminalSidebar.value = true
    } else {
      bridgeStatus.value = 'not_running'
      // 让 App.vue 展示下载提示弹窗（通过 showBridgeDownload 状态驱动）
      showBridgeDownload.value = true
    }
  }

  function openTerminalSidebar() {
    showTerminalSidebar.value = true
  }

  function closeTerminalSidebar() {
    showTerminalSidebar.value = false
  }

  // Bridge 下载提示弹窗状态
  const showBridgeDownload = ref(false)
  function closeBridgeDownload() {
    showBridgeDownload.value = false
  }

  // === 统一 SSH 连接弹框状态 ===
  /** 弹框是否可见 */
  const sshFlowDialogVisible = ref(false)
  /** 关联的工单 ID（terminal-only 模式下可能为 null） */
  const sshFlowDialogCaseId = ref<string | null>(null)
  /** 弹框模式：create-case 或 terminal-only */
  const sshFlowDialogMode = ref<'create-case' | 'terminal-only'>('terminal-only')
  /** 创建工单时选择的助手类型（供弹框用于 completeCaseCreationFlow） */
  const sshFlowDialogAssistantType = ref<string | undefined>(undefined)

  /** 打开统一 SSH 弹框 */
  function openSshFlowDialog(
    caseId: string | null,
    mode: 'create-case' | 'terminal-only',
    assistantType?: string,
  ) {
    devLog('SSH-FLOW-DIALOG', '打开弹框', { caseId, mode, assistantType })
    sshFlowDialogCaseId.value = caseId
    sshFlowDialogMode.value = mode
    sshFlowDialogAssistantType.value = assistantType
    sshFlowDialogVisible.value = true
  }

  /** 关闭统一 SSH 弹框（create-case 模式下回退到普通对话流程） */
  async function closeSshFlowDialog() {
    const caseId = sshFlowDialogCaseId.value
    const mode = sshFlowDialogMode.value
    const assistantType = sshFlowDialogAssistantType.value

    sshFlowDialogVisible.value = false

    // create-case 模式下用户取消/无 SSH → 回退到普通对话流程，避免工单卡死
    if (mode === 'create-case' && caseId && currentCase.value?.case_id === caseId) {
      const userMessage = pendingUserMessage.value || currentCase.value.description || ''
      if (userMessage && !conversationId.value) {
        devLog('SSH-FLOW-DIALOG', '用户取消 SSH，回退到普通对话流程', { caseId })
        try {
          await completeCaseCreationFlow(caseId, userMessage, assistantType)
        } catch (e: any) {
          addSystemMessage(`创建对话失败: ${e.message || '未知错误'}`)
        }
      }
    }
  }

  /**
   * 创建工单并打开 SSH 连接弹框（CaseCreateDialog 调用）
   * 执行步骤：创建工单 → 确认工单 → 打开 SshConnectDialog（携带 caseId）
   */
  async function createCaseAndOpenSsh(params: {
    title: string
    description: string
    assistantType?: string
  }) {
    devLog('createCaseAndOpenSsh', '创建工单', params)
    showCaseTemplate.value = false

    try {
      const res = await caseApi.create({
        client_id: clientId,
        title: params.title,
        description: params.description,
        assistant_type: params.assistantType || selectedAssistant.value || undefined,
      })
      currentCase.value = res.data
      const caseId = res.data.case_id
      devLog('createCaseAndOpenSsh', '工单创建成功', { caseId })

      openSshFlowDialog(caseId, 'create-case', params.assistantType)
    } catch (e: any) {
      const detail = e.response?.data?.detail || e.message
      console.error('[createCaseAndOpenSsh] 创建工单失败', detail)
      throw e
    }
  }



  /** 认证超时定时器 */
  let sshAuthTimer: number | null = null
  const SSH_AUTH_TIMEOUT = 15000
  /** SSH 连接稳定判定窗口：避免远端 Shell 启动后立即退出时误报成功 */
  const SSH_STABILITY_DELAY = 800

  /** 清除认证超时定时器 */
  function clearSshAuthTimer() {
    if (sshAuthTimer !== null) {
      window.clearTimeout(sshAuthTimer)
      sshAuthTimer = null
    }
  }

  /** 清理 SSH WebSocket */
  function cleanupSshWebSocket() {
    clearSshAuthTimer()
    if (sshWebSocket.value) {
      sshWebSocket.value.onopen = null
      sshWebSocket.value.onmessage = null
      sshWebSocket.value.onerror = null
      sshWebSocket.value.onclose = null
      sshWebSocket.value.close()
      sshWebSocket.value = null
    }
  }

  /** 全局 SSH 连接方法 */
  function connectSSH(config: {
    host: string
    port: number
    username: string
    authType: 'password' | 'key'
    password?: string
    privateKey?: string
    passphrase?: string
    caseId: string
  }): Promise<void> {
    devLog('SSH', '开始连接', {
      host: config.host,
      port: config.port,
      username: config.username,
      caseId: config.caseId,
      timestamp: new Date().toISOString()
    })

    return new Promise((resolve, reject) => {
      let settled = false
      let stabilityTimer: number | null = null
      let socket: WebSocket

      function clearStabilityTimer() {
        if (stabilityTimer !== null) {
          window.clearTimeout(stabilityTimer)
          stabilityTimer = null
        }
      }

      function failConnect(error: Error, state: 'error' | 'disconnected' = 'error') {
        if (settled) {
          sshConnectionState.value = state
          cleanupSshWebSocket()
          return
        }
        settled = true
        clearStabilityTimer()
        sshConnectionState.value = state
        cleanupSshWebSocket()
        reject(error)
      }

      function markConnected() {
        if (settled || sshWebSocket.value !== socket || socket.readyState !== WebSocket.OPEN) return
        settled = true
        clearStabilityTimer()
        sshConnectionState.value = 'connected'
        resolve()
      }

      // 清理旧连接
      cleanupSshWebSocket()
      sshOutputBuffer.value = ''
      sshErrorMessage.value = ''
      sshConnectionState.value = 'connecting'
      sshCurrentConfig.value = {
        host: config.host,
        port: config.port,
        username: config.username,
        authType: config.authType,
        caseId: config.caseId,
      }

      // 创建 WebSocket
      socket = createBridgeSocket()
        setupWebSocketResumeHandler(socket)  // 缺陷 6: 重连时自动发送 resume
      sshWebSocket.value = socket

      socket.onopen = () => {
        devLog('SSH', 'WebSocket 已打开，发送连接命令')
        clearSshAuthTimer()

        // 15 秒认证超时
        sshAuthTimer = window.setTimeout(() => {
          if (sshConnectionState.value === 'connecting') {
            devLog('SSH', 'ERROR: 认证超时')
            sshErrorMessage.value = 'SSH 认证超时（15秒）'
            failConnect(new Error('SSH 认证超时'))
          }
        }, SSH_AUTH_TIMEOUT)

        // 发送 SSH 连接命令
        socket.send(buildConnectMessage({
          host: config.host,
          port: config.port,
          username: config.username,
          auth_type: config.authType,
          password: config.password,
          private_key: config.privateKey,
          passphrase: config.passphrase,
          case_id: config.caseId,
        }))
      }

      socket.onmessage = (e) => {
        let msg: TerminalWsMessage
        try {
          msg = JSON.parse(String(e.data || ''))
        } catch {
          console.warn('[SSH] 无法解析消息:', e.data)
          return
        }

        devLog('SSH', '收到消息', { type: msg.type })

        if (msg.case_id && msg.case_id !== config.caseId) {
          devLog('SSH', '消息 case_id 不匹配，忽略')
          return
        }

        if (msg.type === 'ssh_connected') {
          devLog('SSH', '远程会话已建立，等待稳定确认')
          clearSshAuthTimer()
          clearStabilityTimer()
          stabilityTimer = window.setTimeout(() => {
            markConnected()
          }, SSH_STABILITY_DELAY)
        } else if (msg.type === 'ssh_output' && msg.output) {
          sshOutputBuffer.value += msg.output
          if (sshConnectionState.value === 'connecting') {
            markConnected()
          }
          // 如果有消费者，触发输出事件
          if (sshCommandConsumer.value) {
            sshTerminalOutputEvent.value = msg.output
          }
        } else if (msg.type === 'ssh_error') {
          devLog('SSH', 'ERROR: 收到错误', { message: msg.message })
          clearSshAuthTimer()
          sshErrorMessage.value = msg.message || 'SSH 连接出错'
          failConnect(new Error(msg.message || 'SSH 连接出错'))
        } else if (msg.type === 'ssh_disconnected') {
          devLog('SSH', '连接断开')
          clearSshAuthTimer()
          if (sshConnectionState.value === 'connecting') {
            sshErrorMessage.value = msg.message || 'SSH 连接建立后立即断开，请检查远端 Shell 或 Bridge 日志'
            failConnect(new Error(sshErrorMessage.value))
          } else if (sshConnectionState.value === 'connected') {
            sshConnectionState.value = 'disconnected'
            cleanupSshWebSocket()
          }
        } else if (msg.type === 'exec_stdout' && msg.exec_id && msg.stdout) {
          const buf = execBuffers.get(msg.exec_id) || { stdout: '', stderr: '' }
          buf.stdout += msg.stdout
          execBuffers.set(msg.exec_id, buf)
        } else if (msg.type === 'exec_stderr' && msg.exec_id && msg.stderr) {
          const buf = execBuffers.get(msg.exec_id) || { stdout: '', stderr: '' }
          buf.stderr += msg.stderr
          execBuffers.set(msg.exec_id, buf)
        } else if (msg.type === 'bridge_log') {
          // terminal_bridge 结构化回采日志（OBS-TERMINAL-BRIDGE-001）
          forwardBridgeLog(msg as unknown as Record<string, unknown>)
        } else if (msg.type === 'exec_result') {
          // Agent 命令执行结果回调
          const parsed = parseAgentExecResult(msg)
          if (parsed) {
            const accumulated = execBuffers.get(parsed.execId)
            if (accumulated) {
              if (parsed.stdout === undefined) parsed.stdout = accumulated.stdout
              if (parsed.stderr === undefined) parsed.stderr = accumulated.stderr
              execBuffers.delete(parsed.execId)
            }
            devLog('SSH', '收到 exec_result', { execId: parsed.execId, exitCode: parsed.exitCode })
            const callback = pendingExecCallbacks.get(parsed.execId)
            if (callback) {
              clearTimeout(callback.timeoutId)
              pendingExecCallbacks.delete(parsed.execId)
              callback.resolve(parsed)
            }
          }
        }
      }

      socket.onerror = () => {
        devLog('SSH', 'ERROR: WebSocket 错误')
        clearSshAuthTimer()
        sshErrorMessage.value = 'SSH Bridge 未运行（ws://localhost:9999）'
        failConnect(new Error('SSH Bridge 未运行'))
      }

      socket.onclose = () => {
        devLog('SSH', 'WebSocket 关闭')
        clearSshAuthTimer()
        clearStabilityTimer()
        if (sshConnectionState.value === 'connecting') {
          sshErrorMessage.value = 'SSH 连接建立后立即关闭，请检查 terminal_bridge 或远端 Shell'
          failConnect(new Error(sshErrorMessage.value))
        } else if (sshConnectionState.value !== 'error') {
          sshConnectionState.value = 'disconnected'
        }
      }
    })
  }

  /** 断开 SSH 连接 */
  function disconnectSSH() {
    devLog('SSH', '断开连接')
    if (sshWebSocket.value && sshConnectionState.value === 'connected') {
      sshWebSocket.value.send(buildDisconnectMessage(sshCurrentConfig.value?.caseId || ''))
    }
    cleanupSshWebSocket()
    sshConnectionState.value = 'disconnected'
    sshCurrentConfig.value = null
    sshOutputBuffer.value = ''
    sshCommandConsumer.value = null
    // 清理所有待处理的 exec_result 回调
    pendingExecCallbacks.forEach((callback) => {
      clearTimeout(callback.timeoutId)
      callback.reject(new Error('SSH 连接已断开'))
    })
    pendingExecCallbacks.clear()
  }

  /**
   * 等待 exec_result WebSocket 消息（带超时）
   * 用于 agent_exec_command SSE 事件处理
   *
   * @param execId 执行 ID
   * @param timeoutMs 超时时间（毫秒）
   * @returns Promise<{output, exitCode}>
   */
  function waitForExecResult(
    execId: string,
    timeoutMs: number,
  ): Promise<{ output: string; exitCode: number; stdout?: string; stderr?: string }> {
    return new Promise((resolve, reject) => {
      // 检查 SSH 连接状态
      if (sshConnectionState.value !== 'connected' || !sshWebSocket.value) {
        reject(new Error('SSH 未连接'))
        return
      }

      // 设置超时
      const timeoutId = window.setTimeout(() => {
        pendingExecCallbacks.delete(execId)
        reject(new Error(`等待 exec_result 超时（${timeoutMs / 1000}s）`))
      }, timeoutMs)

      // 注册回调
      pendingExecCallbacks.set(execId, { resolve, reject, timeoutId })

      devLog('SSH', '等待 exec_result', { execId, timeoutMs })
    })
  }

  /**
   * POST 执行结果到后端 API
   * 用于 agent_exec_command SSE 事件处理
   *
   * @param convId 会话 ID
   * @param execId 执行 ID
   * @param output 命令输出
   * @param exitCode 退出码
   * @param status 执行状态（可选，用于风险拒绝场景）
   */
  async function postExecResult(
    convId: string,
    execId: string,
    output: string,
    exitCode: number,
    status?: string,
    stdout?: string,
    stderr?: string,
  ): Promise<void> {
    try {
      const response = await fetch(`/api/conversations/${convId}/exec-result`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Client-ID': clientId,
        },
        body: JSON.stringify({
          exec_id: execId,
          output: status ? `${status}: ${output}` : output,
          exit_code: exitCode,
          stdout: stdout || undefined,
          stderr: stderr || undefined,
        }),
      })

      if (!response.ok) {
        const errData = await response.json().catch(() => ({ detail: '未知错误' }))
        console.warn('[postExecResult] 回传失败:', errData.detail)
      } else {
        devLog('SSH', 'exec_result 已回传', { execId, exitCode })
      }
    } catch (e) {
      console.warn('[postExecResult] 网络错误:', e)
    }
  }

  /** 发送 SSH 命令 */
  function sendSSHCommand(command: string, consumer: 'terminal' | 'collection') {
    if (!sshWebSocket.value || sshConnectionState.value !== 'connected') {
      console.warn('[SSH] 未连接，无法发送命令')
      return
    }
    devLog('SSH', '发送命令', { command, consumer })
    sshCommandConsumer.value = consumer
    sshWebSocket.value.send(buildInputMessage(sshCurrentConfig.value?.caseId || '', `${command}\n`))
  }

  /** 采集环境数据（SSH 连接成功后自动调用或手动触发） */
  async function collectEnvironmentData(caseId: string) {
    if (collectionState.value === 'collecting') return

    collectionState.value = 'collecting'
    collectionProgress.value = {
      cluster: 'pending',
      alert: 'pending',
      task: 'pending',
    }

    try {
      // 获取环境上下文（包含 cluster/alert/task）
      const res = await environmentApi.getContext(caseId)
      environmentContext.value = res.data

      // 更新进度状态（区分 'done' 有数据、'empty' 无数据、'error' 失败）
      if (res.data.env_info && Object.keys(res.data.env_info).length > 0) {
        collectionProgress.value.cluster = 'done'
      } else {
        collectionProgress.value.cluster = 'empty'  // 成功但无数据
      }

      if (res.data.alert_logs && res.data.alert_logs.length > 0) {
        collectionProgress.value.alert = 'done'
      } else {
        collectionProgress.value.alert = 'empty'  // 无告警也算成功
      }

      if (res.data.task_logs && res.data.task_logs.length > 0) {
        collectionProgress.value.task = 'done'
      } else {
        collectionProgress.value.task = 'empty'  // 无任务也算成功
      }

      // 刷新列表
      const listRes = await environmentApi.listByCase(caseId)
      environmentData.value = listRes.data.items
      collectionState.value = 'success'

      devLog('collectEnvironmentData', '采集完成', { count: environmentData.value.length })
    } catch (e: any) {
      collectionState.value = 'error'
      collectionProgress.value = { cluster: 'error', alert: 'error', task: 'error' }
      console.error('[collectEnvironmentData] 采集失败', e)
    }
  }

  /** 手动刷新环境数据 */
  async function refreshEnvironmentData() {
    if (!currentCase.value) return
    await collectEnvironmentData(currentCase.value.case_id)
  }

  /** 提交环境数据（aClient 采集后调用） */
  async function submitEnvironmentData(data: { case_id: string; env_type: EnvType; env_data: Record<string, unknown>; collected_at?: string }) {
    try {
      const res = await environmentApi.create(data)
      // 刷新列表
      if (currentCase.value) {
        const listRes = await environmentApi.listByCase(currentCase.value.case_id)
        environmentData.value = listRes.data.items
      }
      return res.data
    } catch (e: any) {
      console.error('[submitEnvironmentData] 提交失败', e)
      throw e
    }
  }

  // === SSH 连接状态（创建工单时） ===
  // 注意：状态名称使用 acli_* 而非 acll_* 避免混淆
  const sshCreationPhase = ref<'idle' | 'connecting' | 'connected' | 'acli_check' | 'collecting' | 'done' | 'error' | 'acli_not_found'>('idle')
  const sshCreationError = ref<{ message: string; detail: string } | null>(null)
  const acliAvailable = ref<boolean | null>(null)
  const sshCreationSocket = ref<WebSocket | null>(null)
  const sshCreationFlowId = ref('')
  const sshCreationLogs = ref<SshCreationLogEntry[]>([])

  /** SSH 配置信息 */
  interface SSHConfig {
    host: string
    port: number
    username: string
    password: string
  }

  /** Bridge 命令执行结果 */
  interface BridgeCommandResult {
    output: string
    exitCode: number
  }

  /** 当前待完成的 Bridge 命令 */
  interface PendingBridgeCommand {
    name: string
    marker: string
    buffer: string
    timeoutId: number
    firstChunkLogged: boolean
    resolve: (result: BridgeCommandResult) => void
    reject: (error: Error) => void
  }

  const SSH_CREATION_AUTH_TIMEOUT_MS = 15000
  const SSH_CREATION_COMMAND_TIMEOUT_MS = 12000
  const SSH_CREATION_ACLI_CHECK_TIMEOUT_MS = 5000

  /** 采集命令列表 */
  const COLLECT_COMMANDS = [
    { name: 'cluster', label: '采集集群信息', cmd: 'acli platform info get' },
    { name: 'alert', label: '采集告警列表', cmd: 'acli --formatter json alert list' },
    { name: 'task', label: '采集失败任务列表', cmd: 'acli --formatter json task get -s failed -l 10' },
  ]

  function escapeRegExp(value: string): string {
    return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
  }

  function buildBridgeMarker(caseId: string, name: string, index: number): string {
    const normalizedCaseId = caseId.replace(/[^a-zA-Z0-9]/g, '')
    const normalizedName = name.replace(/[^a-zA-Z0-9]/g, '_')
    return `__HCI_DONE_${normalizedCaseId}_${normalizedName}_${index}_${Date.now()}__`
  }

  function buildBridgeCommandPayload(command: string, marker: string): string {
    return `${command}; status=$?; printf '\\n${marker}:%s\\n' "$status"\n`
  }

  function parseBridgeCommandResult(buffer: string, marker: string): BridgeCommandResult | null {
    const normalized = buffer.replace(/\r/g, '')
    const match = normalized.match(new RegExp(`${escapeRegExp(marker)}:(\\d+)`))
    if (!match || match.index === undefined) return null

    return {
      output: normalized.slice(0, match.index).trim(),
      exitCode: Number(match[1]),
    }
  }

  function createFlowError(message: string, detail = ''): Error & { detail: string } {
    const error = new Error(message) as Error & { detail: string }
    error.detail = detail
    return error
  }

  function buildCommandError(label: string, output: string, exitCode: number): Error & { detail: string } {
    const detail = output.length > 800 ? output.slice(-800) : output
    return createFlowError(`${label}失败（exit=${exitCode}）`, detail)
  }

  function resetSshCreationLogs(flowId = '') {
    sshCreationFlowId.value = flowId
    sshCreationLogs.value = []
  }

  function appendSshCreationLog(
    level: SshCreationLogEntry['level'],
    step: string,
    message: string,
    data?: Record<string, unknown>,
  ) {
    const sanitized = data ? sanitizeSensitive(data) : undefined
    const entry: SshCreationLogEntry = {
      id: `ssh-log-${Date.now()}-${Math.random().toString(36).slice(2, 8)}`,
      timestamp: new Date().toISOString(),
      level,
      step,
      message,
      data: sanitized,
    }

    sshCreationLogs.value = [...sshCreationLogs.value, entry].slice(-100)

    const prefix = sshCreationFlowId.value
      ? `[SSH-CREATE][${sshCreationFlowId.value}][${step}]`
      : `[SSH-CREATE][${step}]`

    // 控制台日志：error 级别始终输出（便于生产环境排查）；info/warn 仅开发环境输出
    if (level === 'error') {
      console.error(prefix, message, sanitized ?? '')
    } else if (isDev) {
      if (level === 'warn') {
        console.warn(prefix, message, sanitized ?? '')
      } else {
        console.info(prefix, message, sanitized ?? '')
      }
    }
  }

  /**
   * 共享流程：创建工单 + 确认 + 建对话 + 首条消息发送
   * 被 confirmCreateCase 和 SSH 流程共用
   */
  async function completeCaseCreationFlow(caseId: string, userMessage: string, assistantType?: string) {
    showCaseTemplate.value = false
    addUserMessage(userMessage)

    // 在调用 createConversation 之前必须先将 currentCase 赋值，
    // 否则 createConversation 内部的 currentCase 为 null 检查会抛出异常。
    // 仅当前工单不存在或与目标 caseId 不匹配时才重新拉取，避免重复请求；
    // 若拉取失败但已有可用 currentCase，则优先回退使用现有工单继续流程。
    const hasMatchingCurrentCase = currentCase.value?.case_id === caseId
    if (!hasMatchingCurrentCase) {
      try {
        const caseRes = await caseApi.getById(caseId)
        currentCase.value = caseRes.data
      } catch (e: any) {
        console.error(`[ChatStore] 加载工单失败: ${caseId}`, e)
        const errorMessage = e.response?.data?.detail || e.message || '未知错误'
        if (!currentCase.value) {
          addSystemMessage(`加载工单 ${caseId} 失败：${errorMessage}，无法继续对话`)
          isLoading.value = false
          pendingUserMessage.value = ''
          return
        }
        // currentCase 已有旧值，继续使用不中断流程
        console.warn(`[ChatStore] 加载工单 ${caseId} 失败，回退使用现有工单 ${currentCase.value.case_id}`)
      }
    }

    addSystemMessage(`工单 ${caseId} 已创建，AI 正在识别故障类型，请稍候…`)

    // 同步 selectedAssistant，确保 UI 显示正确且 streamAIResponse 使用工单绑定的助手
    const resolvedAssistantType = currentCase.value?.assistant_type || assistantType
    if (resolvedAssistantType && assistants.value.some(a => a.type === resolvedAssistantType)) {
      selectedAssistant.value = resolvedAssistantType
    }

    // 创建对话（失败时会抛出错误并显示提示）
    try {
      await createConversation()
    } catch (e) {
      isLoading.value = false
      pendingUserMessage.value = ''
      return  // 创建对话失败，不继续发送消息
    }

    // 发送首条消息
    try {
      await streamAIResponse(userMessage)
    } catch (e: any) {
      addSystemMessage(`AI 响应失败: ${e.message || '未知错误'}`)
    }

    isLoading.value = false
    pendingUserMessage.value = ''
  }

  /** 通过 SSH 连接并创建工单（一站式流程） - 返回 Promise 等待完成 */
  function connectSSHAndCreateCase(
    title: string,
    description: string,
    sshConfig: SSHConfig,
    assistantType?: string,
    userMessage?: string,
  ): Promise<void> {
    // 使用非 async executor + IIFE 模式，避免 async executor anti-pattern
    return new Promise((resolve, reject) => {
      (async () => {
        const flowId = `ssh-create-${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 8)}`
        resetSshCreationLogs(flowId)
        sshCreationPhase.value = 'connecting'
        sshCreationError.value = null
        acliAvailable.value = null
        isLoading.value = true
        appendSshCreationLog('info', 'start', '开始执行 SSH 集成创建工单流程', {
          title,
          assistantType: assistantType || selectedAssistant.value || 'default',
          host: sshConfig.host,
          port: sshConfig.port,
          username: sshConfig.username,
        })

        try {
          // 1. 创建工单
          const res = await caseApi.create({
            client_id: clientId,
            title,
            description,
            assistant_type: assistantType || selectedAssistant.value || undefined,
          })
          currentCase.value = res.data
          const caseId = res.data.case_id
          appendSshCreationLog('info', 'case', '工单创建成功', { caseId, status: res.data.status })

          // C 修复：将临时会话（ssh-create-temp）已采集的回采日志迁移到真实工单
          await importBridgeLogsToCase('ssh-create-temp', caseId)

          // 2. 建立 SSH 连接
          devLog('SSH-CREATE', '开始建立 WebSocket 连接到 Bridge')
          const socket = createBridgeSocket()
          setupWebSocketResumeHandler(socket)  // 缺陷 6: 重连时自动发送 resume
          devLog('SSH-CREATE', 'WebSocket 对象已创建，等待连接')
          sshCreationSocket.value = socket
          appendSshCreationLog('info', 'bridge', '开始连接本地 SSH Bridge', { caseId })

          const collectBuffer: Record<string, string> = { cluster: '', alert: '', task: '' }
          let authTimer: number | null = null
          let pendingCommand: PendingBridgeCommand | null = null
          let flowSettled = false

          const clearAuthTimer = () => {
            if (authTimer !== null) {
              window.clearTimeout(authTimer)
              authTimer = null
            }
          }

          const clearPendingCommand = () => {
            if (!pendingCommand) return
            window.clearTimeout(pendingCommand.timeoutId)
            pendingCommand = null
          }

          const cleanupSocket = () => {
            clearAuthTimer()
            clearPendingCommand()
            if (sshCreationSocket.value) {
              sshCreationSocket.value.onopen = null
              sshCreationSocket.value.onmessage = null
              sshCreationSocket.value.onerror = null
              sshCreationSocket.value.onclose = null
              sshCreationSocket.value.close()
              sshCreationSocket.value = null
            }
          }

          const rejectFlow = (error: unknown) => {
            if (flowSettled) return
            flowSettled = true

            const normalized = error instanceof Error
              ? (error as Error & { detail?: string })
              : createFlowError('SSH 创建流程失败')

            if (pendingCommand) {
              const currentCommand = pendingCommand
              clearPendingCommand()
              currentCommand.reject(normalized)
            }

            sshCreationError.value = {
              message: normalized.message || 'SSH 创建流程失败',
              detail: normalized.detail || '',
            }
            sshCreationPhase.value = 'error'
            isLoading.value = false
            appendSshCreationLog('error', 'flow', sshCreationError.value.message, {
              detail: sshCreationError.value.detail || '',
            })
            cleanupSocket()
            console.error('[SSH-CREATE] 流程失败', normalized.message, normalized.detail || '')
            reject(normalized)
          }

          const runBridgeCommand = (
            name: string,
            command: string,
            timeoutMs = SSH_CREATION_COMMAND_TIMEOUT_MS,
          ): Promise<BridgeCommandResult> => {
            if (flowSettled) {
              return Promise.reject(createFlowError('SSH 创建流程已结束'))
            }
            if (pendingCommand) {
              return Promise.reject(createFlowError(`存在未完成的命令：${pendingCommand.name}`))
            }

            return new Promise((commandResolve, commandReject) => {
              const marker = buildBridgeMarker(caseId, name, Date.now())
              const timeoutId = window.setTimeout(() => {
                const timeoutError = createFlowError(`${name}超时`, `命令执行超过 ${Math.ceil(timeoutMs / 1000)} 秒仍未返回`)
                clearPendingCommand()
                appendSshCreationLog('error', 'command', timeoutError.message, {
                  command: name,
                  timeoutMs,
                })
                commandReject(timeoutError)
              }, timeoutMs)

              pendingCommand = {
                name,
                marker,
                buffer: '',
                timeoutId,
                firstChunkLogged: false,
                resolve: commandResolve,
                reject: commandReject,
              }

              devLog('SSH-CREATE', '发送 Bridge 命令', { name, timeoutMs })
              appendSshCreationLog('info', 'command', '发送 SSH 命令', {
                command: name,
                timeoutMs,
              })
              socket.send(buildInputMessage(caseId, buildBridgeCommandPayload(command, marker)))
            })
          }

          const continueAfterConnected = async () => {
            try {
              sshCreationPhase.value = 'acli_check'

              const acliCheckResult = await runBridgeCommand(
                '检查 acli',
                "command -v acli >/dev/null 2>&1 && printf '__HCI_ACLI_OK__' || printf '__HCI_ACLI_MISSING__'",
                SSH_CREATION_ACLI_CHECK_TIMEOUT_MS,
              )

              if (acliCheckResult.output.includes('__HCI_ACLI_MISSING__')) {
                acliAvailable.value = false
                sshCreationPhase.value = 'acli_not_found'
                appendSshCreationLog('warn', 'acli', '目标主机未安装 acli，跳过环境采集')
              } else if (acliCheckResult.output.includes('__HCI_ACLI_OK__')) {
                acliAvailable.value = true
                sshCreationPhase.value = 'collecting'
                appendSshCreationLog('info', 'acli', '检测到 acli 可用，开始采集环境数据')

                for (const command of COLLECT_COMMANDS) {
                  const result = await runBridgeCommand(command.label, command.cmd)
                  if (result.exitCode !== 0) {
                    throw buildCommandError(command.label, result.output, result.exitCode)
                  }
                  collectBuffer[command.name] = result.output
                  appendSshCreationLog('info', 'collect', '采集命令执行完成', {
                    command: command.name,
                    outputLength: result.output.length,
                  })
                }

                await submitCollectedData(caseId, collectBuffer)
                appendSshCreationLog('info', 'collect', '环境数据已提交到 Environment API', {
                  cluster: Boolean(collectBuffer.cluster),
                  alert: Boolean(collectBuffer.alert),
                  task: Boolean(collectBuffer.task),
                })
                await collectEnvironmentData(caseId)
                appendSshCreationLog('info', 'collect', '环境上下文刷新完成', {
                  caseId,
                })
              } else {
                throw createFlowError('检查 acli 失败', acliCheckResult.output)
              }

              await completeCaseCreationFlow(caseId, userMessage || description, assistantType)
              appendSshCreationLog('info', 'conversation', '工单创建后续流程完成，开始建立终端会话', {
                caseId,
              })

              cleanupSocket()
              await connectSSH({
                host: sshConfig.host,
                port: sshConfig.port,
                username: sshConfig.username,
                authType: 'password',
                password: sshConfig.password,
                caseId,
              })
              appendSshCreationLog('info', 'terminal', '终端侧全局 SSH 会话建立成功', {
                caseId,
              })

              flowSettled = true
              sshCreationPhase.value = 'done'
              isLoading.value = false
              appendSshCreationLog('info', 'done', 'SSH 集成创建工单流程完成', {
                caseId,
              })
              resolve()
            } catch (error) {
              rejectFlow(error)
            }
          }

          socket.onopen = () => {
            devLog('SSH-CREATE', 'WebSocket 连接已打开')
            sshCreationPhase.value = 'connected'
            clearAuthTimer()
            appendSshCreationLog('info', 'bridge', '本地 SSH Bridge 已连接', {
              caseId,
            })

            // 15 秒认证超时
            authTimer = window.setTimeout(() => {
              devLog('SSH-CREATE', '认证超时检查', { phase: sshCreationPhase.value })
              if (sshCreationPhase.value === 'connected' || sshCreationPhase.value === 'acli_check') {
                rejectFlow(createFlowError('SSH 认证超时', '15 秒内未收到认证成功信号'))
              }
            }, SSH_CREATION_AUTH_TIMEOUT_MS)

            // 发送 SSH 连接命令
            const connectMsg = buildConnectMessage({
              host: sshConfig.host,
              port: sshConfig.port,
              username: sshConfig.username,
              auth_type: 'password',
              password: sshConfig.password,
              case_id: caseId,
            })
            devLog('SSH-CREATE', '发送 SSH 连接命令', { host: sshConfig.host, port: sshConfig.port, caseId })
            socket.send(connectMsg)
          }

          socket.onmessage = (e) => {
            devLog('SSH-CREATE', '收到 WebSocket 消息', { raw: e.data })

            let msg: TerminalWsMessage
            try {
              msg = JSON.parse(String(e.data || ''))
              devLog('SSH-CREATE', '解析后', { type: msg.type, case_id: msg.case_id, output: msg.output?.substring(0, 100) })
            } catch (parseErr) {
              devLog('SSH-CREATE', 'ERROR: JSON 解析失败', parseErr)
              return
            }

            if (msg.case_id && msg.case_id !== caseId) {
              devLog('SSH-CREATE', '忽略其他 case_id', { expected: caseId, actual: msg.case_id })
              return
            }

            if (msg.type === 'ssh_connected') {
              devLog('SSH-CREATE', 'SSH 连接成功')
              clearAuthTimer()
              appendSshCreationLog('info', 'ssh', '目标主机 SSH 认证成功', {
                caseId,
                host: sshConfig.host,
                username: sshConfig.username,
              })
              void continueAfterConnected()

            } else if (msg.type === 'ssh_output' && msg.output) {
              if (!pendingCommand) return

              pendingCommand.buffer += msg.output
              if (!pendingCommand.firstChunkLogged) {
                pendingCommand.firstChunkLogged = true
                appendSshCreationLog('info', 'command', '收到命令输出片段', {
                  command: pendingCommand.name,
                  outputPreview: msg.output.substring(0, 120),
                })
              }
              devLog('SSH-CREATE', 'ssh_output', {
                phase: sshCreationPhase.value,
                command: pendingCommand.name,
                outputPreview: msg.output.substring(0, 50),
              })

              const result = parseBridgeCommandResult(pendingCommand.buffer, pendingCommand.marker)
              if (result) {
                const currentCommand = pendingCommand
                clearPendingCommand()
                appendSshCreationLog('info', 'command', '命令执行结束', {
                  command: currentCommand.name,
                  exitCode: result.exitCode,
                  outputPreview: result.output.substring(0, 160),
                })
                currentCommand.resolve(result)
              }
            } else if (msg.type === 'ssh_error') {
              appendSshCreationLog('error', 'ssh', 'Bridge 返回 SSH 错误', {
                message: msg.message || '',
                detail: msg.detail || '',
              })
              rejectFlow(createFlowError(msg.message || 'SSH 连接出错', msg.detail || ''))
            }
          }

          socket.onerror = (err) => {
            devLog('SSH-CREATE', 'ERROR: WebSocket 错误', err)
            appendSshCreationLog('error', 'bridge', '本地 SSH Bridge WebSocket 连接失败')
            rejectFlow(createFlowError('本地 SSH Bridge 未运行', '浏览器无法连接 ws://localhost:9999'))
          }

          socket.onclose = (event) => {
            devLog('SSH-CREATE', 'WebSocket 关闭', { code: event.code, reason: event.reason, phase: sshCreationPhase.value })
            if (!flowSettled && sshCreationPhase.value !== 'done' && sshCreationPhase.value !== 'error') {
              appendSshCreationLog('warn', 'bridge', '本地 SSH Bridge 连接异常关闭', {
                code: event.code,
                reason: event.reason || '',
              })
              rejectFlow(createFlowError('SSH 连接意外中断'))
            }
          }

        } catch (e: any) {
          const detail = e.response?.data?.detail || e.message
          sshCreationError.value = { message: '创建工单失败', detail }
          sshCreationPhase.value = 'error'
          isLoading.value = false
          appendSshCreationLog('error', 'case', '创建工单接口失败', { detail })
          reject(createFlowError('创建工单失败', detail))
        }
      })().catch(reject)
    })
  }

  /** 提交采集数据到 Environment API */
  async function submitCollectedData(caseId: string, buffer: Record<string, string>) {
    devLog('submitCollectedData', '开始提交采集数据', {
      caseId,
      hasCluster: Boolean(buffer.cluster),
      hasAlert: Boolean(buffer.alert),
      hasTask: Boolean(buffer.task),
    })

    // 解析并提交集群信息
    if (buffer.cluster) {
      try {
        const cleaned = stripAnsi(buffer.cluster)
        devLog('submitCollectedData', 'cluster 原始输出（截断）', { outputPreview: cleaned.substring(0, 100) })
        const clusterData = parseClusterOutput(cleaned)
        devLog('submitCollectedData', 'cluster 解析成功', { keys: Object.keys(clusterData || {}) })
        await environmentApi.upsert(caseId, 'cluster', clusterData)
        devLog('submitCollectedData', 'cluster upsert 成功', { caseId })
      } catch (e) {
        console.error('[submitCollectedData][cluster] 提交失败:', e)
      }
    } else {
      console.warn('[submitCollectedData][cluster] buffer 为空，跳过')
    }

    // 解析并提交告警
    if (buffer.alert) {
      try {
        const cleaned = stripAnsi(buffer.alert)
        devLog('submitCollectedData', 'alert 原始输出（截断）', { outputPreview: cleaned.substring(0, 100) })
        const parsed = parseJsonOutput(cleaned)
        devLog('submitCollectedData', 'alert 解析类型', { type: typeof parsed, isArray: Array.isArray(parsed) })

        // 兼容纯数组和 {entities:[...]} / {alerts:[...]} 包装格式
        let alertList: unknown[] | null = null
        if (Array.isArray(parsed)) {
          alertList = parsed
        } else if (parsed && typeof parsed === 'object') {
          const obj = parsed as Record<string, unknown>
          const candidate = obj['entities'] ?? obj['alerts'] ?? obj['data'] ?? null
          if (Array.isArray(candidate)) alertList = candidate
        }

        if (alertList !== null) {
          await environmentApi.upsert(caseId, 'alert', { alerts: alertList })
          devLog('submitCollectedData', 'alert upsert 成功', { count: alertList.length })
        } else {
          // 即使无法解析为列表，也以原始文本提交，确保数据不丢失
          console.warn('[submitCollectedData][alert] 无法解析为列表，以原始输出提交')
          await environmentApi.upsert(caseId, 'alert', { raw_output: cleaned, parse_error: '无法提取 alert 列表' })
        }
      } catch (e) {
        console.error('[submitCollectedData][alert] 提交失败:', e)
      }
    } else {
      console.warn('[submitCollectedData][alert] buffer 为空，跳过')
    }

    // 解析并提交任务
    if (buffer.task) {
      try {
        const cleaned = stripAnsi(buffer.task)
        devLog('submitCollectedData', 'task 原始输出（截断）', { outputPreview: cleaned.substring(0, 100) })
        const parsed = parseJsonOutput(cleaned)
        devLog('submitCollectedData', 'task 解析类型', { type: typeof parsed, isArray: Array.isArray(parsed) })

        // 兼容纯数组和 {entities:[...]} / {tasks:[...]} 包装格式
        let taskList: unknown[] | null = null
        if (Array.isArray(parsed)) {
          taskList = parsed
        } else if (parsed && typeof parsed === 'object') {
          const obj = parsed as Record<string, unknown>
          const candidate = obj['entities'] ?? obj['tasks'] ?? obj['data'] ?? null
          if (Array.isArray(candidate)) taskList = candidate
        }

        if (taskList !== null) {
          await environmentApi.upsert(caseId, 'task', { tasks: taskList })
          devLog('submitCollectedData', 'task upsert 成功', { count: taskList.length })
        } else {
          console.warn('[submitCollectedData][task] 无法解析为列表，以原始输出 upsert')
          await environmentApi.upsert(caseId, 'task', { raw_output: cleaned, parse_error: '无法提取 task 列表' })
        }
      } catch (e) {
        console.error('[submitCollectedData][task] 提交失败:', e)
      }
    } else {
      console.warn('[submitCollectedData][task] buffer 为空，跳过')
    }

    devLog('submitCollectedData', '所有数据提交完成', { caseId })
  }

  /**
   * 解析 acli platform info get 输出
   *
   * acli platform info get 输出格式为 ini 风格：
   *   [section]
   *   key=value
   *   key=value
   *   ...
   * 同时包含若干自由文本行（版本号行、内核行、历史记录行、Shell 提示符行）
   *
   * Bridge 命令实际包含 DONE marker：
   *   acli platform info get; status=$?; printf '\n__HCI_DONE_...__:%s\n' "$status"
   * 因此输出中还会包含 marker 行和命令回显行。
   */
  function parseClusterOutput(output: string): Record<string, unknown> {
    const cleaned = stripAnsi(output)
    const result: Record<string, unknown> = {}
    const lines = cleaned.split('\n')

    for (const rawLine of lines) {
      const line = rawLine.trim()
      if (!line) continue

      // 1. 跳过 DONE marker 行（__HCI_DONE_xxx__:0 格式）
      if (line.startsWith('__HCI_DONE_')) continue

      // 2. 跳过命令回显行（包含完整命令字符串）
      if (line.includes('printf') || line.includes('__HCI_DONE_') || line.startsWith('acli ')) continue

      // 3. 跳过 Shell 提示符行（Sangfor:xxx # 格式）
      if (/^Sangfor:/.test(line)) continue

      // 4. 跳过历史记录行（update 日志，格式为 "YYYY-MM-DD HH:MM:SS  update | ..."）
      if (/^\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}\s+update/.test(line)) continue

      // 5. 跳过 section 标题行 [xxx]
      if (/^\[.+\]$/.test(line)) continue

      // 6. 解析 key=value 行（acli 的主要格式）
      const eqIdx = line.indexOf('=')
      if (eqIdx > 0) {
        const key = line.slice(0, eqIdx).trim()
        const value = line.slice(eqIdx + 1).trim()
        // key 必须是合法标识符（字母、数字、下划线），排除误匹配
        if (/^[a-zA-Z][a-zA-Z0-9_]*$/.test(key)) {
          result[key] = value
          continue
        }
      }

      // 7. 提取特殊自由文本行：版本号行（如 "6.10.0_R2"）
      if (/^\d+\.\d+\.\d+/.test(line) && !result['hci_version']) {
        result['hci_version'] = line.trim()
        continue
      }

      // 8. 提取内核信息行（Linux xxx ...）
      if (line.startsWith('Linux ') && !result['kernel']) {
        result['kernel'] = line.trim()
        continue
      }

      // 9. 提取 build 时间行（build YYYY-MM-DD ...）
      if (line.startsWith('build ') && !result['build_time']) {
        result['build_time'] = line.trim()
        continue
      }
    }

    devLog('parseClusterOutput', '解析完成', { keys: Object.keys(result) })
    return result
  }

  // stripAnsi / parseJsonOutput 已从 @/api/terminal 引入，避免实现漂移

  /** 取消 SSH 创建流程 */
  function cancelSSHCreation() {
    if (sshCreationPhase.value !== 'idle') {
      appendSshCreationLog('warn', 'flow', '用户取消 SSH 集成创建工单流程', {
        phase: sshCreationPhase.value,
      })
    }
    if (sshCreationSocket.value) {
      sshCreationSocket.value.close()
      sshCreationSocket.value = null
    }
    sshCreationPhase.value = 'idle'
    sshCreationError.value = null
    isLoading.value = false
  }

  /** 无 SSH 创建工单 */
  async function createCaseWithoutSSH(template: CaseTemplate, assistantType?: string) {
    showCaseTemplate.value = false
    addUserMessage(pendingUserMessage.value)
    isLoading.value = true
    try {
      const res = await caseApi.create({
        client_id: clientId,
        title: template.title,
        description: template.description,
        assistant_type: assistantType || selectedAssistant.value || undefined,
      })
      currentCase.value = res.data
      addSystemMessage(`工单 ${res.data.case_id} 已创建，AI 正在识别故障类型，请稍候…`)

      await createConversation()
      await streamAIResponse(pendingUserMessage.value)
    } catch (e: any) {
      addSystemMessage(`创建工单失败: ${e.response?.data?.detail || e.message}`)
    } finally {
      isLoading.value = false
      pendingUserMessage.value = ''
    }
  }

  return {
    messages,
    currentCase,
    conversationId,
    isLoading,
    isStreaming,
    existingCases,
    hasActiveCase,
    isCaseClosed,
    // 诊断阶段
    diagnosticStage,
    showAssistantSelector,
    assistants,
    selectedAssistant,
    pendingCase,
    showPendingDialog,
    resumePendingCase,
    closePendingCase,
    handleCloseCase,
    showCaseTemplate,
    caseTemplate,
    pendingUserMessage,
    caseCreateDialogBridgeStatus,
    sshConnectDialogBridgeStatus,
    confirmCreateCase,
    cancelCreateCase,
    showHistoryDrawer,
    historyMessages,
    historyCase,
    historyLoading,
    openHistoryDrawer,
    closeHistoryDrawer,
    loadHistoryMessages,
    switchToCase,
    showRatingCard,
    ratingConversationId,
    submitRating,
    skipRating,
    closeRatingCard,
    // 终端
    showTerminalSidebar,
    terminalInputCommand,
    assistantDraftText,
    sendCommandToTerminal,
    clearTerminalInput,
    setAssistantDraftText,
    clearAssistantDraftText,
    openTerminalSidebar,
    closeTerminalSidebar,
    checkAndOpenTerminal,
    // 命令自动执行
    autoExecuteMode,
    isExecutingCommand,
    executingCommand,
    autoExecCount,
    pageVisible,
    setAutoExecuteMode,
    executeCommandViaSSH,
    // Bridge 状态
    bridgeStatus,
    showBridgeDownload,
    closeBridgeDownload,
    // 统一 SSH 连接弹框
    sshFlowDialogVisible,
    sshFlowDialogCaseId,
    sshFlowDialogMode,
    sshFlowDialogAssistantType,
    openSshFlowDialog,
    closeSshFlowDialog,
    createCaseAndOpenSsh,
    // 全局 SSH 连接状态和方法
    sshWebSocket,
    sshConnectionState,
    sshCurrentConfig,
    sshErrorMessage,
    sshOutputBuffer,
    sshTerminalOutputEvent,
    sshCommandConsumer,
    connectSSH,
    disconnectSSH,
    forwardBridgeLog,
    importBridgeLogsToCase,
    sendSSHCommand,
    // Agent 模式：高风险操作确认
    pendingConfirm,
    handleConfirmResult,
    // T-E7: ops-agent 交互请求卡片
    pendingInteractive,
    clearInteractiveRequest: () => { pendingInteractive.value = null },
    // T-TOOL-18: 命令执行确认卡片
    pendingExecConfirm,
    clearExecConfirm: () => { pendingExecConfirm.value = null },
    resumeOpsAgentStream,
    initialize,
    sendMessage,
    startNewConversation,
    // 环境数据采集
    collectionState,
    collectionProgress,
    environmentData,
    environmentContext,
    collectEnvironmentData,
    refreshEnvironmentData,
    submitEnvironmentData,
    submitCollectedData,
    completeCaseCreationFlow,
    // SSH 连接状态（创建工单时）
    sshCreationPhase,
    sshCreationError,
    sshCreationFlowId,
    sshCreationLogs,
    acliAvailable,
    connectSSHAndCreateCase,
    createCaseWithoutSSH,
    cancelSSHCreation,
    // SSH 认证超时清理（供 SshFlowPanel onBeforeUnmount 调用）
    clearSshAuthTimer,
  }
})
