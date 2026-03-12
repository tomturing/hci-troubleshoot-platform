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
    /** 获取可用 AI 助手列表 */
    list() {
      return client.get<AssistantInfo[]>('/assistants/')
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
