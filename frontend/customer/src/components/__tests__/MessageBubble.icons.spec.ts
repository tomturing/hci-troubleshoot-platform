import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { mount } from '@vue/test-utils'
import { describe, expect, it, vi } from 'vitest'

const mockStore = vi.hoisted(() => ({
  messages: [] as any[],
  isLoading: false,
  conversationId: 'icons-conversation',
  sendMessage: vi.fn(),
  clearInteractiveRequest: vi.fn(),
  clearExecConfirm: vi.fn(),
  resumeOpsAgentStream: vi.fn(),
}))

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

/** emoji 与几何图形字符：在 Windows 上可能落到 Segoe UI Symbol，16~22px 显示为黑块或空白 */
const EMOJI_LIKE = /[\u{1F300}-\u{1FAFF}\u{2600}-\u{27BF}\u{25B6}\u{25BC}\u{2B07}]/u

/** 图标位必须用图标组件：源码里不允许再出现 emoji 图标（避免依赖终端用户系统字体） */
describe('MessageBubble 图标实现', () => {
  it('模板中不再硬编码 emoji 图标', () => {
    const file = resolve(process.cwd(), 'src/components/MessageBubble.vue')
    const source = readFileSync(file, 'utf-8')
    const matched = source.match(EMOJI_LIKE)
    expect(matched?.[0] ?? '').toBe('')
  })

  it('交互卡标题渲染图标组件而不是 emoji 文本', async () => {
    const MessageBubble = (await import('@/components/MessageBubble.vue')).default
    const wrapper = mount(MessageBubble, {
      props: {
        message: {
          id: 'icons-interactive',
          role: 'assistant',
          content: '',
          timestamp: new Date(),
          metadata: {
            kind: 'interactive_request',
            event: {
              requestId: 'icons-sop-1',
              acpSessionId: 'conv-icons-1',
              kind: 'sop_step',
              // 后端未下发 title 时使用前端兜底标题
              title: '',
              prompt: '确认已执行该步骤',
              options: [{ optionId: 'done', name: '已执行' }],
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
          'el-icon': { template: '<i class="el-icon-stub"><slot /></i>' },
        },
      },
    })

    const title = wrapper.find('.interactive-title')
    expect(title.exists()).toBe(true)
    expect(title.find('.el-icon-stub').exists()).toBe(true)
    expect(title.text()).toContain('SOP 操作步骤确认')
    expect(title.text()).not.toMatch(EMOJI_LIKE)
  })
})
