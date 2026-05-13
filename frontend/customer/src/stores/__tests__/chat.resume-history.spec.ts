/**
 * chat store — loadConversationHistory 场景A/B 自动 resume 单元测试
 *
 * 验证 2026-05-13 修复：
 * - 场景A：ops-agent 生成中途刷新，最后一条是普通 user 消息且无 AI 回复 → 自动 resume
 * - 场景B1：interactive_request 无 interactive_response → 恢复 pendingInteractive，不触发 resume
 * - 场景B2：有 interactive_response 但无后续 AI 回复 → 自动 resume
 * - 防重入：场景A已调度时，场景B不再重复调度 resumeOpsAgentStream
 * - resumeOpsAgentStream 自身 isStreaming guard 防止并发调用
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
    mockConvCreate: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-resume-1' } }),
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
                case_id: 'c-resume-1',
                assistant_type: 'ops-agent',
                status: 'in_progress',
                client_id: 'test',
                title: 'Resume Test',
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

// ── /resume-stream 返回空流（[DONE]）的 fetch mock ─────────────────────────
function makeDoneResumeFetch() {
    const encoder = new TextEncoder()
    return vi.fn().mockResolvedValue({
        ok: true,
        body: {
            getReader: () => {
                let done = false
                return {
                    read: vi.fn().mockImplementation(async () => {
                        if (done) return { done: true, value: undefined }
                        done = true
                        return { done: false, value: encoder.encode('data: [DONE]\n\n') }
                    }),
                    releaseLock: vi.fn(),
                }
            },
        },
    })
}

// ── 对话+消息历史 mock 辅助 ────────────────────────────────────────────────

/** 构造一条 ops-agent 对话的 GET /conversations/case/:id 响应 */
function makeConvResponse() {
    return {
        data: [{
            conversation_id: 'conv-resume-1',
            assistant_type: 'ops-agent',
        }],
    }
}

/** 构造消息列表（MessageResponse 格式） */
function makeMessages(msgs: Array<{ role: string; metadata?: Record<string, unknown>; content?: string }>) {
    return msgs.map((m, i) => ({
        message_id: `msg-${i}`,
        role: m.role,
        content: m.content ?? (m.role === 'user' ? '用户消息' : 'AI 回复'),
        metadata: m.metadata ?? null,
        created_at: new Date().toISOString(),
    }))
}

// ── 助手列表 mock（ops-agent）─────────────────────────────────────────────
const OPS_AGENT = { id: 'ops-agent', type: 'ops-agent', name: 'Ops Agent', description: '' }
const IR_EVENT = {
    requestId: 'req-001',
    acpSessionId: 'sess-001',
    kind: 'info_request',
    title: '确认信息',
    prompt: '哪台主机？',
    options: [{ optionId: '1', name: '主机A' }],
    customInput: false,
    metadata: {},
}

// ── beforeEach ────────────────────────────────────────────────────────────
beforeEach(() => {
    setActivePinia(createPinia())
    vi.resetAllMocks()
    mockAssistantList.mockResolvedValue({ data: [OPS_AGENT] })
    mockListByClient.mockResolvedValue({ data: [] })
})

// ══════════════════════════════════════════════════════════════════════════
// 场景A：ops-agent 生成中途刷新，最后一条是普通 user 消息，无 AI 回复
// ══════════════════════════════════════════════════════════════════════════
describe('场景A：普通 user 消息结尾，无 AI 回复 → 自动 resume', () => {
    it('调用一次 /conversations/case/:id 后触发 resumeOpsAgentStream（fetch /resume-stream 被调用一次）', async () => {
        // 历史：user 发了一条普通消息，后面没有 AI 回复
        mockApiGet.mockImplementation((url: string) => {
            if (url.includes('/conversations/case/')) return Promise.resolve(makeConvResponse())
            return Promise.resolve({ data: [] })
        })
        mockConvGetMessages.mockResolvedValue({
            data: makeMessages([
                { role: 'user', content: '帮我排查问题' },
            ]),
        })

        const mockFetch = makeDoneResumeFetch()
        vi.stubGlobal('fetch', mockFetch)

        const { useChatStore } = await import('../chat')
        const store = useChatStore()
        await store.initialize()

        // 模拟已有 currentCase + conversationId（恢复工单场景）
        const caseItem = {
            case_id: 'c-resume-1',
            assistant_type: 'ops-agent',
            status: 'in_progress',
            client_id: 'test',
            title: 'Resume Test',
            description: null,
            created_at: '',
            updated_at: '',
            closed_at: null,
            trace_id: null,
        } as any

        await store.switchToCase(caseItem)

        // nextTick 后才触发，需要等待
        await new Promise(resolve => setTimeout(resolve, 50))

        // /resume-stream 应被调用恰好一次（防重入保证不多于一次）
        const resumeCalls = mockFetch.mock.calls.filter((args: unknown[]) =>
            typeof args[0] === 'string' && args[0].includes('/resume-stream')
        )
        expect(resumeCalls).toHaveLength(1)
    })
})

// ══════════════════════════════════════════════════════════════════════════
// 场景B1：interactive_request 无 interactive_response → 恢复 pendingInteractive
// ══════════════════════════════════════════════════════════════════════════
describe('场景B1：interactive_request 后无 interactive_response → 恢复 pendingInteractive，不触发 resume', () => {
    it('pendingInteractive 被恢复，fetch /resume-stream 不被调用', async () => {
        mockApiGet.mockImplementation((url: string) => {
            if (url.includes('/conversations/case/')) return Promise.resolve(makeConvResponse())
            return Promise.resolve({ data: [] })
        })
        mockConvGetMessages.mockResolvedValue({
            data: makeMessages([
                { role: 'user', content: '帮我排查' },
                {
                    role: 'assistant',
                    content: '请确认信息',
                    metadata: { kind: 'interactive_request', event: IR_EVENT },
                },
                // 没有 interactive_response
            ]),
        })

        const mockFetch = makeDoneResumeFetch()
        vi.stubGlobal('fetch', mockFetch)

        const { useChatStore } = await import('../chat')
        const store = useChatStore()
        await store.initialize()

        const caseItem = {
            case_id: 'c-resume-1',
            assistant_type: 'ops-agent',
            status: 'in_progress',
            client_id: 'test',
            title: 'Resume Test',
            description: null,
            created_at: '',
            updated_at: '',
            closed_at: null,
            trace_id: null,
        } as any

        await store.switchToCase(caseItem)
        await new Promise(resolve => setTimeout(resolve, 50))

        // pendingInteractive 应被恢复
        expect(store.pendingInteractive).not.toBeNull()
        expect((store.pendingInteractive as any)?.requestId).toBe('req-001')

        // resume-stream 不应被调用
        const resumeCalls = mockFetch.mock.calls.filter((args: unknown[]) =>
            typeof args[0] === 'string' && args[0].includes('/resume-stream')
        )
        expect(resumeCalls).toHaveLength(0)
    })
})

// ══════════════════════════════════════════════════════════════════════════
// 场景B2：有 interactive_response 但无后续 AI 回复 → 自动 resume
// ══════════════════════════════════════════════════════════════════════════
describe('场景B2：interactive_response 后无 AI 回复 → 自动 resume', () => {
    it('fetch /resume-stream 被调用一次', async () => {
        mockApiGet.mockImplementation((url: string) => {
            if (url.includes('/conversations/case/')) return Promise.resolve(makeConvResponse())
            return Promise.resolve({ data: [] })
        })
        mockConvGetMessages.mockResolvedValue({
            data: makeMessages([
                { role: 'user', content: '帮我排查' },
                {
                    role: 'assistant',
                    content: '请确认信息',
                    metadata: { kind: 'interactive_request', event: IR_EVENT },
                },
                {
                    role: 'user',
                    content: '主机A',
                    metadata: { kind: 'interactive_response', optionId: '1', optionName: '主机A' },
                },
                // 没有后续 AI 文字回复
            ]),
        })

        const mockFetch = makeDoneResumeFetch()
        vi.stubGlobal('fetch', mockFetch)

        const { useChatStore } = await import('../chat')
        const store = useChatStore()
        await store.initialize()

        const caseItem = {
            case_id: 'c-resume-1',
            assistant_type: 'ops-agent',
            status: 'in_progress',
            client_id: 'test',
            title: 'Resume Test',
            description: null,
            created_at: '',
            updated_at: '',
            closed_at: null,
            trace_id: null,
        } as any

        await store.switchToCase(caseItem)
        await new Promise(resolve => setTimeout(resolve, 50))

        const resumeCalls = mockFetch.mock.calls.filter((args: unknown[]) =>
            typeof args[0] === 'string' && args[0].includes('/resume-stream')
        )
        expect(resumeCalls).toHaveLength(1)
    })
})

// ══════════════════════════════════════════════════════════════════════════
// 防重入：场景A已调度时，场景B不重复调度
// ══════════════════════════════════════════════════════════════════════════
describe('防重入：场景A和场景B同时满足时，resume 只触发一次', () => {
    it('最后一条是普通 user 消息且历史中有 interactive_response 无续写 → fetch 只被调用一次', async () => {
        // 构造：history 里有 interactive_request + interactive_response，
        // 但最后一条又是一条新的普通 user 消息（用户刷新前又发了一条消息）
        mockApiGet.mockImplementation((url: string) => {
            if (url.includes('/conversations/case/')) return Promise.resolve(makeConvResponse())
            return Promise.resolve({ data: [] })
        })
        mockConvGetMessages.mockResolvedValue({
            data: makeMessages([
                { role: 'user', content: '帮我排查' },
                {
                    role: 'assistant',
                    content: '请确认信息',
                    metadata: { kind: 'interactive_request', event: IR_EVENT },
                },
                {
                    role: 'user',
                    content: '主机A',
                    metadata: { kind: 'interactive_response', optionId: '1', optionName: '主机A' },
                },
                // 又发了一条普通消息（场景A条件满足）
                { role: 'user', content: '还有什么需要确认的？' },
            ]),
        })

        const mockFetch = makeDoneResumeFetch()
        vi.stubGlobal('fetch', mockFetch)

        const { useChatStore } = await import('../chat')
        const store = useChatStore()
        await store.initialize()

        const caseItem = {
            case_id: 'c-resume-1',
            assistant_type: 'ops-agent',
            status: 'in_progress',
            client_id: 'test',
            title: 'Resume Test',
            description: null,
            created_at: '',
            updated_at: '',
            closed_at: null,
            trace_id: null,
        } as any

        await store.switchToCase(caseItem)
        await new Promise(resolve => setTimeout(resolve, 100))

        // 尽管场景A和场景B都满足条件，resume 只应被调用一次
        const resumeCalls = mockFetch.mock.calls.filter((args: unknown[]) =>
            typeof args[0] === 'string' && args[0].includes('/resume-stream')
        )
        expect(resumeCalls).toHaveLength(1)
    })
})

// ══════════════════════════════════════════════════════════════════════════
// resumeOpsAgentStream isStreaming guard
// ══════════════════════════════════════════════════════════════════════════
describe('resumeOpsAgentStream isStreaming guard：并发调用只建立一个连接', () => {
    it('连续两次调用 resumeOpsAgentStream，fetch /resume-stream 只被调用一次', async () => {
        mockApiGet.mockImplementation((url: string) => {
            if (url.includes('/conversations/case/')) return Promise.resolve(makeConvResponse())
            return Promise.resolve({ data: [] })
        })
        mockConvGetMessages.mockResolvedValue({ data: [] })

        // fetch mock：故意慢返回，模拟第一个请求还在 pending
        const encoder = new TextEncoder()
        let resolveFirst: (() => void) | null = null as (() => void) | null
        const slowFetch = vi.fn().mockImplementation((url: string) => {
            if (!url.includes('/resume-stream')) return Promise.resolve({ data: [] })
            return new Promise<object>((resolve) => {
                resolveFirst = () => resolve({
                    ok: true,
                    body: {
                        getReader: () => {
                            let done = false
                            return {
                                read: vi.fn().mockImplementation(async () => {
                                    if (done) return { done: true, value: undefined }
                                    done = true
                                    return { done: false, value: encoder.encode('data: [DONE]\n\n') }
                                }),
                                releaseLock: vi.fn(),
                            }
                        },
                    },
                })
            })
        })
        vi.stubGlobal('fetch', slowFetch)

        const { useChatStore } = await import('../chat')
        const store = useChatStore()
        await store.initialize()

        store.currentCase = { case_id: 'c-resume-1', assistant_type: 'ops-agent' } as any
        store.conversationId = 'conv-resume-1'

        // 并发调用两次
        const p1 = store.resumeOpsAgentStream()
        const p2 = store.resumeOpsAgentStream()

        // 让第一个完成
        resolveFirst?.()
        await Promise.all([p1, p2])

        const resumeCalls = slowFetch.mock.calls.filter((args: unknown[]) =>
            typeof args[0] === 'string' && (args[0] as string).includes('/resume-stream')
        )
        // isStreaming guard 确保只建立一个连接
        expect(resumeCalls).toHaveLength(1)
    })
})
