import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h, nextTick, reactive } from 'vue'
import { flushPromises, mount } from '@vue/test-utils'
import TerminalPanel from '@/components/TerminalPanel.vue'

/**
 * SSH 终端模式二（实时交互终端）交互与录制契约测试。
 *
 * 覆盖核心需求：
 * 1. 保持模式一（关闭自动执行 / 安全模式 / 激进模式）不变；
 * 2. 下拉菜单第 4 项「交互终端（实时 Shell）」切入模式二，隐藏底部输入框、启用实时光标与按键透传；
 * 3. 模式二实时透传 Tab、方向键、Ctrl+C 等控制序列并同步 PTY 窗口尺寸（ssh_resize）；
 * 4. 模式二的输入（含 Tab 补全与方向键历史）与输出（含连续相同字符）均无损录制，支持完整历史回放，
 *    且密码提示行绝不落库明文；
 * 5. 命令卡片注入在模式二下遵循安全边界：单行不回车注入、多行自动切回模式一确认。
 */

const {
  mockRecordInput,
  mockRecordOutput,
  mockForceFlushOutput,
  mockStartRecording,
  mockStopRecording,
  mockRecordingFlag,
  FakeTerminal,
  xtermState,
  fitAddonState,
} = vi.hoisted(() => {
  const xtermState: { latest: any } = { latest: null }
  const fitAddonState: {
    proposeResult: { cols: number; rows: number } | undefined
    fitCalls: number
  } = {
    proposeResult: { cols: 110, rows: 30 },
    fitCalls: 0,
  }

  class HoistedFakeTerminal {
    rows = 30
    cols = 110
    options: Record<string, any>
    write = vi.fn()
    clear = vi.fn()
    dispose = vi.fn()
    loadAddon = vi.fn()
    open = vi.fn()
    resize = vi.fn((cols: number, rows: number) => {
      this.cols = cols
      this.rows = rows
    })
    focus = vi.fn()
    clearSelection = vi.fn()
    selectionText = ''
    getSelection = vi.fn(() => this.selectionText)
    hasSelection = vi.fn(() => Boolean(this.selectionText))
    bufferLine = '[root@hci-node01 ~]# '
    bufferType = 'normal'
    buffer = {
      active: {
        get type() {
          return xtermState.latest?.bufferType ?? 'normal'
        },
        baseY: 0,
        cursorY: 0,
        length: 1,
        getLine: () => ({
          isWrapped: false,
          translateToString: (trimRight?: boolean) =>
            trimRight ? (xtermState.latest?.bufferLine ?? '').trimEnd() : (xtermState.latest?.bufferLine ?? ''),
        }),
      },
    }
    private dataHandler: ((data: string) => void) | null = null
    private keyHandler: ((e: KeyboardEvent) => boolean) | null = null

    constructor(opts: Record<string, any> = {}) {
      this.options = { ...opts }
      xtermState.latest = this
    }

    onData(cb: (data: string) => void) {
      this.dataHandler = cb
      return { dispose: vi.fn() }
    }

    attachCustomKeyEventHandler(cb: (e: KeyboardEvent) => boolean) {
      this.keyHandler = cb
    }

    emitUserData(data: string) {
      this.dataHandler?.(data)
    }

    triggerKeyEvent(e: Partial<KeyboardEvent>): boolean {
      if (!this.keyHandler) return true
      return this.keyHandler({
        type: 'keydown',
        key: '',
        ctrlKey: false,
        metaKey: false,
        shiftKey: false,
        preventDefault: vi.fn(),
        ...e,
      } as KeyboardEvent)
    }
  }

  return {
    mockRecordInput: vi.fn().mockResolvedValue(undefined),
    mockRecordOutput: vi.fn(),
    mockForceFlushOutput: vi.fn().mockResolvedValue(undefined),
    mockStartRecording: vi.fn(),
    mockStopRecording: vi.fn().mockResolvedValue(undefined),
    mockRecordingFlag: { value: true },
    FakeTerminal: HoistedFakeTerminal,
    xtermState,
    fitAddonState,
  }
})

let mockStore: any

vi.mock('@/stores/chat', () => ({ useChatStore: () => mockStore }))

vi.mock('@/api/terminal', () => ({
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue('running'),
}))

vi.mock('@/api/terminal-recording', () => ({
  createRecordingState: () => ({
    isRecording: mockRecordingFlag,
    diagnosticStage: { value: undefined },
  }),
  startRecording: mockStartRecording,
  stopRecording: mockStopRecording,
  recordInput: mockRecordInput,
  recordOutput: mockRecordOutput,
  forceFlushOutput: mockForceFlushOutput,
  stripAnsi: (text: string) => text,
}))

vi.mock('xterm', () => ({
  Terminal: FakeTerminal,
}))

vi.mock('xterm-addon-fit', () => ({
  FitAddon: class {
    proposeDimensions = vi.fn(() => fitAddonState.proposeResult)
    fit = vi.fn(() => {
      fitAddonState.fitCalls++
      if (xtermState.latest && fitAddonState.proposeResult) {
        xtermState.latest.cols = fitAddonState.proposeResult.cols
        xtermState.latest.rows = fitAddonState.proposeResult.rows
      }
    })
  },
}))

const ElSelect = {
  name: 'ElSelect',
  props: ['modelValue', 'size', 'placeholder'],
  emits: ['change'],
  setup(props: any, { slots, emit }: any) {
    return () =>
      h(
        'select',
        {
          class: 'mode-select-stub',
          value: props.modelValue,
          onChange: (e: any) => emit('change', e.target.value),
        },
        slots.default?.(),
      )
  },
}

const ElOption = {
  name: 'ElOption',
  props: ['value', 'label'],
  setup(props: any) {
    return () => h('option', { value: props.value }, props.label)
  },
}

const ElInput = {
  name: 'ElInput',
  props: ['modelValue', 'disabled', 'placeholder', 'type', 'rows'],
  emits: ['update:modelValue'],
  setup(props: any, { emit, attrs, expose }: any) {
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

const passthrough = (name: string, tag = 'div') => ({
  name,
  setup(_: any, { slots, attrs }: any) {
    return () => h(tag, attrs, slots.default?.())
  },
})

function mountPanel() {
  return mount(TerminalPanel, {
    global: {
      stubs: {
        ElSelect,
        ElOption,
        ElInput,
        ElButton: passthrough('ElButton', 'button'),
        ElTag: passthrough('ElTag', 'span'),
        ElTooltip: passthrough('ElTooltip'),
        ElIcon: passthrough('ElIcon', 'i'),
        ElLink: passthrough('ElLink', 'a'),
      },
    },
  })
}

async function flushAnimationFrames() {
  await nextTick()
  await flushPromises()
}

describe('TerminalPanel 模式二（实时交互终端）', () => {
  beforeEach(() => {
    xtermState.latest = null
    fitAddonState.proposeResult = { cols: 110, rows: 30 }
    fitAddonState.fitCalls = 0
    mockRecordingFlag.value = true
    mockRecordInput.mockClear()
    mockRecordOutput.mockClear()
    mockForceFlushOutput.mockClear()
    mockStartRecording.mockClear()
    mockStopRecording.mockClear()

    ;(globalThis as any).ResizeObserver = class {
      observe() {}
      disconnect() {}
      unobserve() {}
    }
    ;(globalThis as any).requestAnimationFrame = (cb: FrameRequestCallback) => {
      cb(0)
      return 1
    }

    mockStore = reactive({
      sshConnectionState: 'connected',
      currentCase: { case_id: 'Q202611270001' },
      logCollectCaseId: '',
      conversationId: 'conv-interactive-1',
      diagnosticStage: 'S2',
      sshCurrentConfig: { host: '10.0.0.10', caseId: 'Q202611270001' },
      sshErrorMessage: '',
      sshOutputBuffer: '[root@hci-node01 ~]# ',
      autoExecuteMode: 'off' as 'off' | 'safe-only' | 'aggressive',
      terminalMode: 'agent' as 'agent' | 'interactive',
      setAutoExecuteMode: vi.fn((mode: 'off' | 'safe-only' | 'aggressive') => {
        mockStore.autoExecuteMode = mode
        mockStore.terminalMode = 'agent'
      }),
      setTerminalMode: vi.fn((mode: 'agent' | 'interactive') => {
        mockStore.terminalMode = mode
        if (mode === 'interactive') mockStore.autoExecuteMode = 'off'
      }),
      setTerminalHeaderMode: vi.fn((mode: 'off' | 'safe-only' | 'aggressive' | 'interactive') => {
        if (mode === 'interactive') {
          mockStore.terminalMode = 'interactive'
          mockStore.autoExecuteMode = 'off'
        } else {
          mockStore.terminalMode = 'agent'
          mockStore.autoExecuteMode = mode
        }
      }),
      closeTerminalSidebar: vi.fn(),
      disconnectSSH: vi.fn(),
      sendSSHCommand: vi.fn(),
      sendSSHRawInput: vi.fn(),
      sendSSHInjectCommand: vi.fn(),
      sendSSHResize: vi.fn(),
      terminalInputCommand: '',
      clearTerminalInput: vi.fn(() => {
        mockStore.terminalInputCommand = ''
      }),
      setAssistantDraftText: vi.fn(),
      sshTerminalOutputEvent: '',
      sshTerminalOutputChunk: null as { seq: number; data: string } | null,
      sshTerminalNotice: null,
      sshCommandConsumer: 'terminal',
      isRightPanelOpen: true,
      rightPanelTab: 'terminal',
    })
  })

  it('顶部下拉菜单包含模式一三档与模式二「交互终端（实时 Shell）」，切换立即改变布局与 PTY 同步', async () => {
    const wrapper = mountPanel()
    await flushAnimationFrames()

    const options = wrapper.findAll('select.mode-select-stub option').map((opt) => ({
      value: opt.attributes('value'),
      text: opt.text(),
    }))
    expect(options).toEqual([
      { value: 'off', text: '关闭自动执行' },
      { value: 'safe-only', text: '安全模式' },
      { value: 'aggressive', text: '激进模式（有风险）' },
      { value: 'interactive', text: '交互终端（实时 Shell）' },
    ])

    // 初始为模式一：保留底部输入框，禁用 xterm 键盘输入
    expect(wrapper.find('.panel-composer').exists()).toBe(true)
    expect(wrapper.find('.interactive-statusbar').exists()).toBe(false)
    expect(xtermState.latest!.options.disableStdin).toBe(true)
    expect(mockStore.sendSSHResize).toHaveBeenCalledWith(110, 30)

    // 切换到模式二：隐藏底部输入框，恢复交互光标并聚焦 xterm
    await wrapper.find('select.mode-select-stub').setValue('interactive')
    await flushAnimationFrames()

    expect(mockStore.setTerminalHeaderMode).toHaveBeenCalledWith('interactive')
    expect(wrapper.find('.panel-composer').exists()).toBe(false)
    expect(wrapper.find('.interactive-statusbar').exists()).toBe(true)
    expect(wrapper.find('.terminal-canvas--interactive').exists()).toBe(true)
    expect(xtermState.latest!.options.disableStdin).toBe(false)
    expect(xtermState.latest!.options.cursorBlink).toBe(true)
    expect(xtermState.latest!.options.convertEol).toBe(false)
    expect(xtermState.latest!.focus).toHaveBeenCalled()

    // 切回安全模式（模式一）：恢复底部输入框
    await wrapper.find('select.mode-select-stub').setValue('safe-only')
    await flushAnimationFrames()

    expect(wrapper.find('.panel-composer').exists()).toBe(true)
    expect(wrapper.find('.interactive-statusbar').exists()).toBe(false)
    expect(xtermState.latest!.options.disableStdin).toBe(true)
    wrapper.unmount()
  })

  it('模式二支持 Tab 补全、方向键与实时按键透传，并完整录制输入输出供历史回放', async () => {
    mockStore.terminalMode = 'interactive'
    const wrapper = mountPanel()
    await flushAnimationFrames()

    // 1. 模拟用户按 Tab 自动补全与方向键上翻历史
    xtermState.latest!.emitUserData('cd /sf/l\t')
    xtermState.latest!.emitUserData('\x1b[A')
    expect(mockStore.sendSSHRawInput).toHaveBeenNthCalledWith(1, 'cd /sf/l\t')
    expect(mockStore.sendSSHRawInput).toHaveBeenNthCalledWith(2, '\x1b[A')
    expect(mockRecordInput).not.toHaveBeenCalled()

    // 2. 模拟远端回显了补全后的完整命令行，用户按 Enter 提交
    xtermState.latest!.bufferLine = '[root@hci-node01 ~]# ls -la /sf/log/today'
    xtermState.latest!.emitUserData('\r')
    await flushPromises()

    expect(mockStore.sendSSHRawInput).toHaveBeenNthCalledWith(3, '\r')
    expect(mockForceFlushOutput).toHaveBeenCalled()
    expect(mockRecordInput).toHaveBeenCalledWith(expect.anything(), 'ls -la /sf/log/today')

    // 3. 连续收到两次完全相同的输出字符（如连按 'l' 'l'），经带 seq 的 chunk 无损写入且全部录制
    mockRecordOutput.mockClear()
    xtermState.latest!.write.mockClear()
    mockStore.sshTerminalOutputChunk = { seq: 1, data: 'l' }
    await nextTick()
    mockStore.sshTerminalOutputChunk = { seq: 2, data: 'l' }
    await nextTick()

    expect(xtermState.latest!.write).toHaveBeenCalledTimes(2)
    expect(xtermState.latest!.write).toHaveBeenNthCalledWith(1, 'l')
    expect(xtermState.latest!.write).toHaveBeenNthCalledWith(2, 'l')
    expect(mockRecordOutput).toHaveBeenCalledTimes(2)

    // 4. Ctrl+C 实时发送并录制 ^C
    xtermState.latest!.emitUserData('\x03')
    await flushPromises()
    expect(mockStore.sendSSHRawInput).toHaveBeenLastCalledWith('\x03')
    expect(mockRecordInput).toHaveBeenLastCalledWith(expect.anything(), '^C')

    // 5. 密码提示行按回车绝不落库明文
    mockRecordInput.mockClear()
    xtermState.latest!.bufferLine = '[sudo] password for root: '
    xtermState.latest!.emitUserData('MySecretPass\r')
    await flushPromises()
    expect(mockRecordInput).not.toHaveBeenCalled()

    wrapper.unmount()
  })

  it('模式二有选区按 Ctrl+C 复制并清选区，无选区按 Ctrl+C 放行中断远端进程', async () => {
    mockStore.terminalMode = 'interactive'
    const writeText = vi.fn().mockResolvedValue(undefined)
    Object.defineProperty(window, 'isSecureContext', { value: true, configurable: true })
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText, readText: vi.fn().mockResolvedValue('echo pasted') },
      configurable: true,
    })

    const wrapper = mountPanel()
    await flushAnimationFrames()

    // 有选区时 Ctrl+C：拦截并复制
    xtermState.latest!.selectionText = 'selected log line'
    const allowWhenSelected = xtermState.latest!.triggerKeyEvent({ ctrlKey: true, key: 'c' })
    expect(allowWhenSelected).toBe(false)
    expect(writeText).toHaveBeenCalledWith('selected log line')
    expect(xtermState.latest!.clearSelection).toHaveBeenCalled()

    // 无选区时 Ctrl+C：放行给 xterm 发 \x03
    xtermState.latest!.selectionText = ''
    const allowWhenEmpty = xtermState.latest!.triggerKeyEvent({ ctrlKey: true, key: 'c' })
    expect(allowWhenEmpty).toBe(true)

    wrapper.unmount()
  })

  it('命令卡片发送到模式二：单行不回车注入命令行，多行切回模式一输入框确认', async () => {
    mockStore.terminalMode = 'interactive'
    const wrapper = mountPanel()
    await flushAnimationFrames()

    // 单行命令直接注入不回车
    mockStore.terminalInputCommand = 'acli system status\n'
    await flushAnimationFrames()
    expect(mockStore.sendSSHInjectCommand).toHaveBeenCalledWith('acli system status')
    expect(mockStore.terminalMode).toBe('interactive')

    // 多行命令为防逐行自动执行，切回模式一放入输入框
    mockStore.terminalInputCommand = 'cd /sf/log\nls -la'
    await flushAnimationFrames()
    expect(mockStore.terminalMode).toBe('agent')
    expect((wrapper.find('.panel-composer textarea.composer-field-stub').element as HTMLTextAreaElement).value).toBe(
      'cd /sf/log\nls -la',
    )

    wrapper.unmount()
  })

  it('防回归：面板折叠/展开动画初期容器极窄（cols=2）时拒绝 fit 与 ssh_resize，防止提示符拆成 2 列竖排', async () => {
    mockStore.terminalMode = 'interactive'
    // 模拟 PanelShell 展开过渡初期 FitAddon 算出的极窄 2 列尺寸
    fitAddonState.proposeResult = { cols: 2, rows: 24 }

    const wrapper = mountPanel()
    await flushAnimationFrames()

    expect(fitAddonState.fitCalls).toBe(0)
    expect(mockStore.sendSSHResize).not.toHaveBeenCalled()
    // 保持安全的初始默认列数（100x30），不压成 2 列
    expect(xtermState.latest!.options.cols).toBe(100)
    expect(xtermState.latest!.options.rows).toBe(30)

    // 模拟展开动画完成、切回终端可见态且尺寸恢复正常（140x36）
    fitAddonState.proposeResult = { cols: 140, rows: 36 }
    mockStore.isRightPanelOpen = false
    await nextTick()
    mockStore.isRightPanelOpen = true
    await flushAnimationFrames()

    expect(fitAddonState.fitCalls).toBeGreaterThanOrEqual(1)
    expect(mockStore.sendSSHResize).toHaveBeenCalledWith(140, 36)
    wrapper.unmount()
  })

  it('防回归：处于文件下载页签（终端 v-show 隐藏）时不执行 fit 也不发 ssh_resize', async () => {
    mockStore.terminalMode = 'interactive'
    mockStore.rightPanelTab = 'sftp'
    fitAddonState.proposeResult = { cols: 120, rows: 32 }

    const wrapper = mountPanel()
    await flushAnimationFrames()

    expect(fitAddonState.fitCalls).toBe(0)
    expect(mockStore.sendSSHResize).not.toHaveBeenCalled()
    wrapper.unmount()
  })
})
