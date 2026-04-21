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
  // SSH WebSocket 连接（全局唯一）
  const sshWebSocket = ref<WebSocket | null>(null)
  // SSH 连接状态（全局共享）
  const sshConnectionState = ref<'disconnected' | 'connecting' | 'connected' | 'error'>('disconnected')
  // SSH 连接配置
  const sshCurrentConfig = ref<{
    host: string
    port: number
    username: string
    authType: 'password' | 'key'
    caseId?: string
  } | null>(null)
  // SSH 错误信息
  const sshErrorMessage = ref('')
  // SSH 输出缓冲（用于终端展示）
  const sshOutputBuffer = ref<string>('')
  // 命令消费者标识（区分用户命令和采集命令）
  const sshCommandConsumer = ref<'terminal' | 'collection' | null>(null)
  // 终端输出事件（通知 TerminalPanel）
  const sshTerminalOutputEvent = ref<string>('')

  // === SSH 连接状态（创建工单流程） ===
  // 注意：状态名称使用 acli_* 而非 acll_* 避免混淆
  const sshCreationPhase = ref<'idle' | 'connecting' | 'connected' | 'acli_check' | 'collecting' | 'done' | 'error' | 'acli_not_found'>('idle')
  const sshCreationError = ref<{ message: string; detail: string } | null>(null)
  const acliAvailable = ref<boolean | null>(null)

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

  /** 全局 SSH 连接方法
   * @param config SSH 配置
   * @returns Promise，连接成功 resolve，失败 reject
   */
  function connectSSH(config: {
    host: string
    port: number
    username: string
    authType: 'password' | 'key'
    password?: string
    privateKey?: string
    passphrase?: string
    caseId?: string
  }): Promise<void> {
    console.log('[SSH] 开始连接', {
      host: config.host,
      port: config.port,
      username: config.username,
      caseId: config.caseId,
      timestamp: new Date().toISOString()
    })

    return new Promise((resolve, reject) => {
      // 清理旧连接
      cleanupSshWebSocket()

      // 更新状态
      sshConnectionState.value = 'connecting'
      sshErrorMessage.value = ''
      sshCurrentConfig.value = {
        host: config.host,
        port: config.port,
        username: config.username,
        authType: config.authType,
        caseId: config.caseId
      }
      sshOutputBuffer.value = ''

      // 创建 WebSocket
      const socket = createBridgeSocket()
      sshWebSocket.value = socket

      socket.onopen = () => {
        console.log('[SSH] WebSocket 已连接到 Bridge', { timestamp: new Date().toISOString() })

        // 设置认证超时
        sshAuthTimer = window.setTimeout(() => {
          if (sshConnectionState.value === 'connecting') {
            console.error('[SSH] 认证超时', { timestamp: new Date().toISOString() })
            sshErrorMessage.value = 'SSH 认证超时：15秒内未收到认证成功信号'
            sshConnectionState.value = 'error'
            cleanupSshWebSocket()
            reject(new Error('SSH 认证超时'))
          }
        }, SSH_AUTH_TIMEOUT)

        // 发送 SSH 连接请求
        const connectMsg = buildConnectMessage({
          host: config.host,
          port: config.port,
          username: config.username,
          auth_type: config.authType,
          password: config.password,
          private_key: config.privateKey,
          passphrase: config.passphrase,
          case_id: config.caseId
        })
        console.log('[SSH] 发送连接请求', { timestamp: new Date().toISOString() })
        socket.send(connectMsg)
      }

      socket.onmessage = (e) => {
        let msg: TerminalWsMessage
        try {
          msg = JSON.parse(String(e.data || ''))
        } catch {
          console.warn('[SSH] 无法解析消息', { raw: e.data })
          return
        }

        console.log('[SSH] 收到消息', { type: msg.type, timestamp: new Date().toISOString() })

        // 过滤 case_id（如果有）
        if (msg.case_id && config.caseId && msg.case_id !== config.caseId) {
          console.log('[SSH] 消息 case_id 不匹配，忽略', { msgCaseId: msg.case_id, configCaseId: config.caseId })
          return
        }

        if (msg.type === 'ssh_connected') {
          console.log('[SSH] 连接成功', { timestamp: new Date().toISOString() })
          clearSshAuthTimer()
          sshConnectionState.value = 'connected'
          sshErrorMessage.value = ''

          // 触发智能采集（如果需要）
          onSshConnected()

          resolve()
        }

        else if (msg.type === 'ssh_output' && msg.output) {
          // 根据消费者分发输出
          if (sshCommandConsumer.value === 'terminal') {
            sshOutputBuffer.value += msg.output
            sshTerminalOutputEvent.value = msg.output  // 触发 TerminalPanel 更新
          } else if (sshCommandConsumer.value === 'collection') {
            // 采集输出由 connectSSHAndCreateCase 内部处理
          } else {
            // 默认添加到缓冲
            sshOutputBuffer.value += msg.output
          }
        }

        else if (msg.type === 'ssh_error') {
          console.error('[SSH] 收到错误', { message: msg.message, detail: msg.detail, timestamp: new Date().toISOString() })
          clearSshAuthTimer()
          sshErrorMessage.value = msg.message || 'SSH 连接出错'
          sshConnectionState.value = 'error'
          cleanupSshWebSocket()
          reject(new Error(msg.message || 'SSH 连接出错'))
        }

        else if (msg.type === 'ssh_disconnected') {
          console.log('[SSH] 收到断开消息', { timestamp: new Date().toISOString() })
          clearSshAuthTimer()
          sshConnectionState.value = 'disconnected'
          cleanupSshWebSocket()
        }
      }

      socket.onerror = (e) => {
        console.error('[SSH] WebSocket 错误', { timestamp: new Date().toISOString() })
        clearSshAuthTimer()
        sshErrorMessage.value = '本地 SSH Bridge 未运行：浏览器无法连接 ws://localhost:9999'
        sshConnectionState.value = 'error'
        cleanupSshWebSocket()
        reject(new Error('本地 SSH Bridge 未运行'))
      }

      socket.onclose = (e) => {
        console.log('[SSH] WebSocket 关闭', { code: e.code, reason: e.reason, timestamp: new Date().toISOString() })
        clearSshAuthTimer()
        // 如果还在 connecting 状态，说明异常关闭
        if (sshConnectionState.value === 'connecting') {
          sshErrorMessage.value = 'SSH 连接意外中断'
          sshConnectionState.value = 'error'
          reject(new Error('SSH 连接意外中断'))
        }
      }
    })
  }

  /** 断开 SSH 连接 */
  function disconnectSSH() {
    console.log('[SSH] 断开连接', { timestamp: new Date().toISOString() })
    if (sshWebSocket.value?.readyState === WebSocket.OPEN && sshCurrentConfig.value?.caseId) {
      sshWebSocket.value.send(buildDisconnectMessage(sshCurrentConfig.value.caseId))
    }
    cleanupSshWebSocket()
    sshConnectionState.value = 'disconnected'
    sshCurrentConfig.value = null
    sshOutputBuffer.value = ''
    sshErrorMessage.value = ''
  }

  /** 发送命令到 SSH 会话
   * @param cmd 命令内容
   * @param consumer 消费者标识（'terminal' 或 'collection'）
   */
  function sendSSHCommand(cmd: string, consumer: 'terminal' | 'collection' = 'terminal') {
    if (!sshWebSocket.value || sshWebSocket.value.readyState !== WebSocket.OPEN) {
      console.warn('[SSH] 无法发送命令：连接未建立')
      return
    }
    const caseId = sshCurrentConfig.value?.caseId || ''
    console.log('[SSH] 发送命令', { cmd: cmd.slice(0, 50), consumer, timestamp: new Date().toISOString() })
    sshCommandConsumer.value = consumer
    sshWebSocket.value.send(buildInputMessage(caseId, cmd.endsWith('\n') ? cmd : `${cmd}\n`))
  }

  /** SSH 连接成功后回调：智能采集环境数据 */
  function onSshConnected() {
    console.log('[SSH] onSshConnected 回调', { timestamp: new Date().toISOString() })

    // 检查环境数据状态
    // 如果没有环境数据，触发采集
    const hasEnvironmentData = environmentData.value.length > 0 || environmentContext.value !== null

    if (!hasEnvironmentData) {
      console.log('[SSH] 无环境数据，触发采集')
      // 自动采集（仅在有当前工单时）
      if (currentCase.value) {
        refreshEnvironmentData()
      }
    } else {
      console.log('[SSH] 已有环境数据，不触发自动采集')
    }
  }

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
    console.log('[SSH创建工单] 开始流程', {
      title,
      host: sshConfig.host,
      port: sshConfig.port,
      username: sshConfig.username,
      timestamp: new Date().toISOString()
    })

    return new Promise(async (resolve, reject) => {
      sshCreationPhase.value = 'connecting'
      sshCreationError.value = null
      acliAvailable.value = null
      isLoading.value = true

      try {
        // 1. 创建工单
        console.log('[SSH创建工单] 创建工单...')
        const res = await caseApi.create({
          client_id: clientId,
          title,
          description,
          assistant_type: assistantType || selectedAssistant.value || undefined,
        })
        currentCase.value = res.data
        const caseId = res.data.case_id
        console.log('[SSH创建工单] 工单创建成功', { caseId })

        // 2. 确认工单
        console.log('[SSH创建工单] 确认工单...')
        const confirmed = await caseApi.confirm(caseId)
        currentCase.value = confirmed.data

        // 3. 使用全局 SSH 连接方法
        console.log('[SSH创建工单] 建立 SSH 连接...')
        try {
          await connectSSH({
            host: sshConfig.host,
            port: sshConfig.port,
            username: sshConfig.username,
            authType: 'password',
            password: sshConfig.password,
            caseId: caseId
          })
          console.log('[SSH创建工单] SSH 连接成功')
        } catch (sshError: any) {
          console.error('[SSH创建工单] SSH 连接失败', { error: sshError.message })
          sshCreationError.value = { message: sshError.message, detail: '' }
          sshCreationPhase.value = 'error'
          isLoading.value = false
          reject(sshError)
          return
        }

        // 连接成功，设置采集消费者
        sshCommandConsumer.value = 'collection'
        sshCreationPhase.value = 'acli_check'

        // 采集输出缓冲
        const collectBuffer: Record<string, string> = { cluster: '', alert: '', task: '' }
        let currentCollectIndex = 0
        let collectingOutput = ''

        // 注册采集消息处理器（监听 WebSocket 消息）
        const collectHandler = (e: MessageEvent) => {
          let msg: TerminalWsMessage
          try {
            msg = JSON.parse(String(e.data || ''))
          } catch {
            return
          }

          console.log('[SSH创建工单] 收到消息', { type: msg.type })

          if (msg.case_id && msg.case_id !== caseId) return

          if (msg.type === 'ssh_output' && msg.output) {
            const output = msg.output

            // 阶段：acli 检查
            if (sshCreationPhase.value === 'acli_check') {
              if (output.includes('command not found') || output.includes('not recognized')) {
                console.log('[SSH创建工单] acli 未找到')
                acliAvailable.value = false
                sshCreationPhase.value = 'acli_not_found'
                // 移除处理器
                removeCollectHandler()
                // 继续完成工单流程（无环境数据）
                completeCaseCreationFlow(caseId, userMessage || description, assistantType)
                  .then(() => {
                    sshCreationPhase.value = 'done'
                    resolve()
                  })
                  .catch((err) => reject(err))
              } else if (output.includes('Usage') || output.includes('HCI')) {
                console.log('[SSH创建工单] acli 可用，开始采集')
                acliAvailable.value = true
                sshCreationPhase.value = 'collecting'
                currentCollectIndex = 0
                collectingOutput = ''

                // 开始执行第一个采集命令
                const cmd = COLLECT_COMMANDS[0].cmd
                sendSSHCommand(cmd, 'collection')
              }
            }

            // 阶段：采集数据
            else if (sshCreationPhase.value === 'collecting') {
              collectingOutput += output

              // 检测命令结束（通过输出特征判断）
              if (collectingOutput.includes('}') || collectingOutput.includes('\n\n') || output.includes('$')) {
                // 保存当前采集结果
                const currentCmd = COLLECT_COMMANDS[currentCollectIndex]
                collectBuffer[currentCmd.name] = collectingOutput.trim()
                console.log('[SSH创建工单] 采集完成', { name: currentCmd.name })
                collectingOutput = ''

                // 下一个采集命令
                currentCollectIndex++
                if (currentCollectIndex < COLLECT_COMMANDS.length) {
                  const nextCmd = COLLECT_COMMANDS[currentCollectIndex].cmd
                  sendSSHCommand(nextCmd, 'collection')
                } else {
                  // 所有采集完成，提交数据
                  console.log('[SSH创建工单] 所有采集完成，提交数据')
                  removeCollectHandler()
                  submitCollectedData(caseId, collectBuffer)
                    .then(async () => {
                      // 刷新环境数据
                      await collectEnvironmentData(caseId)

                      // 完成工单流程
                      await completeCaseCreationFlow(caseId, userMessage || description, assistantType)

                      sshCreationPhase.value = 'done'
                      sshCommandConsumer.value = null
                      resolve()
                    })
                    .catch((err) => {
                      // 提交失败仍继续流程
                      console.warn('[SSH创建工单] 提交失败，继续流程:', err)
                      completeCaseCreationFlow(caseId, userMessage || description, assistantType)
                        .then(() => {
                          sshCreationPhase.value = 'done'
                          sshCommandConsumer.value = null
                          resolve()
                        })
                        .catch((e) => reject(e))
                    })
                }
              }
            }
          }

          else if (msg.type === 'ssh_error') {
            console.error('[SSH创建工单] 收到 ssh_error', { message: msg.message })
            removeCollectHandler()
            sshCreationError.value = { message: msg.message || 'SSH 连接出错', detail: msg.detail || '' }
            sshCreationPhase.value = 'error'
            isLoading.value = false
            reject(new Error(msg.message || 'SSH 连接出错'))
          }

          else if (msg.type === 'ssh_disconnected') {
            console.log('[SSH创建工单] SSH 断开')
            removeCollectHandler()
            if (sshCreationPhase.value !== 'done') {
              sshCreationError.value = { message: 'SSH 连接意外中断', detail: '' }
              sshCreationPhase.value = 'error'
              isLoading.value = false
              reject(new Error('SSH 连接意外中断'))
            }
          }
        }

        // 注册处理器
        const removeCollectHandler = () => {
          if (sshWebSocket.value) {
            sshWebSocket.value.removeEventListener('message', collectHandler)
          }
        }

        // 添加处理器到 WebSocket
        if (sshWebSocket.value) {
          sshWebSocket.value.addEventListener('message', collectHandler)
        }

        // 发送第一个命令检查 acli
        sendSSHCommand('acli', 'collection')

      } catch (e: any) {
        console.error('[SSH创建工单] 流程异常', { error: e.message })
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
    disconnectSSH()
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
