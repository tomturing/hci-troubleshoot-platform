/**
 * chat store - 「SSH 会话独立于工单」的入口契约。
 *
 * 背景（现场问题）：SSH 连接已经是公共能力，终端 / 文件下载 / 日志收集 / Agent 排障
 * 共用同一条会话，但顶栏「SSH 终端」与终端空状态仍沿用旧的三态逻辑：
 * 无工单时点进去先弹「连接 SSH 并创建工单」。只想登录看终端的用户被迫走完整建单流程。
 *
 * 这里锁死三条：
 * 1. 未连接时点击 SSH 终端 = 打开统一登录弹框（terminal-only），不打开空白面板；
 * 2. 已连接时 = 零探测直接展示终端（探测会把每次点击拖 1.5s，且会话已存在无需再问 Bridge）；
 * 3. Bridge 不可用时仍走下载提示，不弹登录框（登录框里没有可用的 Bridge）。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const { mockCheckBridgeBeforeOpen } = vi.hoisted(() => ({
  mockCheckBridgeBeforeOpen: vi.fn().mockResolvedValue('running'),
}))

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
  checkBridgeBeforeOpen: mockCheckBridgeBeforeOpen,
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
  buildSftpMessage: vi.fn(),
  parseSftpListPayload: vi.fn(),
  parseSftpTransferPayload: vi.fn(),
}))

async function setup(connectionState: 'connected' | 'disconnected' = 'disconnected') {
  const { useChatStore } = await import('../chat')
  const store = useChatStore()
  store.setViewportWidth(1920)
  store.sshConnectionState = connectionState
  return store
}

describe('chat store - SSH 终端独立入口', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockCheckBridgeBeforeOpen.mockReset()
    mockCheckBridgeBeforeOpen.mockResolvedValue('running')
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('未连接时点击 SSH 终端：展开右栏并打开统一登录弹框，不再弹创建工单', async () => {
    const store = await setup('disconnected')
    store.showCaseTemplate = true // 先前的建单弹框若还开着，必须被收起

    await store.checkAndOpenTerminal()

    expect(mockCheckBridgeBeforeOpen).toHaveBeenCalledTimes(1)
    expect(store.sshFlowDialogVisible).toBe(true)
    expect(store.sshFlowDialogMode).toBe('terminal-only')
    // 建单弹框不得与登录弹框叠加
    expect(store.showCaseTemplate).toBe(false)
    // 右栏同时展开：用户取消登录后仍能看到终端面板与「连接 SSH」重试入口
    expect(store.isRightPanelOpen).toBe(true)
    expect(store.rightPanelTab).toBe('terminal')
  })

  it('登录成功后的回调落到 SSH 终端页签', async () => {
    const store = await setup('disconnected')

    await store.checkAndOpenTerminal()
    // SshConnectSection 登录成功后会调用 openTerminalSidebar()
    store.openTerminalSidebar()

    expect(store.isRightPanelOpen).toBe(true)
    expect(store.rightPanelTab).toBe('terminal')
  })

  it('已连接时零探测直接展示终端', async () => {
    const store = await setup('connected')

    await store.checkAndOpenTerminal()

    expect(mockCheckBridgeBeforeOpen).not.toHaveBeenCalled()
    expect(store.isRightPanelOpen).toBe(true)
    expect(store.rightPanelTab).toBe('terminal')
    expect(store.sshFlowDialogVisible).toBe(false)
  })

  it('Bridge 不可用：走下载提示，不弹登录框', async () => {
    mockCheckBridgeBeforeOpen.mockResolvedValue('not-running')
    const store = await setup('disconnected')

    await store.checkAndOpenTerminal()

    expect(store.showBridgeDownload).toBe(true)
    expect(store.sshFlowDialogVisible).toBe(false)
    expect(store.isRightPanelOpen).toBe(false)
  })

  it('打开任意 SSH 登录弹框都会先收起创建工单弹框（同一时刻只允许一个模态）', async () => {
    const store = await setup('disconnected')
    store.showCaseTemplate = true

    store.openSshFlowDialog('Q2026110500001', 'log-collect')

    expect(store.showCaseTemplate).toBe(false)
    expect(store.sshFlowDialogVisible).toBe(true)
    expect(store.sshFlowDialogMode).toBe('log-collect')
  })
})
