/**
 * 终端 API 封装
 * 提供 SSH 会话创建、关闭以及 WebSocket URL 生成能力
 */

import type { AxiosInstance, AxiosResponse } from 'axios'

export type TerminalAuthType = 'password' | 'key'

export interface TerminalSessionCreateRequest {
  host: string
  port?: number
  username: string
  auth_type: TerminalAuthType
  password?: string
  private_key?: string
  passphrase?: string
  client_id?: string
  case_id?: string
}

export interface TerminalSessionCreateResponse {
  session_id: string
  host: string
  port: number
  username: string
  status: 'connected' | 'connecting' | 'error'
  message?: string
}

export interface TerminalSessionCloseResponse {
  session_id: string
  status: 'closed'
  message: string
}

export interface TerminalWsMessage {
  type: 'stdin' | 'resize' | 'ping' | 'stdout' | 'stderr' | 'status' | 'pong' | 'error'
  data?: string
  cols?: number
  rows?: number
  state?: 'connected' | 'disconnected' | 'error' | 'connecting'
  message?: string
}

/** 创建终端 API 客户端 */
export function createTerminalApi(client: AxiosInstance) {
  return {
    /** 创建 SSH 会话 */
    createSession(
      data: TerminalSessionCreateRequest,
    ): Promise<AxiosResponse<TerminalSessionCreateResponse>> {
      return client.post<TerminalSessionCreateResponse>('/terminal/sessions', data)
    },

    /** 关闭 SSH 会话 */
    closeSession(sessionId: string): Promise<AxiosResponse<TerminalSessionCloseResponse>> {
      return client.post<TerminalSessionCloseResponse>(`/terminal/sessions/${sessionId}/close`)
    },
  }
}

/** 构建终端 WebSocket URL */
export function buildTerminalWsUrl(sessionId: string, clientId: string): string {
  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
  const encodedSessionId = encodeURIComponent(sessionId)
  const encodedClientId = encodeURIComponent(clientId)
  return `${protocol}//${window.location.host}/ws/terminal/${encodedSessionId}?client_id=${encodedClientId}`
}
