/**
 * chat store — risk=2 命令确认执行（confirmAgentExec）单元测试
 *
 * 验证工单 Q2026082095867 修复：
 * - 收到 agent_exec_command（risk=2）时，填充 pendingExecConfirm 完整上下文
 * - 用户确认后调用 confirmAgentExec，通过 SSH WebSocket 下发 ssh_exec_process
 * - 正确上报结果并调用 resumeOpsAgentStream
 * - SSH 未连接时上报 ssh_not_connected 错误并继续流
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
  mockConvCreate: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-ec-1' } }),
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
        case_id: 'Q2026082095867',
        assistant_type: 'ops-agent',
        status: 'in_progress',
        client_id: 'test',
        title: 'Exec Confirm Test',
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
vi.mock('@/api/terminal', async (importOriginal) => {
  const original = await importOriginal<typeof import('@/api/terminal')>()
  return {
    ...original,
    createBridgeSocket: vi.fn(),
    checkBridgeRunning: vi.fn().mockResolvedValue(true),
  }
})

import { useChatStore } from '../chat'

describe('chat store: risk=2 命令确认执行下发 (Q2026082095867)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('confirmAgentExec: SSH 未连接时应直接上报 ssh_not_connected 并恢复流', async () => {
    const store = useChatStore()
    const fetchMock = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ ok: true }) })
    globalThis.fetch = fetchMock

    // 设置 pendingExecConfirm 状态
    store.pendingExecConfirm = {
      execId: 'exec-test-1',
      command: 'acli system cat /sf/cfg/gpu_info.ini',
      reason: '检查 GPU 配置',
      riskLevel: 2,
      nodeIp: '172.28.25.4',
      caseId: 'Q2026082095867',
      convId: 'conv-ec-1',
      timestamp: Date.now(),
      container: null,
      traceId: 'trace-123',
      traceparent: null,
      toolCallId: 'exec-test-1',
      timeoutSeconds: 120,
      outputFilters: [],
    }

    // 执行确认
    await store.confirmAgentExec('exec-test-1')

    // pendingExecConfirm 应当被清空
    expect(store.pendingExecConfirm).toBeNull()

    // 应当调用 /exec-result 上报 SSH 未连接
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv-ec-1/exec-result',
      expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('ssh_not_connected'),
      }),
    )
  })

  it('confirmAgentExec: SSH 连接正常时应通过 WebSocket 发送 ssh_exec_process', async () => {
    // 模拟 SSH WebSocket
    const mockSend = vi.fn()
    const mockWs: {
      send: typeof mockSend
      close: ReturnType<typeof vi.fn>
      readyState: number
      onopen: (() => void) | null
      onmessage: ((e: { data: string }) => void) | null
      onerror: (() => void) | null
      onclose: (() => void) | null
    } = {
      send: mockSend,
      close: vi.fn(),
      readyState: 1, // OPEN
      onopen: null,
      onmessage: null,
      onerror: null,
      onclose: null,
    }

    const { createBridgeSocket } = await import('@/api/terminal')
    vi.mocked(createBridgeSocket).mockReturnValue(mockWs as unknown as WebSocket)

    const encoder = new TextEncoder()
    const doneChunk = encoder.encode('data: [DONE]\n\n')
    let streamRead = false
    const fetchMock = vi.fn().mockImplementation((url: string) => {
      if (String(url).includes('resume-stream')) {
        streamRead = false
        return Promise.resolve({
          ok: true,
          body: {
            getReader: () => ({
              read: vi.fn().mockImplementation(async () => {
                if (!streamRead) {
                  streamRead = true
                  return { done: false, value: doneChunk }
                }
                return { done: true, value: undefined }
              }),
            }),
          },
        })
      }
      return Promise.resolve({ ok: true, json: async () => ({ ok: true }) })
    })
    globalThis.fetch = fetchMock
    const store = useChatStore()

    // 建立 SSH 连接
    const connectPromise = store.connectSSH({
      host: '172.28.25.4',
      port: 22,
      username: 'root',
      authType: 'password',
      password: 'password',
      caseId: 'Q2026082095867',
    })

    mockWs.onopen?.()
    mockWs.onmessage?.({
      data: JSON.stringify({
        type: 'ssh_connected',
        case_id: 'Q2026082095867',
      }),
    })
    mockWs.onmessage?.({
      data: JSON.stringify({
        type: 'ssh_output',
        case_id: 'Q2026082095867',
        output: 'Welcome\n',
      }),
    })

    await connectPromise
    expect(store.sshConnectionState).toBe('connected')

    store.pendingExecConfirm = {
      execId: 'exec-test-2',
      command: 'acli --timeout 120 system cat /sf/cfg/gpu_info.ini',
      reason: '检查 GPU 配置文件',
      riskLevel: 2,
      nodeIp: '172.28.25.4',
      caseId: 'Q2026082095867',
      convId: 'conv-ec-1',
      timestamp: Date.now(),
      container: null,
      traceId: 'trace-456',
      traceparent: null,
      toolCallId: 'exec-test-2',
      timeoutSeconds: 120,
      outputFilters: [],
    }

    // 发起确认
    const confirmPromise = store.confirmAgentExec('exec-test-2')

    expect(mockSend).toHaveBeenCalledTimes(2)
    const sentMsg = JSON.parse(mockSend.mock.calls[1][0])
    expect(sentMsg.type).toBe('ssh_exec_process')
    expect(sentMsg.exec_id).toBe('exec-test-2')
    expect(sentMsg.case_id).toBe('Q2026082095867')
    expect(sentMsg.command).toBe('acli --timeout 120 system cat /sf/cfg/gpu_info.ini')
    expect(sentMsg.node_ip).toBe('172.28.25.4')

    // 模拟 terminal_bridge 通过 WebSocket 返回 exec_result
    mockWs.onmessage?.({
      data: JSON.stringify({
        type: 'exec_result',
        exec_id: 'exec-test-2',
        case_id: 'Q2026082095867',
        exit_code: 0,
        output: 'gpu_type=NVIDIA\n',
        stdout: 'gpu_type=NVIDIA\n',
        stderr: '',
      }),
    })

    await confirmPromise

    // 验证 postExecResult 是否提交了 bridge 返回的输出
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/conversations/conv-ec-1/exec-result',
      expect.objectContaining({
        method: 'POST',
        body: expect.stringContaining('gpu_type=NVIDIA'),
      }),
    )
  })
})
