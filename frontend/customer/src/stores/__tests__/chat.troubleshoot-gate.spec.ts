/**
 * chat store — 排障前置门禁（bridge 可打开 + SSH 已连接）
 *
 * 现场缺陷（2026-10-29 Q2026102901522）：刷新页面后点「继续处理」，前端没有检查
 * terminal_bridge 与 SSH 会话就直接继续排障，SOP 被推到"执行中"却永远收不到节点回包
 * （界面停在「正在等待输出」）。
 *
 * 门禁要同时满足三件事，缺一即失败：
 * ① 进入排障动作前就阻断（不是执行失败再报错）；
 * ② 已连接 SSH 时零成本放行（不重复探测，不打扰）；
 * ③ 只给三条出路：下载并运行 bridge / SSH 登录 / 无 SSH 排障（跳离线诊断）。
 */

import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { mockApiGet, mockListByClient, mockAssistantList, mockCheckBridgeBeforeOpen, mockNavigateOffline } =
  vi.hoisted(() => ({
    mockApiGet: vi.fn().mockResolvedValue({ data: [] }),
    mockListByClient: vi.fn().mockResolvedValue({ data: [] }),
    mockAssistantList: vi.fn().mockResolvedValue({ data: { assistants: [], show_selector: false } }),
    mockCheckBridgeBeforeOpen: vi.fn(),
    mockNavigateOffline: vi.fn(),
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
    getById: vi.fn().mockResolvedValue({ data: {} }),
    close: vi.fn().mockResolvedValue({ data: {} }),
    create: vi.fn().mockResolvedValue({ data: {} }),
  }),
  createConversationApi: () => ({
    create: vi.fn().mockResolvedValue({ data: { conversation_id: 'conv-gate' } }),
    getMessages: vi.fn().mockResolvedValue({ data: [] }),
  }),
  createAssistantApi: () => ({ list: mockAssistantList }),
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
vi.mock('@/utils/offlineDiagnosis', () => ({
  OFFLINE_DIAGNOSIS_PATH: '/offline-diagnosis',
  navigateToOfflineDiagnosis: mockNavigateOffline,
  buildOfflineDiagnosisUrl: (caseId: string) => `/offline-diagnosis?case_id=${caseId}`,
  isOfflineDiagnosisPath: () => false,
}))
vi.mock('@/api/terminal', () => ({
  checkBridgeRunning: vi.fn().mockResolvedValue(false),
  checkBridgeBeforeOpen: mockCheckBridgeBeforeOpen,
  createBridgeSocket: vi.fn(),
  getBridgeUrl: vi.fn().mockReturnValue('ws://localhost:9999'),
  buildConnectMessage: vi.fn(),
  buildInputMessage: vi.fn(),
  buildDisconnectMessage: vi.fn(),
  buildAgentExecMessage: vi.fn(),
  buildAgentExecProcessMessage: vi.fn(),
  buildVmConsoleOpMessage: vi.fn(),
  buildSftpMessage: vi.fn(),
  parseAgentExecResult: vi.fn(),
  parseSftpListPayload: vi.fn(),
  parseSftpTransferPayload: vi.fn(),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

function makeCase(caseId = 'Q2026102901522') {
  return {
    case_id: caseId,
    client_id: 'test-client-id',
    status: 'in_progress',
    title: '检测到硬盘',
    description: null,
    assistant_type: 'htp-agent',
    created_at: '2026-10-29T00:00:00Z',
    updated_at: '2026-10-29T00:00:00Z',
    closed_at: null,
    trace_id: null,
  }
}

/** 空 SSE 流：只关心"消息有没有发出去"，不关心回复内容 */
function makeEmptyStreamFetchMock() {
  return vi.fn().mockResolvedValue({
    ok: true,
    body: {
      getReader: () => ({ read: vi.fn().mockResolvedValue({ done: true, value: undefined }) }),
    },
  })
}

describe('chat store — 排障前置门禁', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
    mockApiGet.mockResolvedValue({ data: [] })
    mockCheckBridgeBeforeOpen.mockResolvedValue('not-running')
  })

  afterEach(() => {
    vi.unstubAllGlobals()
  })

  it('已连接 SSH 时直接放行，且不重复探测 bridge', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = makeCase() as any
    store.conversationId = 'conv-gate'
    store.sshConnectionState = 'connected'
    vi.stubGlobal('fetch', makeEmptyStreamFetchMock())

    const sent = await store.sendMessage('继续排查硬盘')

    expect(sent).toBe(true)
    expect(store.troubleshootGateVisible).toBe(false)
    // SSH 会话本身就是 bridge 存活的证据，不该再花 1.5s 探测
    expect(mockCheckBridgeBeforeOpen).not.toHaveBeenCalled()
    expect(store.messages.some((m) => m.role === 'user')).toBe(true)
  })

  it('bridge 未运行：消息不发出，弹 bridge 引导，取消后内容回到输入框', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = makeCase() as any
    store.conversationId = 'conv-gate'

    const pending = store.sendMessage('继续排查硬盘')
    await vi.waitFor(() => expect(store.troubleshootGateStage).toBe('bridge-down'))

    expect(store.troubleshootGateVisible).toBe(true)
    expect(store.troubleshootGateBridgeUrl).toBe('ws://localhost:9999')
    // 门禁未通过前不得落消息、不得发起请求
    expect(store.messages).toHaveLength(0)

    store.cancelTroubleshootGate()
    await expect(pending).resolves.toBe(false)
    expect(store.troubleshootGateVisible).toBe(false)
    expect(store.assistantDraftText).toBe('继续排查硬盘')
  })

  it('bridge 已运行但未连 SSH：进入 SSH 阶段，连上后自动继续发送', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = makeCase() as any
    store.conversationId = 'conv-gate'
    mockCheckBridgeBeforeOpen.mockResolvedValue('running')
    vi.stubGlobal('fetch', makeEmptyStreamFetchMock())

    const pending = store.sendMessage('继续排查硬盘')
    await vi.waitFor(() => expect(store.troubleshootGateStage).toBe('ssh-required'))
    expect(store.messages).toHaveLength(0)

    // 用户在门禁内完成 SSH 登录（SshConnectSection 的 connected 事件）
    store.sshConnectionState = 'connected'
    store.confirmTroubleshootGate()

    await expect(pending).resolves.toBe(true)
    expect(store.troubleshootGateVisible).toBe(false)
    expect(store.messages.some((m) => m.role === 'user' && m.content === '继续排查硬盘')).toBe(true)
  })

  it('选择「无 SSH 排障」：跳转离线诊断并放弃本次发送', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = makeCase('Q2026102901522') as any
    store.conversationId = 'conv-gate'

    const pending = store.sendMessage('继续排查硬盘')
    await vi.waitFor(() => expect(store.troubleshootGateStage).toBe('bridge-down'))

    store.chooseOfflineTroubleshoot()

    await expect(pending).resolves.toBe(false)
    expect(mockNavigateOffline).toHaveBeenCalledWith('Q2026102901522')
    expect(store.messages).toHaveLength(0)
  })

  it('继续已有工单（resumePendingCase）也必须过门禁', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.pendingCase = makeCase() as any

    await store.resumePendingCase()

    await vi.waitFor(() => expect(store.troubleshootGateVisible).toBe(true))
    expect(store.currentCase?.case_id).toBe('Q2026102901522')
    expect(store.troubleshootGateStage).toBe('bridge-down')
  })

  it('继续已有工单时若已连接 SSH，则不弹门禁', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.pendingCase = makeCase() as any
    store.sshConnectionState = 'connected'

    await store.resumePendingCase()

    expect(store.troubleshootGateVisible).toBe(false)
    expect(mockCheckBridgeBeforeOpen).not.toHaveBeenCalled()
  })

  it('并发入口共用同一个弹框（发消息 + 继续工单不会叠两层）', async () => {
    const { useChatStore } = await import('../chat')
    const store = useChatStore()
    store.currentCase = makeCase() as any
    store.conversationId = 'conv-gate'

    const pendingSend = store.sendMessage('继续排查硬盘')
    await vi.waitFor(() => expect(store.troubleshootGateVisible).toBe(true))

    // 第二个入口在弹框未决期间触发：只应复用同一份结论
    const second = store.sendMessage('再补充一句')
    await Promise.resolve()

    expect(mockCheckBridgeBeforeOpen).toHaveBeenCalledTimes(1)

    store.cancelTroubleshootGate()
    await expect(pendingSend).resolves.toBe(false)
    await expect(second).resolves.toBe(false)
  })
})
