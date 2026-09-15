import { shallowMount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const mockStore = vi.hoisted(() => ({
  messages: [] as any[],
  isLoading: false,
  conversationId: 'semantic-conversation',
  sendMessage: vi.fn(),
  clearInteractiveRequest: vi.fn(),
  clearExecConfirm: vi.fn(),
  resumeOpsAgentStream: vi.fn(),
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

describe('MessageBubble semantic entry', () => {
  it('renders matched support ID and title before the evidence guidance', async () => {
    const MessageBubble = (await import('@/components/MessageBubble.vue')).default
    const wrapper = shallowMount(MessageBubble, {
      props: {
        message: {
          id: 'semantic-message',
          role: 'assistant',
          content: '当前只能请求补充证据：请提供安装失败截图',
          timestamp: new Date(),
          metadata: {
            semantic_entry: {
              decision: 'inconclusive',
              reason: 'guidance_only',
              candidates: [{ support_id: '15936', title: 'ISO 安装缺少介质驱动程序' }],
            },
          },
        },
      },
      global: { stubs: { CommandBlock: true, InteractiveOptions: true } },
    })

    expect(wrapper.text()).toContain('语义入口已命中')
    expect(wrapper.text()).toContain('案例 15936')
    expect(wrapper.text()).toContain('ISO 安装缺少介质驱动程序')
  })

  it('submits only the profile-declared structured evidence values', async () => {
    const MessageBubble = (await import('@/components/MessageBubble.vue')).default
    const wrapper = shallowMount(MessageBubble, {
      props: {
        message: {
          id: 'semantic-message', role: 'assistant', content: '', timestamp: new Date(),
          metadata: {
            semantic_entry: {
              candidates: [{
                kbd_id: '223', support_id: '15936', title: '示例',
                manual_evidence_fields: [{ id: 'controller_type', label: '控制器类型' }],
              }],
            },
          },
        },
      },
      global: {
        stubs: {
          CommandBlock: true,
          InteractiveOptions: true,
          ElInput: { props: ['modelValue'], emits: ['update:modelValue'], template: '<input :value="modelValue" @input="$emit(\'update:modelValue\', $event.target.value)" />' },
          ElButton: { template: '<button @click="$emit(\'click\')"><slot /></button>' },
        },
      },
    })
    await wrapper.find('input').setValue('VirtIO')
    await wrapper.find('button').trigger('click')

    expect(mockStore.sendMessage).toHaveBeenCalledWith(
      '补充证据：控制器类型：VirtIO',
      expect.objectContaining({
        kind: 'semantic_evidence_response', candidateId: '223', values: { controller_type: 'VirtIO' }, sourceMessageId: 'semantic-message',
      }),
    )
  })
})
