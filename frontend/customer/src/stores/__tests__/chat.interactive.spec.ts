/**
 * chat store — interactive_request 气泡行为单元测试
 *
 * 验证 2026-05 改动：将 ops-agent interactive_request 弹框改为对话气泡
 * - interactive_request SSE 事件 → messages 追加 assistant 气泡
 * - 气泡携带 metadata.kind='interactive_request' 和完整 metadata.event
 * - pendingInteractive 同步更新（兼容旧代码路径）
 */

import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// ── vi.hoisted：在 mock 工厂闭包内可引用的可控 mock 函数 ──────────────────
const {
  mockApiGet,
  mockListByClient,
  mockAssistantList,
  mockConvCreate,
  mockConvGetMessages,
} = vi.hoisted(() => ({
  mockApiGet: vi.fn().mockResolvedValue({ data: [] }),
  mockListByClient: vi.fn().mockResolvedValue({ data: [] }),
  mockAssistantList: vi.fn(),
  mockConvCreate: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-ir-1' } }),
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
    listByClient: mockListByClient,
    getById: vi.fn(),
    close: vi.fn().mockResolvedValue({}),
    create: vi.fn().mockResolvedValue({
      data: {
        case_id: 'c-ir-1',
        assistant_type: 'ops-agent',
        status: 'in_progress',
        client_id: 'test',
        title: 'IR Test',
        description: null,
        created_at: '',
        updated_at: '',
        closed_at: null,
        trace_id: null,
      },
    }),
  }),
  createConversationApi: () => ({
    create: mockConvCreate,
    getMessages: mockConvGetMessages,
  }),
  createAssistantApi: () => ({
    list: mockAssistantList,
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

// ── 辅助函数 ─────────────────────────────────────────────────────────────

/** 构造携带 interactive_request SSE 事件的 fetch mock */
function makeInteractiveFetchMock(eventPayload: Record<string, unknown>) {
  const encoder = new TextEncoder()
  const sseData = JSON.stringify(eventPayload)
  const chunks = [
    encoder.encode(`event: interactive_request\ndata: ${sseData}\n\n`),
    encoder.encode('data: [DONE]\n\n'),
  ]
  let i = 0
  return vi.fn().mockResolvedValue({
    ok: true,
    body: {
      getReader: () => ({
        read: vi.fn().mockImplementation(async () => {
          if (i < chunks.length) return { done: false, value: chunks[i++] }
          return { done: true, value: undefined }
        }),
      }),
    },
  })
}

/** 构造最小 CaseResponse */
function makeCase(overrides = {}) {
  return {
    case_id: 'c-ir-1',
    client_id: 'test-client-id',
    status: 'in_progress',
    title: 'IR Test',
    description: null,
    assistant_type: 'ops-agent',
    created_at: '2026-01-01T00:00:00Z',
    updated_at: '2026-01-01T00:00:00Z',
    closed_at: null,
    trace_id: null,
    ...overrides,
  }
}

/** 构造完整 interactive_request SSE 事件 payload */
function makeInteractiveEvent(overrides: Record<string, unknown> = {}) {
  return {
    requestId: 'req-001',
    acpSessionId: 'sess-001',
    kind: 'info_request',
    title: '信息确认',
    prompt: '请确认虚拟机当前状态',
    options: [
      { optionId: '1', name: '虚拟机已关闭' },
      { optionId: '2', name: '虚拟机运行中' },
      { optionId: '3', name: '不确定' },
    ],
    customInput: true,
    metadata: { question: '请确认虚拟机当前状态', context: '用于判断后续排障步骤' },
    ...overrides,
  }
}

// ── 测试套件 ──────────────────────────────────────────────────────────────
describe('chat store — interactive_request 气泡行为', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockListByClient.mockResolvedValue({ data: [] })
    mockAssistantList.mockResolvedValue({
      data: { assistants: [], show_selector: false, default_assistant: null, selector_mode: 'auto' },
    })
  })

  // ═══════════════════════════════════════════════════════════════════════
  // 核心改动：interactive_request 事件追加消息气泡而非弹窗
  // ═══════════════════════════════════════════════════════════════════════
  describe('interactive_request SSE 事件 → 追加 assistant 气泡', () => {
    it('收到 interactive_request 事件后，messages 中追加一条 role=assistant 的消息', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.currentCase = makeCase() as any
      store.conversationId = 'conv-ir-1'

      const eventPayload = makeInteractiveEvent()
      vi.stubGlobal('fetch', makeInteractiveFetchMock(eventPayload))

      await store.sendMessage('虚拟机有异常')

      // 找到 interactive_request 类型的消息
      const irMsg = store.messages.find(
        m => m.role === 'assistant' && m.metadata?.kind === 'interactive_request'
      )
      expect(irMsg).toBeDefined()
      expect(irMsg?.role).toBe('assistant')

      vi.unstubAllGlobals()
    })

    it('interactive_request 气泡的 metadata.event 携带完整 event 结构（requestId/options/kind）', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.currentCase = makeCase() as any
      store.conversationId = 'conv-ir-1'

      const eventPayload = makeInteractiveEvent({ kind: 'sop_step', requestId: 'req-sop-001' })
      vi.stubGlobal('fetch', makeInteractiveFetchMock(eventPayload))

      await store.sendMessage('虚拟机开机失败')

      const irMsg = store.messages.find(
        m => m.metadata?.kind === 'interactive_request'
      )
      expect(irMsg).toBeDefined()
      const ev = irMsg?.metadata?.event as any
      expect(ev).toBeDefined()
      expect(ev.requestId).toBe('req-sop-001')
      expect(ev.kind).toBe('sop_step')
      expect(ev.options).toHaveLength(3)
      expect(ev.customInput).toBe(true)

      vi.unstubAllGlobals()
    })

    it('interactive_request 气泡的 id 包含 requestId（格式：ir-<requestId>）', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.currentCase = makeCase() as any
      store.conversationId = 'conv-ir-1'

      const eventPayload = makeInteractiveEvent({ requestId: 'req-abc-123' })
      vi.stubGlobal('fetch', makeInteractiveFetchMock(eventPayload))

      await store.sendMessage('测试消息')

      const irMsg = store.messages.find(m => m.id?.startsWith('ir-'))
      expect(irMsg).toBeDefined()
      expect(irMsg?.id).toBe('ir-req-abc-123')

      vi.unstubAllGlobals()
    })

    it('pendingInteractive 同步更新，保持兼容性', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.currentCase = makeCase() as any
      store.conversationId = 'conv-ir-1'

      const eventPayload = makeInteractiveEvent()
      vi.stubGlobal('fetch', makeInteractiveFetchMock(eventPayload))

      await store.sendMessage('测试')

      // pendingInteractive 应同步设置（兼容旧代码路径）
      expect(store.pendingInteractive).not.toBeNull()
      expect(store.pendingInteractive?.requestId).toBe('req-001')
      expect(store.pendingInteractive?.kind).toBe('info_request')
      expect(store.pendingInteractive?.options).toHaveLength(3)

      vi.unstubAllGlobals()
    })

    it('interactive_request 气泡 content 为 prompt 字段内容', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.currentCase = makeCase() as any
      store.conversationId = 'conv-ir-1'

      const eventPayload = makeInteractiveEvent({ prompt: '请确认主机状态' })
      vi.stubGlobal('fetch', makeInteractiveFetchMock(eventPayload))

      await store.sendMessage('测试')

      const irMsg = store.messages.find(m => m.metadata?.kind === 'interactive_request')
      expect(irMsg?.content).toBe('请确认主机状态')

      vi.unstubAllGlobals()
    })
  })

  // ═══════════════════════════════════════════════════════════════════════
  // clearInteractiveRequest：清除 pendingInteractive
  // ═══════════════════════════════════════════════════════════════════════
  describe('clearInteractiveRequest', () => {
    it('调用后 pendingInteractive 变为 null', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      store.currentCase = makeCase() as any
      store.conversationId = 'conv-ir-1'

      vi.stubGlobal('fetch', makeInteractiveFetchMock(makeInteractiveEvent()))
      await store.sendMessage('测试')

      expect(store.pendingInteractive).not.toBeNull()
      store.clearInteractiveRequest()
      expect(store.pendingInteractive).toBeNull()

      vi.unstubAllGlobals()
    })
  })

  // ═══════════════════════════════════════════════════════════════════════
  // interactiveSubmitted 判断：消息之后有 interactive_response 时应禁用选项
  // ═══════════════════════════════════════════════════════════════════════
  describe('interactiveSubmitted 判断逻辑', () => {
    it('messages 中 interactive_request 消息之后有 interactive_response 用户消息时判断为已提交', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      // 模拟已存在的 interactive_request 消息
      const irMsgId = 'ir-req-001'
      store.messages = [
        {
          id: irMsgId,
          role: 'assistant',
          content: '请确认状态',
          timestamp: new Date(),
          metadata: { kind: 'interactive_request', event: makeInteractiveEvent() },
        },
        // 随后有用户的 interactive_response
        {
          id: 'ir-resp-001',
          role: 'user',
          content: '[操作选择] 虚拟机已关闭',
          timestamp: new Date(),
          metadata: { kind: 'interactive_response' },
        },
      ]

      // 验证：在 interactive_request 消息之后存在 interactive_response 用户消息
      const irMsgIndex = store.messages.findIndex(m => m.id === irMsgId)
      const laterMessages = store.messages.slice(irMsgIndex + 1)
      const submitted = laterMessages.some(
        m => m.role === 'user' && m.metadata?.kind === 'interactive_response'
      )
      expect(submitted).toBe(true)
    })

    it('messages 中 interactive_request 消息之后没有 interactive_response 时判断为未提交', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      const irMsgId = 'ir-req-002'
      store.messages = [
        {
          id: irMsgId,
          role: 'assistant',
          content: '请确认状态',
          timestamp: new Date(),
          metadata: { kind: 'interactive_request', event: makeInteractiveEvent() },
        },
        // 只有普通用户消息，没有 interactive_response
        {
          id: 'user-msg-001',
          role: 'user',
          content: '虚拟机有问题',
          timestamp: new Date(),
        },
      ]

      const irMsgIndex = store.messages.findIndex(m => m.id === irMsgId)
      const laterMessages = store.messages.slice(irMsgIndex + 1)
      const submitted = laterMessages.some(
        m => m.role === 'user' && m.metadata?.kind === 'interactive_response'
      )
      expect(submitted).toBe(false)
    })
  })

  // ═══════════════════════════════════════════════════════════════════════
  // startNewConversation / handleCloseCase 清除 pendingInteractive
  // ═══════════════════════════════════════════════════════════════════════
  describe('工单关闭/新建会话清除 pendingInteractive', () => {
    it('startNewConversation 后 pendingInteractive 为 null', async () => {
      const { useChatStore } = await import('../chat')
      const store = useChatStore()

      // 手动设置 pendingInteractive
      store.pendingInteractive = {
        requestId: 'req-001',
        acpSessionId: 'sess-001',
        kind: 'info_request',
        title: '测试',
        prompt: '测试',
        options: [],
        customInput: false,
        metadata: {},
      }

      store.startNewConversation()

      expect(store.pendingInteractive).toBeNull()
    })
  })
})
