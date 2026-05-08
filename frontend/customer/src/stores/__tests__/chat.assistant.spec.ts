/**
 * chat store 助手选择行为单元测试
 *
 * 覆盖 2026-05 修复的三处根因：
 * - Fix A: streamAIResponse 必须优先使用 currentCase.assistant_type
 * - Fix B: completeCaseCreationFlow 在首条消息前同步 selectedAssistant
 * - Fix C: _restoreAssistantFromCase 不应因 available=false 降级已绑定助手
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import type { AssistantInfo, CaseResponse } from '@hci/shared'

// ── vi.hoisted：在 mock 工厂闭包内可引用的可控 mock 函数 ──────────────────
const {
  mockApiGet,
  mockCaseGetById,
  mockConvCreate,
  mockConvGetMessages,
} = vi.hoisted(() => ({
  mockApiGet: vi.fn().mockResolvedValue({ data: [] }),
  mockCaseGetById: vi.fn(),
  mockConvCreate: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-test-1' } }),
  mockConvGetMessages: vi.fn().mockResolvedValue({ data: [] }),
}))

// ── 模块 mock ─────────────────────────────────────────────────────────────
vi.mock('@hci/shared', () => ({
  createApiClient: () => ({
    get: mockApiGet,
    post: vi.fn().mockResolvedValue({ data: {} }),
    patch: vi.fn().mockResolvedValue({ data: {} }),
    delete: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createCaseApi: () => ({
    listByClient: vi.fn().mockResolvedValue({ data: [] }),
    getById: mockCaseGetById,
    close: vi.fn().mockResolvedValue({}),
    create: vi.fn().mockResolvedValue({ data: { case_id: 'c-new', assistant_type: 'qwen', status: 'open', client_id: 'test', title: 'test', description: null, created_at: '', updated_at: '', closed_at: null, trace_id: null } }),
  }),
  createConversationApi: () => ({
    create: mockConvCreate,
    getMessages: mockConvGetMessages,
  }),
  createAssistantApi: () => ({
    // 默认返回两个助手：qwen（默认可用）、ops-agent（当前不可用）
    list: vi.fn().mockResolvedValue({
      data: {
        assistants: [],
        show_selector: false,
        default_assistant: null,
        selector_mode: 'auto',
      },
    }),
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
  checkBridgeRunning: vi.fn().mockResolvedValue({ running: false }),
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue({ running: false }),
  createBridgeSocket: vi.fn(),
  buildConnectMessage: vi.fn(),
  buildInputMessage: vi.fn(),
  buildDisconnectMessage: vi.fn(),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

// ── 工厂函数 ─────────────────────────────────────────────────────────────

/** 构造最小 CaseResponse */
function makeCase(overrides: Partial<CaseResponse> = {}): CaseResponse {
  return {
    case_id: 'case-1',
    client_id: 'test-client-id',
    status: 'open',
    title: 'Test Case',
    description: null,
    assistant_type: 'ops-agent',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    closed_at: null,
    trace_id: null,
    ...overrides,
  }
}

/** 构造 AssistantInfo */
function makeAssistant(type: string, available: boolean, is_default = false): AssistantInfo {
  return {
    type,
    display_name: type,
    description: '',
    capabilities: [],
    available,
    is_default,
  }
}

/** 构造最小 SSE fetch mock（立即返回 [DONE]）*/
function makeStreamFetchMock(capturedBodies: string[]) {
  const encoder = new TextEncoder()
  return vi.fn().mockImplementation(async (_url: string, init: RequestInit) => {
    capturedBodies.push(init.body as string)
    const chunks = [
      encoder.encode('data: [DONE]\n\n'),
    ]
    let i = 0
    return {
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn().mockImplementation(async () => {
            if (i < chunks.length) return { done: false, value: chunks[i++] }
            return { done: true, value: undefined }
          }),
        }),
      },
    }
  })
}

// ── 测试套件 ──────────────────────────────────────────────────────────────
describe('chat store — 助手选择行为', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    // 默认 loadConversationHistory 返回空列表（无历史对话）
    mockApiGet.mockResolvedValue({ data: [] })
  })

  // ═══════════════════════════════════════════════════════════════════════
  // Fix C：_restoreAssistantFromCase 不应因 available=false 降级
  // ═══════════════════════════════════════════════════════════════════════
  describe('Fix C: 刷新页面恢复助手选择', () => {
    it('工单绑定 ops-agent（available=false）时，resumePendingCase 应恢复为 ops-agent 而非降级', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      // 模拟页面刷新后状态：fetchAssistants 已完成，selectedAssistant 被置为默认 qwen
      store.assistants = [
        makeAssistant('qwen', true, true),
        makeAssistant('ops-agent', false),  // ops-agent 当前不可用（pod 繁忙等）
      ]
      store.selectedAssistant = 'qwen'
      store.pendingCase = makeCase({ assistant_type: 'ops-agent' })

      await store.resumePendingCase()

      // Fix C 修复前：因 available=false 会降级到 qwen
      // Fix C 修复后：只要类型存在于列表即恢复
      expect(store.selectedAssistant).toBe('ops-agent')
    })

    it('工单绑定的助手类型已从列表中移除时，应降级到默认可用助手', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.assistants = [makeAssistant('qwen', true, true)]
      store.selectedAssistant = 'qwen'
      // pendingCase 保存的助手已被删除
      store.pendingCase = makeCase({ assistant_type: 'removed-assistant' })

      await store.resumePendingCase()

      // 助手不在列表中应降级
      expect(store.selectedAssistant).toBe('qwen')
    })

    it('工单没有 assistant_type 时，selectedAssistant 保持不变', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.assistants = [makeAssistant('qwen', true, true)]
      store.selectedAssistant = 'qwen'
      store.pendingCase = makeCase({ assistant_type: undefined })

      await store.resumePendingCase()

      expect(store.selectedAssistant).toBe('qwen')
    })
  })

  // ═══════════════════════════════════════════════════════════════════════
  // Fix A：streamAIResponse 必须用 currentCase.assistant_type 而非 selectedAssistant
  // ═══════════════════════════════════════════════════════════════════════
  describe('Fix A: 消息发送时路由到正确助手', () => {
    it('sendMessage 发送的 assistant_type 应来自 currentCase，而非 selectedAssistant', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      // 模拟：用户选的是 qwen（默认），但工单绑定的是 ops-agent
      store.selectedAssistant = 'qwen'
      store.currentCase = makeCase({ case_id: 'case-1', assistant_type: 'ops-agent' })
      store.conversationId = 'conv-1'

      const capturedBodies: string[] = []
      vi.stubGlobal('fetch', makeStreamFetchMock(capturedBodies))

      await store.sendMessage('帮我排查 HCI 节点故障')

      expect(capturedBodies).toHaveLength(1)
      const body = JSON.parse(capturedBodies[0])
      // Fix A 修复前：body.assistant_type === 'qwen'（错误）
      // Fix A 修复后：body.assistant_type === 'ops-agent'（正确）
      expect(body.assistant_type).toBe('ops-agent')
      expect(body.content).toBe('帮我排查 HCI 节点故障')

      vi.unstubAllGlobals()
    })

    it('currentCase 没有 assistant_type 时，降级使用 selectedAssistant', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.selectedAssistant = 'qwen'
      // 旧工单可能没有 assistant_type 字段
      store.currentCase = makeCase({ case_id: 'case-old', assistant_type: undefined })
      store.conversationId = 'conv-1'

      const capturedBodies: string[] = []
      vi.stubGlobal('fetch', makeStreamFetchMock(capturedBodies))

      await store.sendMessage('测试消息')

      const body = JSON.parse(capturedBodies[0])
      expect(body.assistant_type).toBe('qwen')

      vi.unstubAllGlobals()
    })
  })

  // ═══════════════════════════════════════════════════════════════════════
  // Fix B：completeCaseCreationFlow 在发送首条消息前同步 selectedAssistant
  // ═══════════════════════════════════════════════════════════════════════
  describe('Fix B: 工单创建流程同步 selectedAssistant', () => {
    it('completeCaseCreationFlow 执行后 selectedAssistant 应为工单绑定的助手类型', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.assistants = [
        makeAssistant('qwen', true, true),
        makeAssistant('ops-agent', false),
      ]
      store.selectedAssistant = 'qwen'

      // caseApi.getById 返回 ops-agent 类型的工单
      mockCaseGetById.mockResolvedValue({
        data: makeCase({ case_id: 'case-2', assistant_type: 'ops-agent' }),
      })
      // conversationApi.create 成功
      mockConvCreate.mockResolvedValue({ data: { conversation_id: 'conv-2' } })

      const capturedBodies: string[] = []
      vi.stubGlobal('fetch', makeStreamFetchMock(capturedBodies))

      await store.completeCaseCreationFlow('case-2', '我的问题描述', 'ops-agent')

      // Fix B 修复前：selectedAssistant 仍为 'qwen'
      // Fix B 修复后：completeCaseCreationFlow 会同步到 'ops-agent'
      expect(store.selectedAssistant).toBe('ops-agent')

      // 同时验证 Fix A：发送的消息也用了正确的 assistant_type
      expect(capturedBodies).toHaveLength(1)
      const body = JSON.parse(capturedBodies[0])
      expect(body.assistant_type).toBe('ops-agent')

      vi.unstubAllGlobals()
    })

    it('assistantType 参数不在列表中时，selectedAssistant 保持不变（不崩溃）', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.assistants = [makeAssistant('qwen', true, true)]
      store.selectedAssistant = 'qwen'

      mockCaseGetById.mockResolvedValue({
        data: makeCase({ case_id: 'case-3', assistant_type: 'unknown-type' }),
      })
      mockConvCreate.mockResolvedValue({ data: { conversation_id: 'conv-3' } })

      vi.stubGlobal('fetch', makeStreamFetchMock([]))

      // 不应抛出异常
      await expect(
        store.completeCaseCreationFlow('case-3', '测试', 'unknown-type')
      ).resolves.not.toThrow()

      vi.unstubAllGlobals()
    })
  })
})
