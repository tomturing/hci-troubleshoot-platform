/**
 * chat store 本地日志补采与前端异常上报单元测试
 *
 * 覆盖工单 Q2026092010235 的可观测性补齐：
 * - uploadBridgeLogs 读取本地 JSONL 文件并 POST 到 /bridge-logs/upload
 * - 上传入口关闭（404）时给出可读提示
 * - reportClientError 把浏览器异常转换为 customer-ui 日志条目回采
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { mockPost } = vi.hoisted(() => ({
  mockPost: vi.fn().mockResolvedValue({ data: {} }),
}))

vi.mock('@hci/shared', () => ({
  createApiClient: () => ({
    get: vi.fn().mockResolvedValue({ data: [] }),
    post: mockPost,
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
  checkBridgeRunning: vi.fn().mockResolvedValue({ running: false }),
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue({ running: false }),
  createBridgeSocket: vi.fn(),
  buildConnectMessage: vi.fn(),
  buildInputMessage: vi.fn(),
  buildDisconnectMessage: vi.fn(),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

/** 构造内存 File 对象（jsdom/happy-dom 下 File.text() 可用） */
function makeLogFile(name: string, content: string): File {
  return new File([content], name, { type: 'text/plain' })
}

describe('chat store - 本地日志补采与异常上报', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockPost.mockResolvedValue({ data: { ok: true, accepted: 3, duplicates: 1, skipped: 0, invalid: 2 } })
    vi.useFakeTimers()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
  })

  it('uploadBridgeLogs 将本地 JSONL 文件内容 POST 到 /bridge-logs/upload', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    const file = makeLogFile('bridge-20260920-abcd1234.log',
      '{"event":"exec.done","message":"done","case_id":"Q001"}\n')
    const result = await store.uploadBridgeLogs([file])

    expect(result.ok).toBe(true)
    const [path, payload] = mockPost.mock.calls[0]
    expect(path).toBe('/bridge-logs/upload')
    expect(payload.files).toHaveLength(1)
    expect(payload.files[0].name).toBe('bridge-20260920-abcd1234.log')
    expect(payload.files[0].content).toContain('exec.done')
    expect(result.message).toContain('新增 3')
  })

  it('uploadBridgeLogs 在入口关闭（404）时给出可读提示', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    mockPost.mockRejectedValue({ response: { status: 404 } })

    const result = await store.uploadBridgeLogs([makeLogFile('bridge.log', '{"event":"x"}')])

    expect(result.ok).toBe(false)
    expect(result.message).toContain('上传入口已关闭')
  })

  it('reportClientError 将浏览器异常转为 customer-ui 日志条目回采', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    // 绑定当前工单：无工单上下文的日志会被有意丢弃（避免噪音）
    store.currentCase = { case_id: 'Q001' } as any

    store.reportClientError('window.onerror', new Error('boom'))

    // 回采缓冲按 500ms 聚合后上报
    await vi.advanceTimersByTimeAsync(600)
    await vi.runOnlyPendingTimersAsync()

    const bridgeCalls = mockPost.mock.calls.filter((c) => c[0] === '/bridge-logs')
    expect(bridgeCalls.length).toBeGreaterThanOrEqual(1)
    const payload = bridgeCalls[bridgeCalls.length - 1][1]
    const entry = payload.logs.find((l: Record<string, unknown>) => l.event === 'client.error')
    expect(entry).toBeDefined()
    expect(entry['service.name']).toBe('customer-ui')
    expect(entry.message).toContain('boom')
    // 无工单上下文且不携带 fallback 时不得落库噪音（此处应被计入 fallback/dropped 分支）
    expect(entry.level).toBe('ERROR')
  })
})
