import { describe, expect, it } from 'vitest'
import { consumeConversationStream } from '@hci/shared'

describe('共享 Agent SSE 协议', () => {
  it('支持 CRLF、多行 data、注释和不带结尾空行的事件', async () => {
    const chunks = [
      'event: message\r\ndata: {"content":"你',
      '好"}\r\n\r\n: keepalive\n\nevent: tool_call\ndata: first\ndata: second',
    ]
    const stream = new ReadableStream({
      start(controller) {
        for (const chunk of chunks) controller.enqueue(new TextEncoder().encode(chunk))
        controller.close()
      },
    })
    const events: Array<{ type: string; data: string }> = []
    await consumeConversationStream(new Response(stream, { status: 200 }), (event) => { events.push(event) })
    expect(events).toEqual([
      { type: 'message', data: '{"content":"你好"}' },
      { type: 'tool_call', data: 'first\nsecond' },
    ])
  })
})
