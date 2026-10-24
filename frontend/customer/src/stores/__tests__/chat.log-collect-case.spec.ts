/**
 * chat store 日志收集「人工指定工单号」测试
 *
 * 背景：入口改为常驻后，无活跃工单（刚进页面 / 工单已关闭）也要能采集日志，
 * 但日志任务必须挂在平台工单上，因此允许用户手工指定工单号。锁定四点：
 * 1. 无活跃工单时不直接开面板，而是弹框要求指定；
 * 2. 有活跃工单时一律以当前工单为准，不被历史上手工指定的值污染；
 * 3. 指定工单必须经服务端归属校验（404/403 统一提示，不泄露存在性）；
 * 4. 指定成功后再次打开沿用该工单，不重复输入。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const { mockGetById, mockResolveCase } = vi.hoisted(() => ({
  mockGetById: vi.fn(),
  mockResolveCase: vi.fn(),
}))

vi.mock('@hci/shared', () => ({
  createApiClient: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createCaseApi: () => ({
    listByClient: vi.fn().mockResolvedValue({ data: [] }),
    getById: mockGetById,
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
vi.mock('@/api/logCollect', () => ({
  logCollectApi: { resolveCase: mockResolveCase },
}))
vi.mock('@/api/terminal', () => ({
  checkBridgeRunning: vi.fn().mockResolvedValue(false),
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue('not-running'),
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

describe('chat store - 日志收集人工指定工单号', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockResolveCase.mockReset()
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('无活跃工单时打开入口 → 弹框要求指定工单号，不直接开面板', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.openLogCollect()

    expect(store.logCollectCaseDialogVisible).toBe(true)
    expect(store.logCollectVisible).toBe(false)
    expect(store.logCollectCaseId).toBe(null)
  })

  it('有活跃工单且已登录 SSH 时直接开面板，并以当前工单为准', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = { case_id: 'Q2026102300001', status: 'in_progress' } as any
    store.sshConnectionState = 'connected'

    store.openLogCollect()

    expect(store.logCollectVisible).toBe(true)
    expect(store.logCollectCaseDialogVisible).toBe(false)
    expect(store.logCollectCaseId).toBe('Q2026102300001')
  })

  it('未登录 SSH 时即使有活跃工单也先开弹框（强制登录）', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = { case_id: 'Q2026102300003', status: 'in_progress' } as any

    store.openLogCollect()

    expect(store.logCollectCaseDialogVisible).toBe(true)
    expect(store.logCollectVisible).toBe(false)
    // 预填当前工单号，减少一次输入
    expect(store.logCollectCaseId).toBe('Q2026102300003')
  })

  it('服务端拒绝格式（422）→ 提示格式错误，不绑定、不开面板', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockResolveCase.mockRejectedValue({
      response: { status: 422, data: { detail: '工单号中的日期不合法' } },
    })

    store.openLogCollect()
    await expect(store.confirmLogCollectCase('Q2026133200001')).rejects.toThrow('日期不合法')

    expect(store.logCollectCaseId).toBe(null)
    expect(store.logCollectVisible).toBe(false)
    expect(store.logCollectCaseDialogVisible).toBe(true)
  })

  it('工单号不存在时由服务端建档（created=true）→ 绑定并打开面板', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockResolveCase.mockResolvedValue({ case_id: 'Q2026101401684', created: true })

    store.sshConnectionState = 'connected'
    store.openLogCollect()
    await store.confirmLogCollectCase('  Q2026101401684  ')

    expect(mockResolveCase).toHaveBeenCalledWith('Q2026101401684')
    expect(store.logCollectCaseId).toBe('Q2026101401684')
    expect(store.logCollectVisible).toBe(true)
    expect(store.logCollectCaseDialogVisible).toBe(false)

    // 再次打开：沿用人工指定的工单，不再弹框
    store.closeLogCollect()
    store.openLogCollect()
    expect(store.logCollectVisible).toBe(true)
    expect(store.logCollectCaseDialogVisible).toBe(false)
  })

  it('历史手工指定不会覆盖当前活跃工单', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockResolveCase.mockResolvedValue({ case_id: 'Q2026101401684', created: false })

    store.sshConnectionState = 'connected'
    store.openLogCollect()
    await store.confirmLogCollectCase('Q2026101401684')
    store.closeLogCollect()

    store.currentCase = { case_id: 'Q2026102300002', status: 'in_progress' } as any
    store.openLogCollect()

    expect(store.logCollectCaseId).toBe('Q2026102300002')
  })

  it('409 后接管查看：openLogCollectForTask 设置 resumeTaskId 并打开面板', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.openLogCollectForTask('11111111-1111-1111-1111-111111111111')

    expect(store.logCollectResumeTaskId).toBe('11111111-1111-1111-1111-111111111111')
    expect(store.logCollectVisible).toBe(true)
  })

  it('复用了他人工单（owned=false）→ 绑定但标记未归属，供面板提示', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockResolveCase.mockResolvedValue({
      case_id: 'Q2026101401684',
      created: false,
      owned: false,
    })

    store.sshConnectionState = 'connected'
    store.openLogCollect()
    await store.confirmLogCollectCase('Q2026101401684')

    // 采集仍允许（运维需用外部单号补采），但必须暴露"日志回不去"的事实
    expect(store.logCollectCaseId).toBe('Q2026101401684')
    expect(store.logCollectVisible).toBe(true)
    expect(store.logCollectCaseOwned).toBe(false)
  })

  it('本人名的工单（owned=true）→ 不显示未归属提示', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockResolveCase.mockResolvedValue({
      case_id: 'Q2026102300009',
      created: false,
      owned: true,
    })

    store.sshConnectionState = 'connected'
    store.openLogCollect()
    await store.confirmLogCollectCase('Q2026102300009')

    expect(store.logCollectCaseOwned).toBe(true)
  })
})
