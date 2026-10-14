/**
 * MessageBubble — SOP 变量交互卡片提交状态回归测试
 *
 * 守护工单 Q2026101496655 的修复：
 * 后端 /interactive-response 写入变量池成功后（HTTP 200），
 * 即使随后的 chatStore.sendMessage 触发消息被流式守卫静默吞掉
 * （返回 false），弹框也必须立即进入「已提交」态并给出明确提示，
 * 绝不能再出现「输入框被清空 + isTouched → 假报错『请输入值（必填）』、
 * 弹框不消失诱导用户反复重提」的状态机断裂。
 */

import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mockSendMessage, mockStore, mockElMessage } = vi.hoisted(() => {
  const sendMessage = vi.fn().mockResolvedValue(true)
  return {
    mockSendMessage: sendMessage,
    mockElMessage: {
      info: vi.fn(),
      warning: vi.fn(),
      error: vi.fn(),
      success: vi.fn(),
    },
    mockStore: {
      messages: [] as any[],
      isLoading: false,
      conversationId: 'conv-var-1',
      sendMessage,
      clearInteractiveRequest: vi.fn(),
      clearExecConfirm: vi.fn(),
      resumeOpsAgentStream: vi.fn(),
    },
  }
})

vi.mock('@/stores/chat', () => ({
  useChatStore: () => mockStore,
}))

vi.mock('element-plus', () => ({
  ElMessage: mockElMessage,
}))

/** 构造 variable_input 交互气泡消息（与线上工单落库结构一致） */
function variableInputMessage(requestId = 'req-var-1') {
  return {
    id: `ir-${requestId}`,
    role: 'assistant' as const,
    content: '告警硬盘所在主机',
    timestamp: new Date('2026-10-14T10:04:05+08:00'),
    metadata: {
      kind: 'interactive_request',
      event: {
        kind: 'variable_input',
        title: `填写变量：node_ip`,
        prompt: '告警硬盘所在主机',
        options: [],
        customInput: true,
        requestId,
        acpSessionId: 'conv-var-1',
        metadata: {
          required: true,
          variable_name: 'node_ip',
          variable_type: 'ip',
          validation_pattern: null,
          current_value: null,
          sop_tool: 'sop_request_variable',
        },
      },
    },
  }
}

async function mountBubble(message: Record<string, unknown>) {
  const MessageBubble = (await import('@/components/MessageBubble.vue')).default
  return shallowMount(MessageBubble, {
    props: { message: message as any },
    global: {
      stubs: {
        CommandBlock: true,
        InteractiveOptions: true,
      },
    },
  })
}

/** stub 全局 fetch：指定 /interactive-response 的响应状态 */
function stubFetch(status: number) {
  const fetchMock = vi.fn().mockResolvedValue({ ok: status === 200, status })
  vi.stubGlobal('fetch', fetchMock)
  return fetchMock
}

describe('MessageBubble 变量卡片提交状态（Q2026101496655 回归）', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    vi.unstubAllGlobals()
    mockStore.messages = []
    mockSendMessage.mockResolvedValue(true)
  })

  it('提交成功但触发消息被流式守卫吞掉（sendMessage 返回 false）时，弹框仍进入已提交态并提示', async () => {
    const msg = variableInputMessage()
    mockStore.messages = [msg]
    mockSendMessage.mockResolvedValue(false) // 模拟 isStreaming 吞掉
    const fetchMock = stubFetch(200)

    const wrapper = await mountBubble(msg)
    const setup = (wrapper.getCurrentComponent() as any).setupState

    // 用户输入 IP（触发 @input → isTouched=true）
    setup.interactiveFreeText = '10.71.175.119'
    setup.isTouched = true
    await setup.handleInteractiveFreeText()

    // 后端写入成功即视为已提交：不再依赖后续 interactive_response 消息
    expect(fetchMock).toHaveBeenCalledTimes(1)
    expect(setup.interactiveSubmitted).toBe(true)
    expect(mockElMessage.info).toHaveBeenCalledWith(
      expect.stringContaining('变量值已提交成功'),
    )
    // 表单被「已提交」标签替换，不再渲染输入框与假报错
    const html = wrapper.html()
    expect(html).toContain('已提交')
    expect(html).not.toContain('请输入值（必填）')
    wrapper.unmount()
  })

  it('提交成功且触发消息正常发出（sendMessage 返回 true）时，不弹补充提示', async () => {
    const msg = variableInputMessage('req-var-2')
    mockStore.messages = [msg]
    stubFetch(200)

    const wrapper = await mountBubble(msg)
    const setup = (wrapper.getCurrentComponent() as any).setupState

    setup.interactiveFreeText = '10.71.175.119'
    setup.isTouched = true
    await setup.handleInteractiveFreeText()

    expect(setup.interactiveSubmitted).toBe(true)
    expect(mockSendMessage).toHaveBeenCalledWith('10.71.175.119', {
      kind: 'interactive_response',
    })
    expect(mockElMessage.info).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('提交失败（非 2xx）时不进入已提交态，保留输入内容并报错', async () => {
    const msg = variableInputMessage('req-var-3')
    mockStore.messages = [msg]
    stubFetch(502)

    const wrapper = await mountBubble(msg)
    const setup = (wrapper.getCurrentComponent() as any).setupState

    setup.interactiveFreeText = '10.71.175.119'
    setup.isTouched = true
    await setup.handleInteractiveFreeText()

    expect(setup.interactiveSubmitted).toBe(false)
    // 关键：失败时不清空输入框，用户可直接重试原值
    expect(setup.interactiveFreeText).toBe('10.71.175.119')
    expect(mockElMessage.error).toHaveBeenCalledWith(
      expect.stringContaining('提交失败'),
    )
    expect(mockSendMessage).not.toHaveBeenCalled()
    wrapper.unmount()
  })

  it('历史消息中已存在 interactive_response 时保持既有已提交判定（向后兼容）', async () => {
    const msg = variableInputMessage('req-var-4')
    mockStore.messages = [
      msg,
      {
        id: 'user-resp-1',
        role: 'user',
        content: '10.71.175.119',
        timestamp: new Date(),
        metadata: { kind: 'interactive_response' },
      },
    ]

    const wrapper = await mountBubble(msg)
    const setup = (wrapper.getCurrentComponent() as any).setupState

    expect(setup.interactiveSubmitted).toBe(true)
    expect(wrapper.html()).toContain('已提交')
    wrapper.unmount()
  })

  it('sop_fallback 选择在流式期间被吞掉时提示稍后重选且不标记已提交', async () => {
    const msg = {
      id: 'assistant-sop-fallback-1',
      role: 'assistant' as const,
      content: '请选择后续排查方式',
      timestamp: new Date(),
      metadata: {
        kind: 'interactive_request',
        event: {
          requestId: 'sop-fallback-1',
          acpSessionId: 'conv-var-1',
          kind: 'sop_fallback',
          title: '选择后续排查方式',
          prompt: '当前 SOP 是否解决了问题？',
          options: [
            { optionId: 'continue_sop', name: '继续按当前 SOP 排查' },
            { optionId: 'switch_kbd', name: 'SOP 未解决，切换 KBD' },
          ],
          customInput: false,
          metadata: {},
        },
      },
    }
    mockStore.messages = [msg]
    mockSendMessage.mockResolvedValue(false)

    const wrapper = await mountBubble(msg)
    const setup = (wrapper.getCurrentComponent() as any).setupState

    await setup.handleInteractiveOption('switch_kbd', 'SOP 未解决，切换 KBD')

    expect(mockElMessage.warning).toHaveBeenCalledWith(
      expect.stringContaining('请稍后再选择'),
    )
    expect(setup.interactiveSubmitted).toBe(false)
    wrapper.unmount()
  })
})
