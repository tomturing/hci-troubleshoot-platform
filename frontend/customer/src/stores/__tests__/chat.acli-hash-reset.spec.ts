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

const { ensureAcliReadyMock } = vi.hoisted(() => ({
  ensureAcliReadyMock: vi.fn(),
}))

vi.mock('@/services/acliManager', () => ({ ensureAcliReady: ensureAcliReadyMock }))

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
    setActivePinia(createPinia())
    ensureAcliReadyMock.mockReset()
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
})
