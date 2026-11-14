/**
 * chat store - 「终端打开 ⇒ 中央让位」的联动契约。
 *
 * 背景（现场回归）：旧版右栏是 el-drawer 浮层，打开 SSH 终端会盖住中央排障窗口；
 * 面板化成 flex 三栏后中央变成抢像素的邻居，1080 / 1366 屏上终端只剩 60 列左右。
 * 现在的契约是「终端页签可见就让位」，并且让位态下左右两栏共占整屏。
 *
 * 这里锁的是“不能越界”的五条：
 * 1. 只有 SSH 已连接才自动开终端：没连接时开出来是个空壳，用户还得再登录一次；
 * 2. 让位的条件是“终端可见”，不是“日志收集可见”——单开终端也必须让位；
 * 3. 右栏正在文件下载时不让位：传文件不需要 xterm 列宽，藏了中央只剩空白；
 * 4. 1024~1439（1080 / 1366 屏）双开是压缩布局，不许把用户的面板关掉；
 * 5. 真·窄屏（<1024）仍是覆盖式“一次看一块”，打开终端会收掉另一侧。
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

describe('chat store - 终端打开后中央让位', () => {
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

  it('日志收集 + 终端同时可见：中央让位，两栏铺满视口', async () => {
    const store = await setup()

    await openLogCollectForTask(store)

    expect(store.isCenterCollapsed).toBe(true)
    // 中央宽度记 0：这是 App 外壳收合主区的唯一依据
    expect(store.workspaceLayout.centerMaxWidth).toBe(0)
    // 两栏之和等于视口（双开无图标条占位）
    expect(store.workspaceLayout.leftWidth + store.workspaceLayout.rightWidth).toBe(1920)
    // 日志表格与终端都拿到远超常规上限的宽度
    expect(store.workspaceLayout.leftWidth).toBeGreaterThan(420)
    expect(store.workspaceLayout.rightWidth).toBeGreaterThan(560)
  })

  it('单开终端（没有日志收集）也让位：这是「终端覆盖中央」的等价行为', async () => {
    const store = await setup({ viewport: 1080 })

    await store.checkAndOpenTerminal()
    await nextTick()

    expect(store.logCollectVisible).toBe(false)
    expect(store.isTerminalVisible).toBe(true)
    expect(store.isCenterCollapsed).toBe(true)
    expect(store.workspaceLayout.centerMaxWidth).toBe(0)
    // 左栏收起成图标条，终端吃掉剩下的整屏
    expect(store.workspaceLayout.leftWidth).toBe(48)
    expect(store.workspaceLayout.rightWidth).toBe(1080 - 48)
  })

  it('未连接 SSH 时不自动开终端，也不让位', async () => {
    const store = await setup({ connectionState: 'disconnected' })

    await openLogCollectForTask(store)

    expect(store.logCollectVisible).toBe(true)
    expect(store.isRightPanelOpen).toBe(false)
    expect(store.isCenterCollapsed).toBe(false)
    expect(store.workspaceLayout.centerMaxWidth).toBeGreaterThan(0)
  })

  it('真·窄屏（<1024）仍是覆盖式：打开日志收集不动右栏', async () => {
    const store = await setup({ viewport: 900 })

    await openLogCollectForTask(store)

    expect(store.logCollectVisible).toBe(true)
    expect(store.isRightPanelOpen).toBe(false)
    expect(store.isCenterCollapsed).toBe(false)
  })

  it('1080 屏：打开日志收集自动并排终端，两栏共占整屏且日志面板不被关', async () => {
    const store = await setup({ viewport: 1080 })

    await openLogCollectForTask(store)

    expect(store.logCollectVisible).toBe(true)
    expect(store.isRightPanelOpen).toBe(true)
    expect(store.isCenterCollapsed).toBe(true)
    expect(store.workspaceLayout.centerMaxWidth).toBe(0)
    expect(store.workspaceLayout.leftWidth).toBeGreaterThanOrEqual(420)
    expect(store.workspaceLayout.rightWidth).toBeGreaterThanOrEqual(560)
    expect(store.workspaceLayout.leftWidth + store.workspaceLayout.rightWidth).toBe(1080)
  })

  it('1080 屏：采集进行中点开终端，不会把正在采集的日志面板关掉', async () => {
    const store = await setup({ viewport: 1080, connectionState: 'disconnected' })

    await openLogCollectForTask(store)
    expect(store.isRightPanelOpen).toBe(false)

    // 用户随后在弹框里登录成功，再点顶栏「SSH终端」
    store.sshConnectionState = 'connected'
    await store.checkAndOpenTerminal()
    await nextTick()

    expect(store.logCollectVisible).toBe(true)
    expect(store.isTerminalVisible).toBe(true)
    expect(store.isCenterCollapsed).toBe(true)
    expect(store.workspaceLayout.leftWidth + store.workspaceLayout.rightWidth).toBe(1080)
  })

  it('真·窄屏（<1024）手动点终端：覆盖式互斥仍然生效（一次看一块）', async () => {
    const store = await setup({ viewport: 900, connectionState: 'disconnected' })

    await openLogCollectForTask(store)
    store.sshConnectionState = 'connected'
    await store.checkAndOpenTerminal()
    await nextTick()

    expect(store.logCollectVisible).toBe(false)
    expect(store.isTerminalVisible).toBe(true)
    expect(store.workspaceLayout.mode).toBe('overlay')
  })

  it('右栏正在文件下载时不让位', async () => {
    const store = await setup()
    await store.openSftpPanel()
    expect(store.rightPanelTab).toBe('sftp')

    await openLogCollectForTask(store)

    expect(store.rightPanelTab).toBe('sftp')
    // 终端没出现在屏幕上，就不该把中央藏起来（否则中间会空一块）
    expect(store.isCenterCollapsed).toBe(false)
  })

  it('关闭日志收集不关终端：中央仍让位给还开着的终端', async () => {
    const store = await setup()
    await openLogCollectForTask(store)

    store.closeLogCollect()
    await nextTick()

    expect(store.logCollectVisible).toBe(false)
    // 用户可能还在终端里敲命令，关面板不能顺手把会话入口收掉
    expect(store.isRightPanelOpen).toBe(true)
    expect(store.isTerminalVisible).toBe(true)
    // 终端还在屏幕上，中央继续让位（关掉终端才恢复）
    expect(store.isCenterCollapsed).toBe(true)
    expect(store.workspaceLayout.centerMaxWidth).toBe(0)
  })

  it('收起终端后中央恢复', async () => {
    const store = await setup()
    await openLogCollectForTask(store)

    store.closeTerminalSidebar()
    await nextTick()

    expect(store.isCenterCollapsed).toBe(false)
    expect(store.workspaceLayout.centerMaxWidth).toBeGreaterThan(0)
  })

  it('终端页签被切走后（用户主动看文件下载）中央立刻恢复', async () => {
    const store = await setup()
    await openLogCollectForTask(store)
    expect(store.isCenterCollapsed).toBe(true)

    store.openSftpPanel()
    await nextTick()

    expect(store.isCenterCollapsed).toBe(false)
    // 日志面板此时仍在，切页签不该顺带把它关掉
    expect(store.logCollectVisible).toBe(true)
  })
})