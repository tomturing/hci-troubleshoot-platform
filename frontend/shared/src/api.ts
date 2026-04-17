/**
 * API 客户端 - Axios 封装
 * 自动注入 X-Client-ID 和 traceparent 头
 */

import axios, { type AxiosInstance, type AxiosRequestConfig } from 'axios'
import type {
  CaseCreate,
  CloseReason,
  CaseResponse,
  CaseListResponse,
  CaseStatsResponse,
  ClientListResponse,
  ConversationResponse,
  MessageResponse,
  AssistantInfo,
  AssistantsResponse,
} from './types'

/** 创建带通用拦截器的 Axios 实例 */
export function createApiClient(baseURL: string, clientId?: string): AxiosInstance {
  const client = axios.create({
    baseURL,
    timeout: 30000,
    headers: { 'Content-Type': 'application/json' },
  })

  // 请求拦截：注入 X-Client-ID
  client.interceptors.request.use((config) => {
    if (clientId) {
      config.headers['X-Client-ID'] = clientId
    }
    return config
  })

  // 响应拦截：统一错误处理
  client.interceptors.response.use(
    (res) => res,
    (error) => {
      console.error('[API Error]', error.response?.status, error.response?.data)
      return Promise.reject(error)
    },
  )

  return client
}

/** Case API 方法集合 */
export function createCaseApi(client: AxiosInstance) {
  return {
    /** 创建工单 */
    create(data: CaseCreate) {
      return client.post<CaseResponse>('/cases/', data)
    },

    /** 获取工单详情 */
    getById(caseId: string) {
      return client.get<CaseResponse>(`/cases/${caseId}`)
    },

    /** 查询客户端的工单列表 */
    listByClient(clientId: string) {
      return client.get<CaseResponse[]>('/cases/', { params: { client_id: clientId } })
    },

    /** 确认工单 */
    confirm(caseId: string) {
      return client.put<CaseResponse>(`/cases/${caseId}/confirm`)
    },

    /** 关闭工单 */
    close(caseId: string, data?: { close_reason?: CloseReason }) {
      return client.put<CaseResponse>(`/cases/${caseId}/close`, data)
    },

    // ---- Admin ----

    /** [Admin] 所有工单列表 */
    listAll(params?: { skip?: number; limit?: number; status?: string; client_id?: string }) {
      return client.get<CaseListResponse>('/cases/all', { params })
    },

    /** [Admin] 工单统计 */
    stats() {
      return client.get<CaseStatsResponse>('/cases/stats')
    },

    /** [Admin] 客户端列表 */
    clients() {
      return client.get<ClientListResponse>('/cases/clients')
    },
  }
}

/** Assistant API 方法集合 */
export function createAssistantApi(client: AxiosInstance) {
  return {
    /** 获取可用 AI 助手列表（v2.1：结构化响应）*/
    list() {
      return client.get<AssistantsResponse>('/assistants/')
    },
  }
}

/** Conversation API 方法集合 */
export function createConversationApi(client: AxiosInstance) {
  return {
    /** 创建会话（case_id 通过 query parameter 传递） */
    create(caseId: string, assistantType?: string) {
      return client.post<ConversationResponse>('/conversations/', null, {
        params: {
          case_id: caseId,
          ...(assistantType ? { assistant_type: assistantType } : {}),
        },
      })
    },

    /** 获取会话消息历史 */
    getMessages(conversationId: string) {
      return client.get<MessageResponse[]>(`/conversations/${conversationId}/messages`)
    },

    /** 发送消息并接收 SSE 流 */
    sendMessageStream(conversationId: string, content: string): EventSource {
      const params = new URLSearchParams({ content })
      const url = `/api/conversations/${conversationId}/message?${params.toString()}`
      // 使用 POST 方式需要通过 fetch，因 EventSource 仅支持 GET
      // 所以后端 SSE 端点需用 POST，这里改用 fetch + ReadableStream
      // 为与现有后端一致，直接返回 URL，让调用方自行处理
      return new EventSource(url)
    },
  }
}

/** PromptAudit API 方法集合（Admin 专用） */
export function createPromptAuditApi(client: AxiosInstance) {
  return {
    /** [Admin] 获取工单的 PromptAudit 记录列表 */
    listByCaseId(caseId: string, params?: { limit?: number; offset?: number; include_messages?: boolean }) {
      return client.get<{
        case_id: string
        total: number
        offset: number
        limit: number
        records: Array<{
          audit_id: string
          conversation_id: string | null
          assistant_type: string | null
          model: string | null
          has_sop: boolean | null
          kb_chunks_count: number | null
          kb_top_score: number | null
          system_prompt_chars: number | null
          message_count: number | null
          user_rating: number | null
          captured_at: string | null
          messages?: any
        }>
      }>(`/cases/${caseId}/prompt-audit`, { params })
    },
  }
}

/** AuditLog API 方法集合（Admin 专用） */
export function createAuditLogApi(client: AxiosInstance) {
  return {
    /** [Admin] 查询工具调用审计日志 */
    list(params?: {
      session_id?: string
      tool_name?: string
      risk_level?: number
      limit?: number
      offset?: number
    }) {
      return client.get<{
        total: number
        limit: number
        offset: number
        items: Array<{
          id: string
          session_id: string
          tool_name: string
          tool_args: any
          risk_level: number
          policy: string | null
          authorized_by: string | null
          result: any
          error: string | null
          started_at: string | null
          completed_at: string | null
          duration_ms: number | null
          trace_id: string | null
        }>
      }>('/api/v1/audit-logs', { params })
    },
  }
}
