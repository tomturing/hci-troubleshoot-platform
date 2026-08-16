import { shallowMount } from '@vue/test-utils'
import { beforeEach, describe, expect, it, vi } from 'vitest'

const { mockSendMessage, mockStore } = vi.hoisted(() => {
  const sendMessage = vi.fn().mockResolvedValue(undefined)
  return {
    mockSendMessage: sendMessage,
    mockStore: {
      messages: [] as any[],
      isLoading: false,
      conversationId: 'conv-s0-1',
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

function message(options: Record<string, unknown>[], requestId = 'triage-conv-s0-1') {
  return {
    id: 'assistant-s0-1',
    role: 'assistant' as const,
    content: '请确认故障分类',
    timestamp: new Date('2026-07-28T03:14:59+08:00'),
    metadata: {
      kind: 'choice_options',
      schemaVersion: 2,
      requestId,
      options,
    },
  }
}

async function mountBubble(options: Record<string, unknown>[]) {
  const MessageBubble = (await import('@/components/MessageBubble.vue')).default
  return shallowMount(MessageBubble, {
    props: { message: message(options) },
    global: {
      stubs: {
        CommandBlock: true,
        InteractiveOptions: true,
      },
    },
  })
}

async function mountInteractiveBubble(options: Record<string, unknown>[]) {
  const MessageBubble = (await import('@/components/MessageBubble.vue')).default
  return shallowMount(MessageBubble, {
    props: {
      message: {
        id: 'assistant-interactive-s0-1',
        role: 'assistant' as const,
        content: '请确认故障分类',
        timestamp: new Date('2026-08-16T12:00:00+08:00'),
        metadata: {
          kind: 'interactive_request',
          event: {
            requestId: 'triage-interactive-s0-1',
            acpSessionId: 'conv-s0-1',
            kind: 'intent_selection',
            title: '请确认故障分类',
            prompt: '请选择最匹配当前故障的分类',
            options,
            customInput: false,
            metadata: {},
          },
        },
      },
    },
    global: {
      stubs: {
        CommandBlock: true,
        InteractiveOptions: true,
      },
    },
  })
}

describe('MessageBubble S0 稳定分类身份', () => {
  beforeEach(() => {
    vi.clearAllMocks()
    mockStore.messages = []
    mockStore.isLoading = false
  })

  it('稳定 category code 不改变页面的圆圈序号展示', async () => {
    const wrapper = await mountBubble([
      {
        optionId: '虚拟机-038',
        code: '虚拟机-038',
        categoryName: '虚拟机IO读写慢',
        name: '虚拟机-038 虚拟机IO读写慢',
      },
      {
        optionId: '虚拟机-003',
        code: '虚拟机-003',
        categoryName: '虚拟机开机失败',
        name: '虚拟机-003 虚拟机开机失败',
      },
      { optionId: '__none__', name: '以上都不是（请补充症状描述）' },
    ])
    const setup = (wrapper.getCurrentComponent() as any).setupState

    expect(setup.choiceOptions.map((choice: any) => choice.label)).toEqual(['①', '②', '③'])
    await setup.handleChoiceSelect(setup.choiceOptions[1])

    expect(mockSendMessage).toHaveBeenCalledWith('②', {
      kind: 'intent_selection_response',
      selectedOptionId: '虚拟机-003',
      selectedCategoryCode: '虚拟机-003',
      selectedCategoryName: '虚拟机开机失败',
      isNoneOfAbove: false,
      sourceMessageId: 'assistant-s0-1',
      sourceRequestId: 'triage-conv-s0-1',
    })
    wrapper.unmount()
  })

  it('历史卡片前置幻觉项存在时，点击③仍传递虚拟机-003', async () => {
    const wrapper = await mountBubble([
      { optionId: '1', name: 'ubu-sus-25 ' },
      { optionId: '2', name: '虚拟机-038 虚拟机IO读写慢' },
      { optionId: '3', name: '虚拟机-003 虚拟机开机失败' },
      { optionId: '4', name: '存储-020 虚拟存储性能告警' },
      { optionId: '5', name: '以上都不是（请补充症状描述）' },
    ])
    const setup = (wrapper.getCurrentComponent() as any).setupState

    await setup.handleChoiceSelect(setup.choiceOptions[2])

    expect(mockSendMessage).toHaveBeenCalledWith(
      '③',
      expect.objectContaining({
        selectedOptionId: '3',
        selectedCategoryCode: '虚拟机-003',
        selectedCategoryName: '虚拟机开机失败',
      }),
    )
    wrapper.unmount()
  })

  it('以上都不是的补充文本带结构化 none 语义', async () => {
    const wrapper = await mountBubble([
      { optionId: '虚拟机-003', name: '虚拟机-003 虚拟机开机失败' },
      { optionId: '__none__', name: '以上都不是（请补充症状描述）' },
    ])
    const setup = (wrapper.getCurrentComponent() as any).setupState

    await setup.handleChoiceSelect(setup.choiceOptions[1])
    setup.freeInputText = '虚拟机可以开机，但是网络不通'
    await setup.handleFreeInputSubmit()

    expect(mockSendMessage).toHaveBeenCalledWith(
      '② 虚拟机可以开机，但是网络不通',
      expect.objectContaining({
        selectedOptionId: '__none__',
        isNoneOfAbove: true,
        freeText: '虚拟机可以开机，但是网络不通',
      }),
    )
    wrapper.unmount()
  })

  it('兼容 interactive_request 的以上都不是必须先填写症状', async () => {
    const wrapper = await mountInteractiveBubble([
      { optionId: '虚拟机-003', name: '虚拟机-003 虚拟机开机失败' },
      { optionId: '__none__', name: '以上都不是（请补充症状描述）' },
    ])
    const setup = (wrapper.getCurrentComponent() as any).setupState

    await setup.handleInteractiveOption('__none__', '以上都不是（请补充症状描述）')

    expect(mockSendMessage).not.toHaveBeenCalled()
    expect(setup.pendingInteractiveNoneChoice).toEqual({
      optionId: '__none__',
      optionName: '以上都不是（请补充症状描述）',
    })

    setup.interactiveNoneSymptomText = '虚拟机迁移时任务报错且一直重试'
    await setup.submitInteractiveNoneInput()

    expect(setup.submittedInteractiveIntentOptionId).toBe('__none__')
    expect(mockSendMessage).toHaveBeenCalledWith(
      '② 虚拟机迁移时任务报错且一直重试',
      expect.objectContaining({
        selectedOptionId: '__none__',
        isNoneOfAbove: true,
        freeText: '虚拟机迁移时任务报错且一直重试',
        sourceRequestId: 'triage-interactive-s0-1',
      }),
    )
    wrapper.unmount()
  })

  it('页面刷新后按 stable category code 恢复已选高亮', async () => {
    mockStore.messages = [
      {
        id: 'assistant-s0-1',
        role: 'assistant',
        content: '请确认故障分类',
        timestamp: new Date(),
      },
      {
        id: 'user-s0-1',
        role: 'user',
        content: '③',
        timestamp: new Date(),
        metadata: {
          kind: 'intent_selection_response',
          selectedOptionId: '虚拟机-003',
          selectedCategoryCode: '虚拟机-003',
          sourceMessageId: 'assistant-s0-1',
        },
      },
    ]
    const wrapper = await mountBubble([
      { optionId: '虚拟机-038', name: '虚拟机-038 虚拟机IO读写慢' },
      { optionId: '虚拟机-003', name: '虚拟机-003 虚拟机开机失败' },
      { optionId: '__none__', name: '以上都不是（请补充症状描述）' },
    ])
    const setup = (wrapper.getCurrentComponent() as any).setupState

    expect(setup.hasBeenInteracted).toBe(true)
    expect(setup.interactedChoice).toBe('②')
    wrapper.unmount()
  })
})
