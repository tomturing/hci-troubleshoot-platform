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
  it('shows signal-free references without asking for fabricated evidence or marking a diagnosis', async () => {
    const MessageBubble = (await import('@/components/MessageBubble.vue')).default
    const wrapper = shallowMount(MessageBubble, {
      props: { message: {
        id: 'reference', role: 'assistant', content: '', timestamp: new Date(),
        metadata: { semantic_entry: { decision: 'case_recommendations', candidates: [{
          kbd_id: '1', support_id: '15936', title: '缺少介质驱动程序', diagnosis_capability: 'reference_only',
          problem_excerpt: '安装系统报错', recommendation_solution: '已审核的参考方法',
          resource_revision: { revision: 8 },
        }] } },
      } },
      global: { stubs: { CommandBlock: true, InteractiveOptions: true } },
    })
    expect(wrapper.text()).toContain('相关历史案例')
    expect(wrapper.text()).toContain('尚未通过现场验证')
    expect(wrapper.text()).toContain('安装系统报错')
    expect(wrapper.text()).toContain('已审核的参考方法')
    expect(wrapper.text()).toContain('发布修订：8')
    expect(wrapper.find('.semantic-evidence-form').exists()).toBe(false)
  })

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

  it('renders reviewed root cause and solution verbatim for a semantic recommendation', async () => {
    const MessageBubble = (await import('@/components/MessageBubble.vue')).default
    const wrapper = shallowMount(MessageBubble, {
      props: {
        message: {
          id: 'semantic-recommendation',
          role: 'assistant',
          content: '',
          timestamp: new Date(),
          metadata: {
            semantic_entry: {
              decision: 'semantic_recommendation',
              reason: 'semantic_recommendation',
              candidates: [
                {
                  kbd_id: '223',
                  support_id: '15936',
                  title: 'ISO 安装缺少介质驱动程序',
                  recommendation_conclusion: '安装镜像不完整或缺少磁盘控制器驱动',
                  recommendation_solution: '更换经校验的完整安装镜像，并加载 VirtIO 磁盘控制器驱动',
                  recommendation_facts: [{ question: '错误是否发生在选择要安装的驱动程序界面？', answer: '是' }],
                },
              ],
            },
          },
        },
      },
      global: { stubs: { CommandBlock: true, InteractiveOptions: true } },
    })

    const text = wrapper.text()
    expect(text).toContain('历史案例根因：安装镜像不完整或缺少磁盘控制器驱动')
    expect(text).toContain('解决方案（原始文本）：更换经校验的完整安装镜像，并加载 VirtIO 磁盘控制器驱动')
    expect(text).not.toContain('推荐结论：')
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
