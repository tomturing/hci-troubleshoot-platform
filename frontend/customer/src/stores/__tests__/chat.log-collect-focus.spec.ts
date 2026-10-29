/**
 * chat store - 「日志收集打开 → 中央 Agent 让位」的联动契约。
 *
 * 背景（现场误解）：日志收集与排障 Agent **不联动**——采集是 SSH 会话里跑 acli，
 * Agent 完全不参与。两者并排时用户会以为可以“让 Agent 去收日志”，所以打开日志收集
 * 要自动把右栏切到 SSH 终端，并让中央会话收起，把像素给日志表格与终端。
 *
 * 这里锁的是“不能越界”的四条：
 * 1. 只有 SSH 已连接才自动开终端：没连接时开出来是个空壳，用户还得再登录一次；
 * 2. 覆盖态（窄屏）绝不动右栏：openRightPanel 会反向把刚打开的日志面板关掉；
 * 3. 右栏正在文件下载时不抢页签：用户可能正在传文件；
 * 4. 关闭日志收集不关终端：那条 SSH 会话用户可能还在用。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

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
  buildSftpMessage: vi.fn(),
  parseSftpListPayload: vi.fn(),
  parseSftpTransferPayload: vi.fn(),
}))

async function setup({
  connectionState = 'connected',
  viewport = 1920,
}: {
  connectionState?: 'connected' | 'disconnected'
  viewport?: number
} = {}) {
  const { useChatStore } = await import('../chat')
  const store = useChatStore()
  store.setViewportWidth(viewport)
  store.sshConnectionState = connectionState
  return store
}

/** 打开日志收集（走“接管已有任务”这条入口，它不依赖 currentCase）。 */
async function openLogCollectForTask(store: any) {
  store.openLogCollectForTask('task-1')
  await nextTick()
}

describe('chat store - 日志收集与中央 Agent 让位', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('已连接时打开日志收集：自动展开右栏并落到 SSH 终端页签', async () => {
    const store = await setup()

    await openLogCollectForTask(store)

    expect(store.logCollectVisible).toBe(true)
    expect(store.isRightPanelOpen).toBe(true)
    expect(store.rightPanelTab).toBe('terminal')
    expect(store.isTerminalVisible).toBe(true)
  })

  it('中央 Agent 在日志收集 + 终端同时可见时收起，布局把宽度让给两栏', async () => {
    const store = await setup()

    await openLogCollectForTask(store)

    expect(store.isAgentChatHidden).toBe(true)
    // 中央宽度记 0：这是 App 外壳收合主区的唯一依据
    expect(store.workspaceLayout.centerMaxWidth).toBe(0)
    // 两栏之和等于视口（双开无图标条占位）
    expect(store.workspaceLayout.leftWidth + store.workspaceLayout.rightWidth).toBe(1920)
    // 日志表格与终端都拿到远超常规上限的宽度
    expect(store.workspaceLayout.leftWidth).toBeGreaterThan(420)
    expect(store.workspaceLayout.rightWidth).toBeGreaterThan(560)
  })

  it('未连接 SSH 时不自动开终端，也不隐藏 Agent', async () => {
    const store = await setup({ connectionState: 'disconnected' })

    await openLogCollectForTask(store)

    expect(store.logCollectVisible).toBe(true)
    expect(store.isRightPanelOpen).toBe(false)
    expect(store.isAgentChatHidden).toBe(false)
    expect(store.workspaceLayout.centerMaxWidth).toBeGreaterThan(0)
  })

  it('覆盖态（窄屏）不动右栏：否则 openRightPanel 会反向关掉日志面板', async () => {
    const store = await setup({ viewport: 900 })

    await openLogCollectForTask(store)

    expect(store.logCollectVisible).toBe(true)
    expect(store.isRightPanelOpen).toBe(false)
    expect(store.isAgentChatHidden).toBe(false)
  })

  it('右栏正在文件下载时不抢页签', async () => {
    const store = await setup()
    await store.openSftpPanel()
    expect(store.rightPanelTab).toBe('sftp')

    await openLogCollectForTask(store)

    expect(store.rightPanelTab).toBe('sftp')
    // 终端没出现在屏幕上，就不该把 Agent 藏起来（否则中间会空一块）
    expect(store.isAgentChatHidden).toBe(false)
  })

  it('关闭日志收集不关终端，中央 Agent 恢复', async () => {
    const store = await setup()
    await openLogCollectForTask(store)

    store.closeLogCollect()
    await nextTick()

    expect(store.logCollectVisible).toBe(false)
    // 用户可能还在终端里敲命令，关面板不能顺手把会话入口收掉
    expect(store.isRightPanelOpen).toBe(true)
    expect(store.isTerminalVisible).toBe(true)
    expect(store.isAgentChatHidden).toBe(false)
    expect(store.workspaceLayout.centerMaxWidth).toBeGreaterThan(0)
  })

  it('终端页签被切走后（用户主动看文件下载）Agent 立刻恢复', async () => {
    const store = await setup()
    await openLogCollectForTask(store)
    expect(store.isAgentChatHidden).toBe(true)

    store.openSftpPanel()
    await nextTick()

    expect(store.isAgentChatHidden).toBe(false)
    // 日志面板此时仍在，切页签不该顺带把它关掉
    expect(store.logCollectVisible).toBe(true)
  })
})
