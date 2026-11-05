/**
 * TerminalPanel 未连接态的入口契约。
 *
 * SSH 会话已与工单解耦：终端 / 文件下载 / 日志收集 / Agent 排障共用同一条会话。
 * 未连接时，终端面板只能引导「连接 SSH」，不得再出现「连接 SSH 并创建工单」，
 * 也不得把没有任何工单的用户挡在建单流程里。
 */

import { describe, expect, it, vi, beforeEach } from 'vitest'
import { h, nextTick, reactive } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import TerminalPanel from '@/components/TerminalPanel.vue'

const { mockCheckBridge } = vi.hoisted(() => ({
  mockCheckBridge: vi.fn().mockResolvedValue('running'),
}))

let mockStore: any

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

vi.mock('@/api/terminal', () => ({
  checkBridgeBeforeOpen: mockCheckBridge,
}))

vi.mock('@/api/terminal-recording', () => ({
  createRecordingState: () => ({ isRecording: { value: false } }),
  startRecording: vi.fn(),
  stopRecording: vi.fn(),
  recordInput: vi.fn(),
  recordOutput: vi.fn(),
  forceFlushOutput: vi.fn(),
  stripAnsi: (text: string) => text,
}))

vi.mock('xterm', () => ({
  Terminal: class {
    rows = 24
    cols = 80
    write = vi.fn()
    clear = vi.fn()
    dispose = vi.fn()
    loadAddon = vi.fn()
    open = vi.fn()
    resize = vi.fn()
  },
}))

vi.mock('xterm-addon-fit', () => ({
  FitAddon: class {
    fit = vi.fn()
  },
}))

const ElButton = {
  name: 'ElButton',
  // 声明 emits：否则 @click 会同时作为原生 listener 继承到根元素，
  // 手动 onClick 再转发一次 → 一次点击触发两遍。
  emits: ['click'],
  props: ['disabled', 'type', 'text', 'size', 'loading'],
  setup(props: any, { slots, attrs, emit }: any) {
    return () =>
      h(
        'button',
        {
          class: ['btn-stub', attrs.class],
          disabled: props.disabled,
          onClick: () => {
            if (!props.disabled && !props.loading) emit('click')
          },
        },
        slots.default?.(),
      )
  },
}

const passthrough = (name: string, tag = 'div') => ({
  name,
  setup(_: any, { slots }: any) {
    return () => h(tag, slots.default?.())
  },
})

function mountPanel() {
  return mount(TerminalPanel, {
    global: {
      stubs: {
        ElButton,
        ElTag: passthrough('ElTag', 'span'),
        ElSelect: passthrough('ElSelect'),
        ElOption: passthrough('ElOption'),
        ElTooltip: passthrough('ElTooltip'),
        ElIcon: passthrough('ElIcon', 'i'),
        ElInput: {
          name: 'ElInput',
          props: ['modelValue', 'disabled', 'placeholder', 'type', 'rows'],
          setup() {
            return () => h('textarea', { class: 'composer-field-stub' })
          },
        },
        ElLink: passthrough('ElLink', 'a'),
      },
    },
  })
}

describe('TerminalPanel - SSH 独立登录入口', () => {
  beforeEach(() => {
    ;(globalThis as any).ResizeObserver = class {
      observe() {}
      disconnect() {}
      unobserve() {}
    }
    mockCheckBridge.mockClear()
    mockCheckBridge.mockResolvedValue('running')
    mockStore = reactive({
      sshConnectionState: 'disconnected',
      currentCase: null,
      logCollectCaseId: '',
      conversationId: null,
      sshErrorMessage: '',
      autoExecuteMode: 'off',
      setAutoExecuteMode: vi.fn(),
      closeTerminalSidebar: vi.fn(),
      disconnectSSH: vi.fn(),
      sendSSHCommand: vi.fn(),
      terminalInputCommand: '',
      clearTerminalInput: vi.fn(),
      setAssistantDraftText: vi.fn(),
      sshTerminalOutputEvent: '',
      sshTerminalNotice: null,
      sshCommandConsumer: 'terminal',
      isRightPanelOpen: true,
      rightPanelTab: 'terminal',
      // 旧链路的关键状态：任何路径都不应再把它置 true
      showCaseTemplate: false,
      caseCreateDialogBridgeStatus: 'checking',
      sshConnectDialogBridgeStatus: 'checking',
      openSshFlowDialog: vi.fn(),
    })
  })

  it('无工单且未连接时只提供「连接 SSH」，不再引导建单', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const area = wrapper.find('.ssh-connect-area')
    expect(area.exists()).toBe(true)
    expect(area.text()).toContain('连接 SSH')
    expect(area.text()).not.toContain('创建工单')
    wrapper.unmount()
  })

  it('点击连接只打开统一登录弹框，不打开创建工单弹框', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('button.btn-connect-ssh').trigger('click')
    await nextTick()
    await flushPromises()

    expect(mockCheckBridge).toHaveBeenCalledTimes(1)
    expect(mockStore.openSshFlowDialog).toHaveBeenCalledWith(null, 'terminal-only')
    expect(mockStore.showCaseTemplate).toBe(false)
    wrapper.unmount()
  })

  it('无聊天工单但日志收集已绑定工单时，登录带上该工单用于录像归档', async () => {
    mockStore.logCollectCaseId = 'Q2026110500002'
    const wrapper = mountPanel()
    await flushPromises()

    await wrapper.find('button.btn-connect-ssh').trigger('click')
    await nextTick()
    await flushPromises()

    expect(mockStore.openSshFlowDialog).toHaveBeenCalledWith('Q2026110500002', 'terminal-only')
    wrapper.unmount()
  })
})
