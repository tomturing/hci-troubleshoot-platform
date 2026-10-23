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

const { mockGetById } = vi.hoisted(() => ({ mockGetById: vi.fn() }))

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
vi.mock('@/api/evaluate', () => ({ createEvaluateApi: () => ({}) }))
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

function caseResponse(caseId: string) {
  return { data: { case_id: caseId, client_id: 'test-client-id', status: 'closed' } }
}

describe('chat store - 日志收集人工指定工单号', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    mockGetById.mockReset()
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

  it('有活跃工单时直接开面板，并以当前工单为准', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = { case_id: 'Q2026102300001', status: 'in_progress' } as any

    store.openLogCollect()

    expect(store.logCollectVisible).toBe(true)
    expect(store.logCollectCaseDialogVisible).toBe(false)
    expect(store.logCollectCaseId).toBe('Q2026102300001')
  })

  it('指定他人工单（服务端 404）→ 统一提示，不绑定、不开面板', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockGetById.mockRejectedValue({ response: { status: 404, data: { detail: 'Case not found' } } })

    store.openLogCollect()
    await expect(store.confirmLogCollectCase('Q2026102300099')).rejects.toThrow(
      '工单不存在或不属于当前客户端',
    )

    expect(store.logCollectCaseId).toBe(null)
    expect(store.logCollectVisible).toBe(false)
    expect(store.logCollectCaseDialogVisible).toBe(true)
  })

  it('指定名下已结单工单成功 → 绑定并打开面板，再次打开沿用不重复输入', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockGetById.mockResolvedValue(caseResponse('Q2026102300001'))

    store.openLogCollect()
    await store.confirmLogCollectCase('  Q2026102300001  ')

    expect(mockGetById).toHaveBeenCalledWith('Q2026102300001')
    expect(store.logCollectCaseId).toBe('Q2026102300001')
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
    mockGetById.mockResolvedValue(caseResponse('Q2026102300001'))

    store.openLogCollect()
    await store.confirmLogCollectCase('Q2026102300001')
    store.closeLogCollect()

    store.currentCase = { case_id: 'Q2026102300002', status: 'in_progress' } as any
    store.openLogCollect()

    expect(store.logCollectCaseId).toBe('Q2026102300002')
  })
})
