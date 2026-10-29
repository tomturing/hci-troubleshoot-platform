/**
 * App 外壳 - 「日志收集进行中，中央 Agent 让位」的渲染契约。
 *
 * 现场误解：日志收集面板与右侧排障 Agent 并排时，用户以为可以让 Agent 参与采集。
 * 处理办法有两层，这里锁第二层（渲染层）：
 * 1. 终端就绪 ⇒ 中央区收成 0 宽（`is-center-hidden`），会话用 v-show 隐藏而不是销毁；
 * 2. 终端未就绪 ⇒ 中央区保留，但标题栏必须挂着「与排障 Agent 不联动」的显式提示。
 *
 * 为什么必须是 v-show：v-if 会卸载 ChatWindow，消息、滚动位置、输入框草稿全部丢失，
 * 用户关掉日志收集后就“断片”了。
 */

import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { h, reactive } from 'vue'
import { mount } from '@vue/test-utils'
import ElementPlus from 'element-plus'

const chat = reactive({
  workspaceLayout: {
    mode: 'slide' as const,
    leftWidth: 880,
    rightWidth: 1040,
    centerMaxWidth: 0,
    leftOffset: 880,
    rightOffset: 1040,
  },
  viewportWidth: 1920,
  currentCase: { case_id: 'Q2026102900001', title: '虚拟机挂起失败' } as any,
  hasActiveCase: true,
  isAgentChatHidden: false,
  logCollectVisible: true,
  logCollectCaseId: 'Q2026102900001' as string | null,
  leftPanelTab: 'log' as 'log' | 'history',
  rightPanelTab: 'terminal' as 'terminal' | 'sftp',
  isLeftPanelOpen: true,
  isRightPanelOpen: true,
  showHistoryDrawer: false,
  leftPanelWidth: 880,
  rightPanelWidth: 1040,
  isPanelResizing: false,
  bridgeStatus: 'running',
  sshConnectDialogBridgeStatus: 'running',
  showBridgeDownload: false,
  initialize: vi.fn(),
  setViewportWidth: vi.fn(),
  openLogCollect: vi.fn(),
  closeLogCollect: vi.fn(),
  openHistoryDrawer: vi.fn(),
  collapseLeftPanel: vi.fn(),
  closeTerminalSidebar: vi.fn(),
  checkAndOpenTerminal: vi.fn(),
  openSftpPanel: vi.fn(),
  openTerminalReplay: vi.fn(),
  closeBridgeDownload: vi.fn(),
  handleCloseCase: vi.fn(),
  setPanelResizing: vi.fn(),
  setLeftPanelPreferred: vi.fn(),
  setRightPanelPreferred: vi.fn(),
})

vi.mock('@/stores/chat', () => ({ useChatStore: () => chat }))
vi.mock('@/utils/clientId', () => ({ getClientId: () => 'client-1' }))

const App = (await import('@/App.vue')).default

const passthroughPanel = {
  name: 'PanelShell',
  props: ['side', 'open', 'width', 'overlay', 'resizing'],
  setup(_: any, { slots }: any) {
    return () =>
      h('aside', { class: 'panel-stub' }, [
        slots.rail?.(),
        slots.title?.(),
        slots.default?.(),
      ])
  },
}

function mountApp() {
  return mount(App, {
    global: {
      plugins: [ElementPlus],
      stubs: {
        PanelShell: passthroughPanel,
        ChatWindow: { template: '<div class="chat-window-stub">chat</div>' },
        HistoryPanel: { template: '<div class="history-stub" />' },
        LogCollectPanel: { template: '<div class="log-panel-stub" />' },
        TerminalPanel: { template: '<div class="terminal-stub" />' },
        SftpPanel: { template: '<div class="sftp-stub" />' },
        LogCollectCaseDialog: { template: '<div />' },
        SshConnectDialog: { template: '<div />' },
      },
    },
  })
}

describe('App 外壳 - 日志收集联动', () => {
  beforeEach(() => {
    chat.isAgentChatHidden = false
    chat.logCollectVisible = true
  })

  it('终端就绪时中央区收成 0 宽，会话被隐藏但仍在 DOM 里', async () => {
    chat.isAgentChatHidden = true
    const wrapper = mountApp()

    expect(wrapper.find('main').classes()).toContain('is-center-hidden')
    // v-show：元素还在，只是 style.display 被置为 none（v-if 会连状态一起丢掉）
    const chatWindow = wrapper.find('.chat-window-stub')
    expect(chatWindow.exists()).toBe(true)
    expect(chatWindow.attributes('style') ?? '').toContain('display: none')
    // 中央已被收起，就不该再占用标题栏去挂提示
    expect(wrapper.find('.main-head-tag').exists()).toBe(false)
    wrapper.unmount()
  })

  it('终端未就绪时中央区保留，标题栏显式声明「与排障 Agent 不联动」', async () => {
    chat.isAgentChatHidden = false
    const wrapper = mountApp()

    expect(wrapper.find('main').classes()).not.toContain('is-center-hidden')
    expect(wrapper.find('.chat-window-stub').attributes('style') ?? '').not.toContain('display: none')
    const tag = wrapper.find('.main-head-tag')
    expect(tag.exists()).toBe(true)
    expect(tag.text()).toContain('与排障 Agent 不联动')
    wrapper.unmount()
  })

  it('关闭日志收集后中央区恢复（提示也随之消失）', async () => {
    const wrapper = mountApp()
    expect(wrapper.find('.main-head-tag').exists()).toBe(true)

    chat.isAgentChatHidden = false
    chat.logCollectVisible = false
    await wrapper.vm.$nextTick()

    expect(wrapper.find('main').classes()).not.toContain('is-center-hidden')
    expect(wrapper.find('.main-head-tag').exists()).toBe(false)
    wrapper.unmount()
  })
})

describe('App 外壳源码约束', () => {
  const source = readFileSync(resolve(process.cwd(), 'src/App.vue'), 'utf-8')

  it('中央隐藏用 0 宽而不是 display:none（否则两侧面板不会重新分配像素）', () => {
    const rule = source.match(/\.app-main\.is-center-hidden\s*\{([\s\S]*?)\}/)?.[1] ?? ''
    expect(rule).toMatch(/flex:\s*0 0 0/)
    expect(rule).toMatch(/width:\s*0/)
    expect(rule).toMatch(/overflow:\s*hidden/)
  })

  it('ChatWindow 用 v-show 而不是 v-if', () => {
    expect(source).toContain('<ChatWindow v-show="!chatStore.isAgentChatHidden" />')
    expect(source).not.toMatch(/<ChatWindow\s+v-if/)
  })
})
