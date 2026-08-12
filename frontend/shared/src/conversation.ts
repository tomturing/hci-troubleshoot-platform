/** 浏览器会话流的最小共享协议层。 */

export interface ConversationStreamEvent {
  type: string
  data: string
}

/**
 * 按 SSE 规范消费 fetch() 的响应流。
 *
 * Customer UI 与 Admin UI 必须共享同一事件分帧语义；产品可以有不同外壳，
 * 但不能分别实现一套对空行、CRLF 和多行 data 行为不一致的 Agent 协议。
 */
export async function consumeConversationStream(
  response: Response,
  onEvent: (event: ConversationStreamEvent) => void | Promise<void>,
): Promise<void> {
  if (!response.ok || !response.body) throw new Error(`Agent 会话 HTTP ${response.status}`)

  const reader = response.body.getReader()
  const decoder = new TextDecoder()
  let buffer = ''
  let eventType = 'message'
  let dataLines: string[] = []

  const dispatch = async () => {
    if (!dataLines.length) {
      eventType = 'message'
      return
    }
    const data = dataLines.join('\n')
    dataLines = []
    const type = eventType
    eventType = 'message'
    if (data !== '[DONE]') await onEvent({ type, data })
  }

  const consumeLine = async (rawLine: string) => {
    const line = rawLine.endsWith('\r') ? rawLine.slice(0, -1) : rawLine
    if (!line) return dispatch()
    if (line.startsWith(':')) return
    const separator = line.indexOf(':')
    const field = separator === -1 ? line : line.slice(0, separator)
    let value = separator === -1 ? '' : line.slice(separator + 1)
    if (value.startsWith(' ')) value = value.slice(1)
    if (field === 'event') eventType = value || 'message'
    if (field === 'data') dataLines.push(value)
  }

  while (true) {
    const { done, value } = await reader.read()
    if (done) break
    buffer += decoder.decode(value, { stream: true })
    const lines = buffer.split('\n')
    buffer = lines.pop() || ''
    for (const line of lines) await consumeLine(line)
  }
  buffer += decoder.decode()
  if (buffer) await consumeLine(buffer)
  await dispatch()
}

/** 容错读取事件 JSON；普通 message 允许直接传文本。 */
export function parseConversationEvent(data: string): Record<string, unknown> {
  try {
    const value = JSON.parse(data)
    return value && typeof value === 'object' ? value as Record<string, unknown> : { content: String(value ?? '') }
  } catch {
    return { content: data }
  }
}
