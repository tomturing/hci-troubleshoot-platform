/**
 * chat store - 右栏页签（SSH 终端 / 文件下载）契约测试
 *
 * 背景：「文件下载」从终端底部拆出，成为与「SSH 终端」平级的右栏页签。
 * 两者共用同一块面板位置与同一条 SSH 会话，因此必须锁死四点：
 * 1. 未登录点文件下载 → 不开面板，先弹**统一的** SSH 登录框（与终端同一模板）；
 * 2. 登录成功后要落到文件下载页签，而不是把用户丢回终端（靠挂起意图传递）；
 * 3. 取消登录必须清掉这个意图，否则之后任意一次登录都会莫名跳到文件下载；
 * 4. 终端页签不可见时不得往终端写命令（例如 acli 安装后的 hash -r）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('@/services/acliManager', () => ({ ensureAcliReady: vi.fn() }))

vi.mock('@hci/shared', () => ({
  createApiClient: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createCaseApi: () => ({
    listByClient: vi.fn().mockResolvedValue({ data: [] }),
    getById: vi.fn(),
    close: vi.fn().mockResolvedValue({}),
    create: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createConversationApi: () => ({
    create: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-test' } }),
    getMessages: vi.fn().mockResolvedValue({ data: [] }),
  }),
  createAssistantApi: () => ({
    list: vi.fn().mockResolvedValue({ data: { assistants: [], default: 'htp-agent' } }),
  }),
  createEnvironmentApi: () => ({
    getEnvironmentByCase: vi.fn().mockResolvedValue({ data: null }),
    reportEnvironment: vi.fn().mockResolvedValue({}),
    getContext: vi.fn().mockResolvedValue({ data: { env_info: {}, alert_logs: [], task_logs: [] } }),
    listByCase: vi.fn().mockResolvedValue({ data: [] }),
    create: vi.fn().mockResolvedValue({ data: {} }),
    upsert: vi.fn().mockResolvedValue({ data: {} }),
  }),
}))

vi.mock('@/utils/clientId', () => ({ getClientId: () => 'test-client-id' }))

vi.mock('@/api/terminal', () => ({
  checkBridgeRunning: vi.fn().mockResolvedValue(true),
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue('running'),
  createBridgeSocket: vi.fn(),
  getBridgeUrl: vi.fn().mockReturnValue('ws://127.0.0.1:17680'),
  buildConnectMessage: vi.fn(),
  buildInputMessage: vi.fn(),
  buildDisconnectMessage: vi.fn(),
  buildAgentExecMessage: vi.fn(),
  buildAgentExecProcessMessage: vi.fn(),
  buildVmConsoleOpMessage: vi.fn(),
  parseAgentExecResult: vi.fn(),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

async function setup(connectionState: 'connected' | 'disconnected' = 'connected') {
  const { useChatStore } = await import('../chat')
  const store = useChatStore()
  store.sshConnectionState = connectionState
  return store
}

describe('chat store - 右栏页签', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('未登录点「文件下载」→ 不开面板，先弹统一 SSH 登录框', async () => {
    const store = await setup('disconnected')

    await store.openSftpPanel()

    expect(store.isRightPanelOpen).toBe(false)
    expect(store.sshFlowDialogVisible).toBe(true)
    // 登录弹框与终端用的是同一个入口，不是文件下载专属表单
    expect(store.sshFlowDialogMode).toBe('terminal-only')
  })

  it('登录成功后落到「文件下载」页签（消费挂起意图）', async () => {
    const store = await setup('disconnected')
    await store.openSftpPanel()

    // 模拟 SshConnectSection 登录成功的唯一回调
    store.openTerminalSidebar()

    expect(store.isRightPanelOpen).toBe(true)
    expect(store.rightPanelTab).toBe('sftp')
    expect(store.isTerminalVisible).toBe(false)
  })

  it('取消登录清掉挂起意图：之后登录仍回到终端页', async () => {
    const store = await setup('disconnected')
    await store.openSftpPanel()

    await store.closeSshFlowDialog()
    store.openTerminalSidebar()

    expect(store.rightPanelTab).toBe('terminal')
  })

  it('已登录点「文件下载」→ 直接打开右栏并切到该页签', async () => {
    const store = await setup('connected')

    await store.openSftpPanel()

    expect(store.isRightPanelOpen).toBe(true)
    expect(store.rightPanelTab).toBe('sftp')
    expect(store.sshFlowDialogVisible).toBe(false)
  })

  it('往终端发命令时自动切回终端页（文件下载页会遮住输入框）', async () => {
    const store = await setup('connected')
    await store.openSftpPanel()

    store.sendCommandToTerminal('acli log_collect progress')

    expect(store.rightPanelTab).toBe('terminal')
    expect(store.isTerminalVisible).toBe(true)
  })
})
