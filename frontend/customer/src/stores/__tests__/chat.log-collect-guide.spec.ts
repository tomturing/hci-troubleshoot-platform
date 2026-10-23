/**
 * chat store 日志收集入口状态测试
 *
 * 背景：日志收集面板原先只有 header 按钮，且依赖活跃工单，客户难以发现。
 * 建单后引导卡片的状态必须满足：
 * - 只在有活跃工单时出现（关闭/取消工单立即隐藏）；
 * - 「暂不需要」按 case_id 记忆，同一工单不再重复打扰；
 * - 换新工单（case_id 变化）后重新出现，不被上一单的 dismiss 影响；
 * - 引导关闭不等于功能关闭：openLogCollect 与 dismiss 相互独立。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

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

const GUIDE_DISMISS_KEY = 'hci.logCollectGuide.dismissedCaseId'

function activeCase(caseId: string) {
  return { case_id: caseId, status: 'in_progress' } as any
}

describe('chat store - 日志收集引导', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('无活跃工单时不展示引导', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    expect(store.showLogCollectGuide).toBe(false)
  })

  it('活跃工单展示引导，工单关闭后立即隐藏', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.currentCase = activeCase('Q2026102200001')
    expect(store.showLogCollectGuide).toBe(true)

    store.currentCase = { case_id: 'Q2026102200001', status: 'closed' } as any
    expect(store.showLogCollectGuide).toBe(false)
  })

  it('「暂不需要」按工单记忆，且不影响抽屉打开能力', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.currentCase = activeCase('Q2026102200001')
    store.dismissLogCollectGuide()

    expect(store.showLogCollectGuide).toBe(false)
    expect(localStorage.getItem(GUIDE_DISMISS_KEY)).toBe('Q2026102200001')

    // 引导关闭只影响引导本身，header 入口仍必须可用
    // 入口现在强依赖 SSH 会话，这里模拟已登录状态以验证"直接进面板"路径
    store.sshConnectionState = 'connected'
    store.openLogCollect()
    expect(store.logCollectVisible).toBe(true)
    store.closeLogCollect()
    expect(store.logCollectVisible).toBe(false)
  })

  it('换新工单后引导重新出现，不被上一单 dismiss 影响', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.currentCase = activeCase('Q2026102200001')
    store.dismissLogCollectGuide()
    expect(store.showLogCollectGuide).toBe(false)

    store.currentCase = activeCase('Q2026102200002')
    expect(store.showLogCollectGuide).toBe(true)
  })
})
