export type ToolEventStatus =
  | 'pending'
  | 'running'
  | 'success'
  | 'failed'
  | 'cancelled'
  | 'blocked'

export interface NormalizedToolEvent extends Record<string, any> {
  args: Record<string, any>
  result?: string | Record<string, any>
  status?: ToolEventStatus
}

/** 统一新旧 Agent 工具事件字段，供实时 SSE 与历史消息复用。 */
export function normalizeToolEvent(raw: Record<string, any>): NormalizedToolEvent {
  const statusMap: Record<string, ToolEventStatus> = {
    completed: 'success',
    error: 'failed',
  }
  return {
    ...raw,
    args: raw.args ?? raw.tool_args ?? {},
    result: raw.result ?? raw.tool_result,
    status: statusMap[raw.status] ?? raw.status,
  }
}
