import { describe, expect, it, vi, beforeEach } from 'vitest'
import { h, nextTick, reactive } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import TerminalPanel from '@/components/TerminalPanel.vue'

/**
 * SSH 终端「上条命令」交互契约。
 *
 * 需求原话：在「断开连接」旁边新增「上条命令」，用来直接输入刚被执行的上一条命令。
 *
 * 这里有三个必须锁死的点：
 * 1. 是"回填"不是"重发"——终端命令直接打到客户生产节点，重发必须经过人按 Enter；
 * 2. 回填的必须是最近一次真正下发成功的那条（不是输入框里残留的半截命令）；
 * 3. SSH 断开后必须清空——换主机/换会话后回填上一台机器的命令，等于把 A 机操作送到 B 机。
 */
let mockStore: any

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

vi.mock('@/api/terminal', () => ({
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue({ available: true, message: '' }),
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

const ElInput = {
  name: 'ElInput',
  props: ['modelValue', 'disabled', 'placeholder', 'type', 'rows'],
  emits: ['update:modelValue'],
  setup(props: any, { emit, attrs, expose }: any) {
    // 真实 el-input 会暴露 focus()，回填后要聚焦输入框
    expose({ focus: () => {} })
    return () =>
      h('textarea', {
        class: 'composer-field-stub',
        value: props.modelValue,
        disabled: props.disabled,
        onInput: (e: any) => emit('update:modelValue', e.target.value),
        onKeydown: (e: any) => attrs.onKeydown?.(e),
      })
  },
}

const ElLink = {
  name: 'ElLink',
  props: ['disabled', 'type', 'underline'],
  emits: ['click'],
  setup(props: any, { slots, emit }: any) {
    return () =>
      h(
        'a',
        {
          class: 'link-stub',
          'data-disabled': props.disabled ? 'true' : 'false',
          onClick: (e: any) => {
            if (!props.disabled) emit('click', e)
          },
        },
        slots.default?.(),
      )
  },
}

const ElButton = {
  name: 'ElButton',
  props: ['disabled', 'type', 'text', 'size', 'loading'],
  setup(props: any, { slots, attrs }: any) {
    return () =>
      h(
        'button',
        {
          class: ['btn-stub', attrs.class],
          disabled: props.disabled,
          onClick: (e: any) => !props.disabled && attrs.onClick?.(e),
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
        ElInput,
        ElLink,
        ElButton,
        ElTag: passthrough('ElTag', 'span'),
        ElSelect: passthrough('ElSelect'),
        ElOption: passthrough('ElOption'),
        ElTooltip: passthrough('ElTooltip'),
        ElIcon: passthrough('ElIcon', 'i'),
      },
    },
  })
}

const fieldValue = (wrapper: any) =>
  (wrapper.find('textarea.composer-field-stub').element as HTMLTextAreaElement).value

function findLink(wrapper: any, text: string) {
  return wrapper.findAll('a.link-stub').find((link: any) => link.text() === text)
}

describe('TerminalPanel「上条命令」', () => {
  beforeEach(() => {
    // xterm 初始化会挂 ResizeObserver；happy-dom 的覆盖度随版本变化，这里显式兜底
    ;(globalThis as any).ResizeObserver = class {
      observe() {}
      disconnect() {}
      unobserve() {}
    }
    mockStore = reactive({
      sshConnectionState: 'connected',
      currentCase: { case_id: 'Q2026102700001' },
      logCollectCaseId: '',
      conversationId: 'conv-last-command',
      sshCurrentConfig: null,
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
    })
  })

  it('未执行过命令时「上条命令」不可点', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const link = findLink(wrapper, '上条命令')
    expect(link).toBeTruthy()
    expect(link!.attributes('data-disabled')).toBe('true')
    wrapper.unmount()
  })

  it('执行命令后「上条命令」把该命令回填到输入框，但不自动重发', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const field = wrapper.find('textarea.composer-field-stub')
    await field.setValue('acli log_collect progress')
    await field.trigger('keydown', { key: 'Enter' })

    expect(mockStore.sendSSHCommand).toHaveBeenCalledWith('acli log_collect progress', 'terminal')
    // 执行后输入框应被清空（沿用原有行为）
    expect(fieldValue(wrapper)).toBe('')

    const link = findLink(wrapper, '上条命令')
    expect(link!.attributes('data-disabled')).toBe('false')
    await link!.trigger('click')

    expect(fieldValue(wrapper)).toBe('acli log_collect progress')
    // 关键：只回填，不重发——重发必须由人按 Enter 确认
    expect(mockStore.sendSSHCommand).toHaveBeenCalledTimes(1)
    wrapper.unmount()
  })

  it('回填的是最近一次下发的命令，而不是更早的那条', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const field = wrapper.find('textarea.composer-field-stub')
    await field.setValue('acli log_collect status')
    await field.trigger('keydown', { key: 'Enter' })
    await field.setValue('acli log_collect progress')
    await field.trigger('keydown', { key: 'Enter' })

    await findLink(wrapper, '上条命令')!.trigger('click')
    expect(fieldValue(wrapper)).toBe('acli log_collect progress')
    wrapper.unmount()
  })

  it('SSH 断开后清空上条命令，重连后不会把上一台主机的命令回填回来', async () => {
    const wrapper = mountPanel()
    await flushPromises()

    const field = wrapper.find('textarea.composer-field-stub')
    await field.setValue('rm -rf /sf/data/local/tmp')
    await field.trigger('keydown', { key: 'Enter' })
    expect(findLink(wrapper, '上条命令')).toBeTruthy()

    mockStore.sshConnectionState = 'disconnected'
    await nextTick()
    await flushPromises()
    // 未连接态展示的是"请先连接"，链接随之消失
    expect(findLink(wrapper, '上条命令')).toBeFalsy()

    mockStore.sshConnectionState = 'connected'
    await nextTick()
    await flushPromises()
    const relink = findLink(wrapper, '上条命令')
    expect(relink).toBeTruthy()
    expect(relink!.attributes('data-disabled')).toBe('true')
    wrapper.unmount()
  })
})
