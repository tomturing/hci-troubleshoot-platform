/**
 * chat store - acli 安装后的「生效」动作测试
 *
 * 现场问题：acli 重新安装成功后，已打开的终端里执行 acli 仍报
 * `/sf/sbin/acli: No such file or directory`，必须重连终端才好。
 *
 * 根因：bash 把解析过的命令路径缓存在 hash 表里，安装器替换/挪走二进制后，
 * 老 shell 仍 exec 旧路径，只有新 shell 才会重查 PATH。
 *
 * 契约：
 * 1. 安装成功（installed）且终端抽屉开着 → 向该会话发送 `hash -r`，让现有 shell 立即生效；
 * 2. 终端没开 → 不发任何命令（避免给用户凭空塞一条命令），改为在日志里提示；
 * 3. 非安装（up_to_date 等）→ 二进制没被替换，什么都不做。
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

const { ensureAcliReadyMock, resolveCredentialsMock, confirmMock } = vi.hoisted(() => ({
  ensureAcliReadyMock: vi.fn(),
  resolveCredentialsMock: vi.fn(),
  confirmMock: vi.fn(),
}))

vi.mock('@/services/acliManager', () => ({ ensureAcliReady: ensureAcliReadyMock }))

// 自签证书授权弹窗：store 里是动态 import，这里同样用模块 mock 接管，
// 否则会真的去挂载 Element Plus 的 MessageBox。
vi.mock('element-plus', () => ({ ElMessageBox: { confirm: confirmMock } }))

// 阶段二取票：在 store 边界上桩掉。“连接后总是要同步”现在多了一步 HTTP，
// 不桩就会把用例挂在真实 axios 上。
vi.mock('@/api/acliPluginSync', () => ({
  platformRootUrl: () => 'https://platform.example',
  requestAcliPluginTicket: vi.fn(),
  resolveAcliPluginSyncCredentials: resolveCredentialsMock,
}))

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

vi.mock('@/api/terminal', () => ({
  checkBridgeRunning: vi.fn().mockResolvedValue(false),
  checkBridgeBeforeOpen: vi.fn().mockResolvedValue('not-running'),
  createBridgeSocket: vi.fn(),
  getBridgeUrl: vi.fn().mockReturnValue('ws://127.0.0.1:17680'),
  buildConnectMessage: vi.fn(),
  // 只关心"发了什么内容"，用可读字符串回显即可
  buildInputMessage: (_caseId: string, data: string) => `INPUT:${data.trim()}`,
  buildDisconnectMessage: vi.fn(),
  buildAgentExecMessage: vi.fn(),
  buildAgentExecProcessMessage: vi.fn(),
  buildVmConsoleOpMessage: vi.fn(),
  parseAgentExecResult: vi.fn(),
  stripAnsi: (s: string) => s,
  parseJsonOutput: vi.fn(),
}))

function fakeSocket() {
  return { send: vi.fn(), readyState: 1 } as unknown as WebSocket & { send: ReturnType<typeof vi.fn> }
}

async function setup() {
  const { useChatStore } = await import('../chat')
  const store = useChatStore()
  const ws = fakeSocket()
  store.sshWebSocket = ws as any
  store.sshConnectionState = 'connected'
  store.sshCurrentConfig = { caseId: 'Q2026102300001', host: '10.10.1.21' } as any
  return { store, ws }
}

describe('chat store - acli 安装后生效动作', () => {
  beforeEach(() => {
    localStorage.clear()
    sessionStorage.clear()
    confirmMock.mockReset()
    setActivePinia(createPinia())
    ensureAcliReadyMock.mockReset()
    // 默认“本次没有阶段二凭据”（等价于取票失败），让其余用例只关心 hash -r；
    // 逐例可覆写。mockReset 会把上一条用例留下的 mockResolvedValue 一并清掉。
    resolveCredentialsMock.mockReset()
    resolveCredentialsMock.mockResolvedValue({ credentials: {}, error: '' })
  })

  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('安装成功且终端打开时，向该会话发送 hash -r', async () => {
    ensureAcliReadyMock.mockResolvedValue({ status: 'installed', version: '1.0.1' })
    const { store, ws } = await setup()
    store.showTerminalSidebar = true

    await store.checkAndSyncAcli()

    const sent = (ws.send as any).mock.calls.map((c: any[]) => c[0])
    expect(sent).toContain('INPUT:hash -r')
  })

  it('终端没打开时不发命令（不替用户凭空执行）', async () => {
    ensureAcliReadyMock.mockResolvedValue({ status: 'installed', version: '1.0.1' })
    const { store, ws } = await setup()
    store.showTerminalSidebar = false

    await store.checkAndSyncAcli()

    expect(ws.send).not.toHaveBeenCalled()
  })

  it('环境采集占用输入通道时不抢输出，只留提示', async () => {
    ensureAcliReadyMock.mockResolvedValue({ status: 'installed', version: '1.0.1' })
    const { store, ws } = await setup()
    store.showTerminalSidebar = true
    store.sshCommandConsumer = 'collection'

    await store.checkAndSyncAcli()

    expect(ws.send).not.toHaveBeenCalled()
  })

  it('已是最新版本时不发 hash -r（二进制没被替换）', async () => {
    ensureAcliReadyMock.mockResolvedValue({ status: 'up_to_date', version: '1.0.1' })
    const { store, ws } = await setup()
    store.showTerminalSidebar = true

    await store.checkAndSyncAcli()

    expect(ws.send).not.toHaveBeenCalled()
  })

  it('SSH 未连接时直接跳过，不做任何探测', async () => {
    const { store } = await setup()
    store.sshConnectionState = 'disconnected'

    const result = await store.checkAndSyncAcli()

    expect(result).toBeNull()
    expect(ensureAcliReadyMock).not.toHaveBeenCalled()
  })

  // 收口设计 S4b：插件期望态只在 acli_sync 里拉。连接后的后台同步必须把按工单
  // 签发的凭据一路带到报文选项里——不带来，阶段二在生产环境里永远不会跑。
  it('后台同步带上阶段二凭据（按当前工单取票）', async () => {
    resolveCredentialsMock.mockResolvedValue({
      credentials: { platformUrl: 'https://platform.example', pluginTicket: 'tk-3' },
      error: '',
    })
    ensureAcliReadyMock.mockResolvedValue({ status: 'up_to_date', version: '1.0.1' })
    const { store } = await setup()

    await store.checkAndSyncAcli()

    expect(resolveCredentialsMock).toHaveBeenCalledWith('Q2026102300001')
    const options = ensureAcliReadyMock.mock.calls[0][3]
    expect(options.nodeIp).toBe('10.10.1.21')
    expect(options.platformUrl).toBe('https://platform.example')
    expect(options.pluginTicket).toBe('tk-3')
  })

  it('取票失败只降级不阻断：阶段一照旧执行，且不送半套凭据', async () => {
    resolveCredentialsMock.mockResolvedValue({ credentials: {}, error: '404 case 不存在' })
    ensureAcliReadyMock.mockResolvedValue({ status: 'installed', version: '1.0.1' })
    const { store } = await setup()
    store.showTerminalSidebar = false

    const result = await store.checkAndSyncAcli()

    expect(result?.status).toBe('installed')
    const options = ensureAcliReadyMock.mock.calls[0][3]
    expect(options.platformUrl).toBeUndefined()
    expect(options.pluginTicket).toBeUndefined()
  })

  // 正式环境（staging/prod）是自签 HTTPS：桥拉插件清单必然 x509。
  // 用户在本会话没授权时，必须问一次；授权后带 tls_insecure 重试，并记住该 origin。
  it('插件同步命中自签证书：授权后带 tls_insecure 重试一次', async () => {
    resolveCredentialsMock.mockResolvedValue({
      credentials: {
        platformUrl: 'https://platform.example',
        pluginTicket: 'tk-1',
        tlsInsecure: false,
      },
      error: '',
    })
    ensureAcliReadyMock
      .mockResolvedValueOnce({
        status: 'up_to_date',
        version: '1.0.1',
        pluginSync: {
          stage: 'plugin',
          ok: false,
          reason: 'manifest_unreachable',
          detail: 'x509: certificate signed by unknown authority',
          desired_count: 0,
          installed: 0,
          overwritten: 0,
          skipped: 0,
          failed: 0,
        },
      })
      .mockResolvedValueOnce({ status: 'up_to_date', version: '1.0.1', pluginSync: null })
    confirmMock.mockResolvedValue(undefined)
    const { store } = await setup()

    await store.checkAndSyncAcli()

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(ensureAcliReadyMock).toHaveBeenCalledTimes(2)
    expect(ensureAcliReadyMock.mock.calls[1][3].tlsInsecure).toBe(true)
    expect(
      sessionStorage.getItem('hci.logCollect.trustPlatformCert:https://platform.example'),
    ).toBe('1')
  })

  it('用户取消信任则只跑一次，不重试也不记授权', async () => {
    resolveCredentialsMock.mockResolvedValue({
      credentials: { platformUrl: 'https://platform.example', pluginTicket: 'tk-1' },
      error: '',
    })
    ensureAcliReadyMock.mockResolvedValue({
      status: 'up_to_date',
      version: '1.0.1',
      pluginSync: {
        stage: 'plugin',
        ok: false,
        reason: 'manifest_unreachable',
        detail: 'x509: certificate signed by unknown authority',
        desired_count: 0,
        installed: 0,
        overwritten: 0,
        skipped: 0,
        failed: 0,
      },
    })
    confirmMock.mockRejectedValue(new Error('cancel'))
    const { store } = await setup()

    await store.checkAndSyncAcli()

    expect(confirmMock).toHaveBeenCalledTimes(1)
    expect(ensureAcliReadyMock).toHaveBeenCalledTimes(1)
    expect(
      sessionStorage.getItem('hci.logCollect.trustPlatformCert:https://platform.example'),
    ).toBeNull()
  })
})
