/**
 * 聊天 Store - 管理对话状态
 */

import { defineStore } from 'pinia'
import { ref, computed, nextTick } from 'vue'
import { createApiClient, createCaseApi, createConversationApi, createAssistantApi, createEnvironmentApi } from '@hci/shared'
import type { CaseResponse, MessageResponse, AssistantInfo, AssistantsResponse, EnvironmentResponse, EnvironmentContextResponse, EnvType } from '@hci/shared'
import { getClientId } from '@/utils/clientId'
import { createEvaluateApi } from '@/api/evaluate'
import { checkBridgeRunning, createBridgeSocket, buildConnectMessage, buildInputMessage, buildDisconnectMessage, type BridgeStatus, type TerminalWsMessage } from '@/api/terminal'

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
}

/** 工单创建模板 */
export interface CaseTemplate {
  title: string
  description: string
}

export const useChatStore = defineStore('chat', () => {
  const clientId = getClientId()
  const apiClient = createApiClient('/api', clientId)
  const caseApi = createCaseApi(apiClient)
  const conversationApi = createConversationApi(apiClient)
  const assistantApi = createAssistantApi(apiClient)
  const evaluateApi = createEvaluateApi(apiClient)
  const environmentApi = createEnvironmentApi(apiClient)

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

  // Agent 模式：待确认的高风险操作（confirm_request SSE 事件）
  const pendingConfirm = ref<{
    tool_name: string
    tool_args: Record<string, unknown>
    risk_level: 2 | 3
    risk_description: string
    timeout_seconds: number
  } | null>(null)

  // Bridge 运行状态
  const bridgeStatus = ref<BridgeStatus>('not_running')

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
      } else {
        addSystemMessage('您好！我是 HCI 故障排查助手。请描述您遇到的问题，我会帮您创建工单并提供解决方案。')
      }
    } catch (e) {
      console.error('初始化失败', e)
      addSystemMessage('您好！我是 HCI 故障排查助手。请描述您遇到的问题。')
    }
    initialized.value = true
  }

  async function resumePendingCase() {
    if (!pendingCase.value) return
    currentCase.value = pendingCase.value
    showPendingDialog.value = false
    await loadConversationHistory(pendingCase.value.case_id)
    pendingCase.value = null
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
        const msgRes = await conversationApi.getMessages(conv.conversation_id)
        const history: MessageResponse[] = msgRes.data
        messages.value = history.map((m) => ({
          id: m.message_id,
          role: m.role as ChatMessage['role'],
          content: m.content,
          timestamp: new Date(m.created_at),
        }))
      }
      if (messages.value.length === 0) {
        addSystemMessage(`工单 ${caseId} 已恢复。您可以继续描述问题，或输入 /close 关闭工单。`)
      }
    } catch (e) {
      console.error('加载对话历史失败', e)
      addSystemMessage('对话历史加载失败，但您仍可以继续对话。')
    }
  }

  async function sendMessage(content: string) {
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
      showCaseTemplate.value = true
      return
    }

    addUserMessage(content)

    if (!conversationId.value) {
      await createConversation()
    }

    await streamAIResponse(content)
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
      addSystemMessage(`工单 ${res.data.case_id} 已创建，正在自动确认...`)

      const confirmed = await caseApi.confirm(res.data.case_id)
      currentCase.value = confirmed.data
      addSystemMessage('工单已确认，正在连接 AI 助手...')

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
    if (!currentCase.value) return
    try {
      const assistantType = currentCase.value.assistant_type || selectedAssistant.value || undefined
      const res = await conversationApi.create(currentCase.value.case_id, assistantType)
      conversationId.value = res.data.conversation_id
    } catch (e: any) {
      addSystemMessage(`创建对话失败: ${e.response?.data?.detail || e.message}`)
    }
  }

  async function streamAIResponse(content: string) {
    if (!conversationId.value || !currentCase.value) return

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
          assistant_type: selectedAssistant.value,  // v2.2: 动态切换助手
        }),
      })

      if (!response.ok || !response.body) {
        throw new Error(`HTTP ${response.status}`)
      }

      const reader = response.body.getReader()
      const decoder = new TextDecoder()
      let buffer = ''

      while (true) {
        const { done, value } = await reader.read()
        if (done) break

        buffer += decoder.decode(value, { stream: true })
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        let pendingEventType = 'message'
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
                console.log('[stage_change] 诊断阶段切换:', event.from, '→', event.to, event.label)
              } catch (e) {
                console.warn('[stage_change] 解析失败:', e)
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
      // 重置诊断阶段
      diagnosticStage.value = 'S0'
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
      await loadConversationHistory(caseItem.case_id)
    }
  }

  function addUserMessage(content: string) {
    messages.value.push({
      id: `user-${Date.now()}`,
      role: 'user',
      content,
      timestamp: new Date(),
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

  /** 发送命令到终端（CommandBlock 调用） */
  function sendCommandToTerminal(command: string) {
    terminalInputCommand.value = command
    showTerminalSidebar.value = true
  }

  function clearTerminalInput() {
    terminalInputCommand.value = ''
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
    try {
      await fetch(`/api/conversations/${conversationId.value}/confirm`, {
        method: 'POST',
        headers: {
          'Content-Type': 'application/json',
          'X-Client-ID': clientId,
        },
        body: JSON.stringify({
          confirmed: authorized,
          authorized_by: clientId,
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

  // === 全局 SSH 连接方法 ===

  /** 认证超时定时器 */
  let sshAuthTimer: number | null = null
  const SSH_AUTH_TIMEOUT = 15000

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
      const socket = createBridgeSocket()
      sshWebSocket.value = socket

      socket.onopen = () => {
        devLog('SSH', 'WebSocket 已打开，发送连接命令')
        clearSshAuthTimer()

        // 15 秒认证超时
        sshAuthTimer = window.setTimeout(() => {
          if (sshConnectionState.value === 'connecting') {
            devLog('SSH', 'ERROR: 认证超时')
            sshErrorMessage.value = 'SSH 认证超时（15秒）'
            sshConnectionState.value = 'error'
            cleanupSshWebSocket()
            reject(new Error('SSH 认证超时'))
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
          devLog('SSH', '远程会话已建立')
          clearSshAuthTimer()
          sshConnectionState.value = 'connected'
          resolve()
        } else if (msg.type === 'ssh_output' && msg.output) {
          sshOutputBuffer.value += msg.output
          // 如果有消费者，触发输出事件
          if (sshCommandConsumer.value) {
            sshTerminalOutputEvent.value = msg.output
          }
        } else if (msg.type === 'ssh_error') {
          devLog('SSH', 'ERROR: 收到错误', { message: msg.message })
          clearSshAuthTimer()
          sshErrorMessage.value = msg.message || 'SSH 连接出错'
          sshConnectionState.value = 'error'
          cleanupSshWebSocket()
          reject(new Error(msg.message || 'SSH 连接出错'))
        } else if (msg.type === 'ssh_disconnected') {
          devLog('SSH', '连接断开')
          clearSshAuthTimer()
          if (sshConnectionState.value === 'connected') {
            sshConnectionState.value = 'disconnected'
            cleanupSshWebSocket()
          }
        }
      }

      socket.onerror = () => {
        devLog('SSH', 'ERROR: WebSocket 错误')
        clearSshAuthTimer()
        sshErrorMessage.value = 'SSH Bridge 未运行（ws://localhost:9999）'
        sshConnectionState.value = 'error'
        cleanupSshWebSocket()
        reject(new Error('SSH Bridge 未运行'))
      }

      socket.onclose = () => {
        devLog('SSH', 'WebSocket 关闭')
        clearSshAuthTimer()
        if (sshConnectionState.value !== 'error') {
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

      console.log('[collectEnvironmentData] 采集完成', environmentData.value.length, '条数据')
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

  /** SSH 配置信息 */
  interface SSHConfig {
    host: string
    port: number
    username: string
    password: string
  }

  /** 采集命令列表 */
  const COLLECT_COMMANDS = [
    { name: 'cluster', cmd: 'acli platform info get' },
    { name: 'alert', cmd: 'acli --formatter json alert list' },
    { name: 'task', cmd: 'acli --formatter json task list' },
  ]

  /**
   * 共享流程：创建工单 + 确认 + 建对话 + 首条消息发送
   * 被 confirmCreateCase 和 SSH 流程共用
   */
  async function completeCaseCreationFlow(caseId: string, userMessage: string, assistantType?: string) {
    showCaseTemplate.value = false
    addUserMessage(userMessage)

    addSystemMessage(`工单 ${caseId} 已创建，正在确认...`)

    // 确认工单（若未确认）
    if (currentCase.value?.status === 'created') {
      const confirmed = await caseApi.confirm(caseId)
      currentCase.value = confirmed.data
    }

    addSystemMessage('工单已确认，正在连接 AI 助手...')

    // 创建对话
    await createConversation()

    // 发送首条消息
    await streamAIResponse(userMessage)

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
    return new Promise(async (resolve, reject) => {
      sshCreationPhase.value = 'connecting'
      sshCreationError.value = null
      acliAvailable.value = null
      isLoading.value = true

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

        // 2. 确认工单
        const confirmed = await caseApi.confirm(caseId)
        currentCase.value = confirmed.data

        // 3. 建立 SSH 连接
        devLog('SSH-CREATE', '开始建立 WebSocket 连接到 Bridge')
        const socket = createBridgeSocket()
        devLog('SSH-CREATE', 'WebSocket 对象已创建，等待连接')
        sshCreationSocket.value = socket

        // 采集输出缓冲
        const collectBuffer: Record<string, string> = { cluster: '', alert: '', task: '' }
        let currentCollectIndex = 0
        let collectingOutput = ''

        // 连接超时定时器
        let authTimer: number | null = null
        const clearAuthTimer = () => {
          if (authTimer !== null) {
            window.clearTimeout(authTimer)
            authTimer = null
          }
        }

        const cleanupSocket = () => {
          clearAuthTimer()
          if (sshCreationSocket.value) {
            sshCreationSocket.value.onopen = null
            sshCreationSocket.value.onmessage = null
            sshCreationSocket.value.onerror = null
            sshCreationSocket.value.onclose = null
            sshCreationSocket.value.close()
            sshCreationSocket.value = null
          }
        }

        socket.onopen = () => {
          devLog('SSH-CREATE', 'WebSocket 连接已打开')
          sshCreationPhase.value = 'connected'
          clearAuthTimer()

          // 15 秒认证超时
          authTimer = window.setTimeout(() => {
            devLog('SSH-CREATE', '认证超时检查', { phase: sshCreationPhase.value })
            if (sshCreationPhase.value === 'connected' || sshCreationPhase.value === 'acli_check') {
              sshCreationError.value = { message: 'SSH 认证超时', detail: '15秒内未收到认证成功信号' }
              sshCreationPhase.value = 'error'
              cleanupSocket()
              reject(new Error('SSH 认证超时'))
            }
          }, 15000)

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
            sshConnectionState.value = 'connected'
            sshCreationPhase.value = 'acli_check'

            // 检查 acli 工具
            devLog('SSH-CREATE', '发送 acli 检查命令')
            socket.send(buildInputMessage(caseId, 'acli\n'))

          } else if (msg.type === 'ssh_output' && msg.output) {
            const output = msg.output
            devLog('SSH-CREATE', 'ssh_output', { phase: sshCreationPhase.value, outputPreview: output.substring(0, 50) })

            // 阶段：acli 检查
            if (sshCreationPhase.value === 'acli_check') {
              devLog('SSH-CREATE', 'acli 检查阶段')
              if (output.includes('command not found') || output.includes('not recognized')) {
                devLog('SSH-CREATE', 'acli 未找到')
                acliAvailable.value = false
                sshCreationPhase.value = 'acli_not_found'
                // 继续完成工单流程（无环境数据）
                completeCaseCreationFlow(caseId, userMessage || description, assistantType)
                  .then(() => {
                    sshCreationPhase.value = 'done'
                    cleanupSocket()
                    resolve()
                  })
                  .catch((err) => reject(err))
              } else if (output.includes('Usage') || output.includes('HCI')) {
                // acli 可用
                devLog('SSH-CREATE', 'acli 可用，开始采集')
                acliAvailable.value = true
                sshCreationPhase.value = 'collecting'
                currentCollectIndex = 0
                collectingOutput = ''

                // 开始执行第一个采集命令
                const cmd = COLLECT_COMMANDS[0].cmd
                devLog('SSH-CREATE', '发送采集命令:', cmd)
                socket.send(buildInputMessage(caseId, `${cmd}\n`))
              }
            }

            // 阶段：采集数据
            else if (sshCreationPhase.value === 'collecting') {
              collectingOutput += output
              devLog('SSH-CREATE', '采集阶段', { index: currentCollectIndex, bufferLength: collectingOutput.length })

              // 检测命令结束（通过输出特征判断）
              if (collectingOutput.includes('}') || collectingOutput.includes('\n\n') || output.includes('$')) {
                // 保存当前采集结果
                const currentCmd = COLLECT_COMMANDS[currentCollectIndex]
                devLog('SSH-CREATE', '采集完成', { name: currentCmd.name })
                collectBuffer[currentCmd.name] = collectingOutput.trim()
                collectingOutput = ''

                // 下一个采集命令
                currentCollectIndex++
                if (currentCollectIndex < COLLECT_COMMANDS.length) {
                  const nextCmd = COLLECT_COMMANDS[currentCollectIndex].cmd
                  devLog('SSH-CREATE', '发送下一个采集命令:', nextCmd)
                  socket.send(buildInputMessage(caseId, `${nextCmd}\n`))
                } else {
                  // 所有采集完成，提交数据
                  devLog('SSH-CREATE', '所有采集完成，提交数据')
                  submitCollectedData(caseId, collectBuffer)
                    .then(async () => {
                      // 刷新环境数据
                      await collectEnvironmentData(caseId)

                      // 完成工单流程
                      await completeCaseCreationFlow(caseId, userMessage || description, assistantType)

                      sshCreationPhase.value = 'done'
                      cleanupSocket()
                      resolve()
                    })
                    .catch((err) => {
                      // 提交失败仍继续流程
                      console.warn('[collectEnvironment] 提交失败，继续流程:', err)
                      completeCaseCreationFlow(caseId, userMessage || description, assistantType)
                        .then(() => {
                          sshCreationPhase.value = 'done'
                          cleanupSocket()
                          resolve()
                        })
                        .catch((e) => reject(e))
                    })
                }
              }
            }

          } else if (msg.type === 'ssh_error') {
            clearAuthTimer()
            sshCreationError.value = { message: msg.message || 'SSH 连接出错', detail: msg.detail || '' }
            sshCreationPhase.value = 'error'
            isLoading.value = false
            cleanupSocket()
            reject(new Error(msg.message || 'SSH 连接出错'))
          }
        }

        socket.onerror = (err) => {
          devLog('SSH-CREATE', 'ERROR: WebSocket 错误', err)
          clearAuthTimer()
          sshCreationError.value = { message: '本地 SSH Bridge 未运行', detail: '浏览器无法连接 ws://localhost:9999' }
          sshCreationPhase.value = 'error'
          isLoading.value = false
          cleanupSocket()
          reject(new Error('本地 SSH Bridge 未运行'))
        }

        socket.onclose = (event) => {
          devLog('SSH-CREATE', 'WebSocket 关闭', { code: event.code, reason: event.reason, phase: sshCreationPhase.value })
          clearAuthTimer()
          // 如果还没完成，说明异常关闭
          if (sshCreationPhase.value !== 'done' && sshCreationPhase.value !== 'error') {
            sshCreationError.value = { message: 'SSH 连接意外中断', detail: '' }
            sshCreationPhase.value = 'error'
            isLoading.value = false
            reject(new Error('SSH 连接意外中断'))
          }
        }

      } catch (e: any) {
        sshCreationError.value = { message: '创建工单失败', detail: e.response?.data?.detail || e.message }
        sshCreationPhase.value = 'error'
        isLoading.value = false
        reject(e)
      }
    })
  }

  /** 提交采集数据到 Environment API */
  async function submitCollectedData(caseId: string, buffer: Record<string, string>) {
    // 解析并提交集群信息
    if (buffer.cluster) {
      try {
        const clusterData = parseClusterOutput(buffer.cluster)
        await environmentApi.create({
          case_id: caseId,
          env_type: 'cluster',
          env_data: clusterData,
        })
      } catch (e) {
        console.warn('[submitCollectedData] cluster 提交失败:', e)
      }
    }

    // 解析并提交告警
    if (buffer.alert) {
      try {
        const alertData = parseJsonOutput(buffer.alert)
        if (alertData && Array.isArray(alertData)) {
          await environmentApi.create({
            case_id: caseId,
            env_type: 'alert',
            env_data: { alerts: alertData },
          })
        }
      } catch (e) {
        console.warn('[submitCollectedData] alert 提交失败:', e)
      }
    }

    // 解析并提交任务
    if (buffer.task) {
      try {
        const taskData = parseJsonOutput(buffer.task)
        if (taskData && Array.isArray(taskData)) {
          await environmentApi.create({
            case_id: caseId,
            env_type: 'task',
            env_data: { tasks: taskData },
          })
        }
      } catch (e) {
        console.warn('[submitCollectedData] task 提交失败:', e)
      }
    }
  }

  /** 解析 acli platform info get 输出 */
  function parseClusterOutput(output: string): Record<string, unknown> {
    const result: Record<string, unknown> = {}
    const lines = output.split('\n')
    for (const line of lines) {
      if (line.includes(':')) {
        const [key, value] = line.split(':').map(s => s.trim())
        if (key && value) {
          result[key.toLowerCase().replace(/\s+/g, '_')] = value
        }
      }
    }
    return result
  }

  /** 解析 JSON 输出（acli --formatter json）*/
  function parseJsonOutput(output: string): unknown {
    // 尝试从输出中提取 JSON 部分
    const jsonMatch = output.match(/\{[\s\S]*\}/) || output.match(/\[[\s\S]*\]/)
    if (jsonMatch) {
      try {
        return JSON.parse(jsonMatch[0])
      } catch {
        return null
      }
    }
    return null
  }

  /** 取消 SSH 创建流程 */
  function cancelSSHCreation() {
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
      addSystemMessage(`工单 ${res.data.case_id} 已创建（无 SSH 连接）`)

      const confirmed = await caseApi.confirm(res.data.case_id)
      currentCase.value = confirmed.data
      addSystemMessage('工单已确认，正在连接 AI 助手...')

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
    // Bridge 状态
    bridgeStatus,
    showBridgeDownload,
    closeBridgeDownload,
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
    sendSSHCommand,
    // Agent 模式：高风险操作确认
    pendingConfirm,
    handleConfirmResult,
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
    // SSH 连接状态（创建工单时）
    sshCreationPhase,
    sshCreationError,
    acliAvailable,
    connectSSHAndCreateCase,
    createCaseWithoutSSH,
    cancelSSHCreation,
  }
})
