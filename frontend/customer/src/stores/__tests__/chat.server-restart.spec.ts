/**
 * chat store — 服务端滚动发布收盘（server_restart）的行为测试
 *
 * 背景：发布时服务端会在流尾发 `event: server_restart` 并主动结束连接，前端负责
 * 退避重连。这里锁定两条不能互相抵消的约束：
 *
 * 1. 收到 server_restart 时**不能**把 running 的工具调用标成 failed —— 上游其实还活着，
 *    只是换了 Pod；标 failed 会让用户看到"执行流已结束"而任务仍在继续。
 * 2. 没收到 server_restart 的正常收尾（[DONE]）**仍然要**标 failed —— 否则真丢了结果
 *    反而没有任何提示（修复误报不能把真失败一起吞掉）。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { mockApiGet, mockListByClient, mockAssistantList, mockConvCreate, mockConvGetMessages } =
  vi.hoisted(() => ({
    mockApiGet: vi.fn().mockResolvedValue({ data: [] }),
    mockListByClient: vi.fn().mockResolvedValue({ data: [] }),
    mockAssistantList: vi.fn(),
    mockConvCreate: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-restart-1' } }),
    mockConvGetMessages: vi.fn().mockResolvedValue({ data: [] }),
  }))

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
    create: vi.fn(),
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

const OPS_AGENT = { id: 'ops-agent', type: 'ops-agent', name: 'Ops Agent', description: '' }

/** 把若干行 SSE 文本一次性推给 reader，第二次 read 即结束。 */
function makeStreamFetch(frames: string[]) {
  const encoder = new TextEncoder()
  const payload = encoder.encode(frames.join(''))
  let sent = false
  return vi.fn().mockImplementation(() => {
    return Promise.resolve({
      ok: true,
      body: {
        getReader: () => ({
          read: vi.fn().mockImplementation(async () => {
            if (sent) return { done: true, value: undefined }
            sent = true
            return { done: false, value: payload }
          }),
          releaseLock: vi.fn(),
        }),
      },
    })
  })
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.resetAllMocks()
  mockAssistantList.mockResolvedValue({ data: [OPS_AGENT] })
  mockListByClient.mockResolvedValue({ data: [] })
})

afterEach(() => {
  vi.useRealTimers()
})

async function prepareStore(frames: string[]) {
  const mockFetch = makeStreamFetch(frames)
  vi.stubGlobal('fetch', mockFetch)

  const { useChatStore } = await import('../chat')
  const store = useChatStore()
  await store.initialize()
  store.conversationId = 'conv-restart-1'

  // 只假化 setTimeout：让 finally 里的 2s 退避重连不真的排进事件循环，
  // 避免测试结束后还有一次 fetch 打空。主流程本身不依赖 setTimeout。
  vi.useFakeTimers({ toFake: ['setTimeout'] })
  return { store, mockFetch }
}

function findToolMessage(store: any) {
  return store.messages.find((m: any) => m.metadata?.kind === 'tool_call')
}

const RUNNING_TOOL_CALL = 'event: tool_call\ndata: {"exec_id":"e1","tool":"acli","status":"running"}\n\n'
const SERVER_RESTART = 'event: server_restart\ndata: {}\n\n'

describe('续写流遇到 server_restart', () => {
  it('running 的工具调用保持 running，不被误标 failed', async () => {
    const { store } = await prepareStore([RUNNING_TOOL_CALL, SERVER_RESTART])

    await store.resumeOpsAgentStream()

    const toolMessage = findToolMessage(store)
    expect(toolMessage).toBeTruthy()
    expect(toolMessage.metadata.event.status).toBe('running')
    expect(store.isStreaming).toBe(false)
  })

  it('退避 2s 后自动重连 /resume-stream（换到新副本）', async () => {
    const { store, mockFetch } = await prepareStore([RUNNING_TOOL_CALL, SERVER_RESTART])

    await store.resumeOpsAgentStream()
    const before = mockFetch.mock.calls.filter((a: any[]) => String(a[0]).includes('/resume-stream'))
    expect(before).toHaveLength(1)

    await vi.advanceTimersByTimeAsync(2000)

    const after = mockFetch.mock.calls.filter((a: any[]) => String(a[0]).includes('/resume-stream'))
    expect(after).toHaveLength(2)
  })
})

describe('正常收尾（无 server_restart）', () => {
  it('仍把 running 的工具调用标成 failed 并提示结果缺失', async () => {
    const { store } = await prepareStore([RUNNING_TOOL_CALL, 'data: [DONE]\n\n'])

    await store.resumeOpsAgentStream()

    const toolMessage = findToolMessage(store)
    expect(toolMessage).toBeTruthy()
    expect(toolMessage.metadata.event.status).toBe('failed')
    expect(String(toolMessage.metadata.event.error)).toContain('执行流已结束')
  })
})