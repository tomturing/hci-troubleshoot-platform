/**
 * 聊天 Store - 管理对话状态
 */

import { defineStore } from 'pinia'
import { ref, computed } from 'vue'
import { createApiClient, createCaseApi, createConversationApi } from '@hci/shared'
import type { CaseResponse, MessageResponse } from '@hci/shared'
import { getClientId } from '@/utils/clientId'

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

  // 状态
  const messages = ref<ChatMessage[]>([])
  const currentCase = ref<CaseResponse | null>(null)
  const conversationId = ref<string | null>(null)
  const isLoading = ref(false)
  const isStreaming = ref(false)
  const existingCases = ref<CaseResponse[]>([])
  const initialized = ref(false)

  // 未关闭工单确认流程
  const pendingCase = ref<CaseResponse | null>(null)
  const showPendingDialog = ref(false)

  // 工单创建模板流程
  const showCaseTemplate = ref(false)
  const caseTemplate = ref<CaseTemplate>({ title: '', description: '' })
  const pendingUserMessage = ref('')

  // 计算属性
  const hasActiveCase = computed(() => {
    return currentCase.value && !['closed', 'cancelled'].includes(currentCase.value.status)
  })

  /** 初始化：检查现有工单 */
  async function initialize() {
    if (initialized.value) return
    try {
      const res = await caseApi.listByClient(clientId)
      existingCases.value = res.data
      // 找到最近的未关闭工单
      const activeCase = existingCases.value.find(
        (c) => !['closed', 'cancelled'].includes(c.status),
      )
      if (activeCase) {
        // 发现未关闭工单，弹出确认对话框让用户选择
        pendingCase.value = activeCase
        showPendingDialog.value = true
      } else {
        // 无未关闭工单，显示欢迎消息
        addSystemMessage('您好！我是 HCI 故障排查助手。请描述您遇到的问题，我会帮您创建工单并提供解决方案。')
      }
    } catch (e) {
      console.error('初始化失败', e)
      addSystemMessage('您好！我是 HCI 故障排查助手。请描述您遇到的问题。')
    }
    initialized.value = true
  }

  /** 用户选择继续处理未关闭工单 */
  async function resumePendingCase() {
    if (!pendingCase.value) return
    currentCase.value = pendingCase.value
    showPendingDialog.value = false
    await loadConversationHistory(pendingCase.value.case_id)
    pendingCase.value = null
  }

  /** 用户选择关闭旧工单 */
  async function closePendingCase() {
    if (!pendingCase.value) return
    try {
      await caseApi.close(pendingCase.value.case_id)
      addSystemMessage(`旧工单 ${pendingCase.value.case_id} 已关闭。请描述您遇到的新问题。`)
    } catch (e: any) {
      addSystemMessage(`关闭旧工单失败: ${e.response?.data?.detail || e.message}，但您仍可以创建新工单。`)
    }
    showPendingDialog.value = false
    pendingCase.value = null
  }

  /** 加载已有对话历史 */
  async function loadConversationHistory(caseId: string) {
    try {
      // 获取工单下的对话
      const convRes = await apiClient.get(`/api/conversations/case/${caseId}`)
      const conversations = convRes.data as any[]
      if (conversations.length > 0) {
        const conv = conversations[0]
        conversationId.value = conv.conversation_id
        // 加载消息
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
        addSystemMessage(
          `工单 ${caseId} 已恢复。您可以继续描述问题，或输入 /close 关闭工单。`,
        )
      }
    } catch (e) {
      console.error('加载对话历史失败', e)
      addSystemMessage('对话历史加载失败，但您仍可以继续对话。')
    }
  }

  /** 发送消息 - 核心流程 */
  async function sendMessage(content: string) {
    if (!content.trim() || isStreaming.value) return

    // 处理命令
    if (content.startsWith('/close')) {
      await handleCloseCase()
      return
    }

    // 如果没有活跃工单，显示工单创建模板让用户编辑
    if (!currentCase.value) {
      pendingUserMessage.value = content
      // 生成模板默认值
      const title = content.length > 50 ? content.substring(0, 50) + '...' : content
      caseTemplate.value = { title, description: content }
      showCaseTemplate.value = true
      return
    }

    // 添加用户消息
    addUserMessage(content)

    // 如果没有会话，先创建
    if (!conversationId.value) {
      await createConversation()
    }

    // 发送 AI 消息并流式接收
    await streamAIResponse(content)
  }

  /** 用户确认模板后创建工单 */
  async function confirmCreateCase(template: CaseTemplate) {
    showCaseTemplate.value = false
    addUserMessage(pendingUserMessage.value)
    isLoading.value = true
    try {
      const res = await caseApi.create({
        client_id: clientId,
        title: template.title,
        description: template.description,
      })
      currentCase.value = res.data
      addSystemMessage(`工单 ${res.data.case_id} 已创建，正在自动确认...`)

      // 自动确认
      const confirmed = await caseApi.confirm(res.data.case_id)
      currentCase.value = confirmed.data
      addSystemMessage('工单已确认，正在连接 AI 助手...')

      // 创建对话
      await createConversation()

      // 开始 AI 对话
      await streamAIResponse(pendingUserMessage.value)
    } catch (e: any) {
      addSystemMessage(`创建工单失败: ${e.response?.data?.detail || e.message}`)
    } finally {
      isLoading.value = false
      pendingUserMessage.value = ''
    }
  }

  /** 用户取消创建工单 */
  function cancelCreateCase() {
    showCaseTemplate.value = false
    pendingUserMessage.value = ''
  }

  /** 创建对话 */
  async function createConversation() {
    if (!currentCase.value) return
    try {
      const res = await conversationApi.create(currentCase.value.case_id)
      conversationId.value = res.data.conversation_id
    } catch (e: any) {
      addSystemMessage(`创建对话失败: ${e.response?.data?.detail || e.message}`)
    }
  }

  /** 流式接收 AI 响应 */
  async function streamAIResponse(content: string) {
    if (!conversationId.value || !currentCase.value) return

    isStreaming.value = true

    // 先添加一个空的 AI 消息占位
    const aiMsgId = `ai-${Date.now()}`
    messages.value.push({
      id: aiMsgId,
      role: 'assistant',
      content: '',
      timestamp: new Date(),
      isStreaming: true,
    })

    try {
      // 后端 SSE 是 POST 方式，使用 fetch + ReadableStream
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
        // 按行解析 SSE
        const lines = buffer.split('\n')
        buffer = lines.pop() || ''

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6)
            if (data === '[DONE]') continue
            if (data.startsWith('{') && data.includes('"error"')) continue
            // 追加到 AI 消息
            const aiMsg = messages.value.find((m) => m.id === aiMsgId)
            if (aiMsg) {
              aiMsg.content += data
            }
          }
        }
      }
    } catch (e: any) {
      const aiMsg = messages.value.find((m) => m.id === aiMsgId)
      if (aiMsg && !aiMsg.content) {
        aiMsg.content = `[AI 响应失败: ${e.message}]`
      }
    } finally {
      const aiMsg = messages.value.find((m) => m.id === aiMsgId)
      if (aiMsg) aiMsg.isStreaming = false
      isStreaming.value = false
    }
  }

  /** 关闭当前工单 */
  async function handleCloseCase() {
    if (!currentCase.value) {
      addSystemMessage('当前没有活跃的工单。')
      return
    }
    try {
      const res = await caseApi.close(currentCase.value.case_id)
      currentCase.value = res.data
      addSystemMessage(`工单 ${res.data.case_id} 已关闭。发送新消息开启新工单。`)
      conversationId.value = null
    } catch (e: any) {
      addSystemMessage(`关闭工单失败: ${e.response?.data?.detail || e.message}`)
    }
  }

  /** 开始新对话（关闭当前工单后） */
  function startNewConversation() {
    currentCase.value = null
    conversationId.value = null
    messages.value = []
    addSystemMessage('请描述您遇到的新问题，我会帮您创建工单。')
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

  return {
    messages,
    currentCase,
    conversationId,
    isLoading,
    isStreaming,
    existingCases,
    hasActiveCase,
    // 未关闭工单确认
    pendingCase,
    showPendingDialog,
    resumePendingCase,
    closePendingCase,
    // 工单创建模板
    showCaseTemplate,
    caseTemplate,
    confirmCreateCase,
    cancelCreateCase,
    // 核心方法
    initialize,
    sendMessage,
    startNewConversation,
  }
})
