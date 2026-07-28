import { mount } from '@vue/test-utils'
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'

const { mockSendCommandToTerminal, mockLegacyExecute } = vi.hoisted(() => ({
  mockSendCommandToTerminal: vi.fn(),
  // 仅作为负向哨兵：组件不得再访问旧的 Markdown 自动执行入口。
  mockLegacyExecute: vi.fn(),
}))

vi.mock('@/stores/chat', () => ({
  useChatStore: () => ({
    autoExecuteMode: 'aggressive',
    sshConnectionState: 'connected',
    sendCommandToTerminal: mockSendCommandToTerminal,
    executeCommandViaSSH: mockLegacyExecute,
  }),
}))

describe('CommandBlock 执行信任边界', () => {
  beforeEach(() => {
    vi.useFakeTimers()
    vi.clearAllMocks()
  })

  afterEach(() => {
    vi.useRealTimers()
  })

  it('Aggressive 模式和 SSH 已连接时，普通 Markdown 命令块也绝不自动执行', async () => {
    const CommandBlock = (await import('@/components/CommandBlock.vue')).default
    const wrapper = mount(CommandBlock, {
      props: {
        command: 'PR632_E2E_OK\nLinux',
        language: 'bash',
        riskLevel: 'caution',
      },
      global: {
        stubs: {
          'el-button': {
            emits: ['click'],
            template: '<button v-bind="$attrs" @click="$emit(\'click\')"><slot /></button>',
          },
          'el-tag': {
            template: '<span><slot /></span>',
          },
          'el-icon': {
            template: '<span><slot /></span>',
          },
        },
      },
    })

    await vi.advanceTimersByTimeAsync(10_000)

    expect(mockLegacyExecute).not.toHaveBeenCalled()
    expect(wrapper.text()).not.toContain('后自动执行')
    expect(wrapper.text()).not.toContain('执行中')
    wrapper.unmount()
  })

  it('只有用户显式点击时才把展示命令填入人工终端', async () => {
    const CommandBlock = (await import('@/components/CommandBlock.vue')).default
    const wrapper = mount(CommandBlock, {
      props: {
        command: "printf 'SAFE\\n'",
        language: 'bash',
        riskLevel: 'readonly',
      },
      global: {
        stubs: {
          'el-button': {
            emits: ['click'],
            template: '<button v-bind="$attrs" @click="$emit(\'click\')"><slot /></button>',
          },
          'el-tag': {
            template: '<span><slot /></span>',
          },
          'el-icon': {
            template: '<span><slot /></span>',
          },
        },
      },
    })

    await wrapper.get('.send-btn').trigger('click')

    expect(mockSendCommandToTerminal).toHaveBeenCalledOnce()
    expect(mockSendCommandToTerminal).toHaveBeenCalledWith("printf 'SAFE\\n'")
    expect(mockLegacyExecute).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
