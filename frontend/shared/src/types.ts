/**
 * 共享类型定义 - 与后端 schemas.py 对应
 */

/** 工单状态 */
export type CaseStatus = 'created' | 'confirmed' | 'in_progress' | 'resolved' | 'closed' | 'cancelled'

/** 消息角色 */
export type MessageRole = 'user' | 'assistant' | 'system' | 'command'

/** 工单响应 */
export interface CaseResponse {
  case_id: string
  client_id: string
  status: CaseStatus
  title: string
  description: string | null
  assistant_type?: string
  created_at: string
  updated_at: string
  closed_at: string | null
  trace_id: string | null
}

/** 创建工单请求 */
export interface CaseCreate {
  client_id: string
  title: string
  description?: string
  assistant_type?: string  // v2.0: 可选，不传则系统自动分配
}

/** AI 助手信息 */
export interface AssistantInfo {
  type: string
  display_name: string
  description: string
  available: boolean
}

/** 工单分页列表响应 */
export interface CaseListResponse {
  items: CaseResponse[]
  total: number
  skip: number
  limit: number
}

/** 工单统计响应 */
export interface CaseStatsResponse {
  total: number
  by_status: Record<string, number>
}

/** 客户端信息 */
export interface ClientInfo {
  client_id: string
  case_count: number
  last_case_at: string | null
}

/** 客户端列表响应 */
export interface ClientListResponse {
  items: ClientInfo[]
  total: number
}

/** 消息响应 */
export interface MessageResponse {
  message_id: string
  conversation_id: string
  role: MessageRole
  content: string
  metadata: Record<string, unknown> | null
  created_at: string
  trace_id: string | null
}

/** 创建消息请求 */
export interface MessageCreate {
  case_id: string
  role: MessageRole
  content: string
  metadata?: Record<string, unknown>
}

/** 会话响应 */
export interface ConversationResponse {
  conversation_id: string
  case_id: string
  created_at: string
  updated_at: string
  trace_id: string | null
}

/** 状态标签配色映射 */
export const STATUS_LABELS: Record<CaseStatus, string> = {
  created: '待确认',
  confirmed: '已确认',
  in_progress: '处理中',
  resolved: '已解决',
  closed: '已关闭',
  cancelled: '已取消',
}

export const STATUS_COLORS: Record<CaseStatus, string> = {
  created: 'warning',
  confirmed: 'primary',
  in_progress: 'primary',
  resolved: 'success',
  closed: 'info',
  cancelled: 'danger',
}
