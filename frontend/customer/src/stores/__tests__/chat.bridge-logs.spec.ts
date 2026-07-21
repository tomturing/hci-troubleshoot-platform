/**
 * chat store bridge-logs 回采行为单元测试
 *
 * 覆盖回采链路修复（OBS-TERMINAL-BRIDGE-001 / 工单 Q2026072055042）：
 * - forwardBridgeLog 丢弃无 case_id 的条目
 * - flushBridgeLogs POST 到 /bridge-logs 端点
 * - flushBridgeLogs 失败时指数退避重试
 * - flushBridgeLogs 超过最大重试后丢弃 + 暴露 __bridgeLogStats 可观测信号
 * - flushBridgeLogs 成功时暴露 __bridgeLogStats 成功计数
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

// window.__bridgeLogStats 类型化访问（对齐 chat.ts 的 BridgeLogStats 定义）
interface BridgeLogStats {
  successes?: number
  ingested?: number
  lastSuccessAt?: string
  failures?: number
  dropped?: number
  lastError?: string
  lastErrorAt?: string
}
function getStats(): BridgeLogStats | undefined {
  return (window as Window & { __bridgeLogStats?: BridgeLogStats }).__bridgeLogStats
}
function clearStats(): void {
  delete (window as Window & { __bridgeLogStats?: BridgeLogStats }).__bridgeLogStats
}

// ── vi.hoisted：可控的 apiClient.post mock ──────────────────────────────
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

describe('chat store - bridge-logs 回采', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    mockPost.mockResolvedValue({ data: {} })
    vi.useFakeTimers()
    // 清理 window.__bridgeLogStats
    clearStats()
  })

  afterEach(() => {
    vi.useRealTimers()
    vi.restoreAllMocks()
    clearStats()
  })

  it('forwardBridgeLog 丢弃无 case_id 的条目（不触发上报）', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    // forwardBridgeLog 是 store 暴露的方法
    store.forwardBridgeLog({ level: 'INFO', event: 'exec.start', message: 'no case' })

    // 无 case_id 不应触发 POST；推进 fake timer 确保 flush 不发生
    await vi.advanceTimersByTimeAsync(1000)
    expect(mockPost).not.toHaveBeenCalled()
  })

  it('forwardBridgeLog + flushBridgeLogs 将带 case_id 的条目 POST 到 /bridge-logs', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.forwardBridgeLog({ case_id: 'Q001', level: 'INFO', event: 'ssh.connected', message: 'connected' })

    // 500ms 聚合后触发 flush
    await vi.advanceTimersByTimeAsync(600)
    await vi.runOnlyPendingTimersAsync()

    // 仅校验打到 /bridge-logs 的调用（排除 store init 等其他 post）
    const bridgeCalls = mockPost.mock.calls.filter((c) => c[0] === '/bridge-logs')
    expect(bridgeCalls).toHaveLength(1)
    const [, payload] = bridgeCalls[0]
    expect(payload.logs).toHaveLength(1)
    expect(payload.logs[0].case_id).toBe('Q001')
  })

  it('flushBridgeLogs 成功时暴露 __bridgeLogStats 成功计数', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.forwardBridgeLog({ case_id: 'Q001', level: 'INFO', event: 'exec.start', message: 'ok' })
    await vi.advanceTimersByTimeAsync(600)
    await vi.runOnlyPendingTimersAsync()

    const stats = getStats()
    expect(stats).toBeDefined()
    expect(stats?.successes).toBe(1)
    expect(stats?.ingested).toBe(1)
    expect(stats?.lastSuccessAt).toBeDefined()
  })

  it('flushBridgeLogs 失败时指数退避重试（最多 5 次）', async () => {
    mockPost.mockRejectedValue(new Error('network error'))
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.forwardBridgeLog({ case_id: 'Q001', level: 'INFO', event: 'exec.start', message: 'ok' })

    // 推进时间让多次 flush+重试发生（fake timer 会压缩多次重试到一次 advance）
    await vi.advanceTimersByTimeAsync(600)
    await vi.runOnlyPendingTimersAsync()

    const stats = getStats()
    // 失败计数 > 0 且 lastError 记录网络错误（证明失败路径被触发并暴露可观测信号）
    expect(stats?.failures).toBeGreaterThan(0)
    expect(stats?.lastError).toContain('network error')
    expect(stats?.lastErrorAt).toBeDefined()
    // dropped 在未达最大重试时应为 0（仍在重试，未丢弃）
    expect(stats?.dropped).toBe(0)
  })

  it('flushBridgeLogs 超过最大重试次数后丢弃日志并累计 dropped 计数', async () => {
    mockPost.mockRejectedValue(new Error('persistent error'))
    const { useChatStore } = await import('../chat')
    const store = useChatStore()

    store.forwardBridgeLog({ case_id: 'Q001', level: 'INFO', event: 'exec.start', message: 'ok' })

    // 推进足够长时间让 5 次重试全部执行完
    // 重试延迟：500, 1000, 2000, 4000, 8000 = 总计约 15500ms + 初始 500ms
    await vi.advanceTimersByTimeAsync(20000)
    await vi.runOnlyPendingTimersAsync()

    const stats = getStats()
    expect(stats?.dropped).toBeGreaterThan(0)
    expect(stats?.failures).toBeGreaterThanOrEqual(5)
  })
})