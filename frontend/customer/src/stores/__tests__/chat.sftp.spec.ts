/**
 * chat store — SFTP 文件下载接线契约。
 *
 * 用户需求原话：需要在 SSH 终端下面支持把远端主机上已打包好的文件
 * 直接下载到 terminal_bridge 运行的机器上，类似 Xshell / MobaXterm 的文件传输。
 *
 * 这里锁死四件事（都是踩过或极易踩的坑）：
 * 1. 请求只带远端路径，绝不携带本地绝对路径（桌面模式下任意网页都能连 bridge）；
 * 2. 进度按 task_id 配对，旧任务的迟到进度不得污染新任务；
 * 3. list 失败要落到界面上，而不是静默保持“正在读取”；
 * 4. 连接断开必须清空目录快照，否则换主机后会把 A 机路径发到 B 机。
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

const { fakeSocket } = vi.hoisted(() => {
  class FakeSocket {
    readyState = 1
    sent: string[] = []
    onopen: ((ev?: unknown) => void) | null = null
    onmessage: ((ev: { data: string }) => void) | null = null
    onerror: ((ev?: unknown) => void) | null = null
    onclose: ((ev?: unknown) => void) | null = null
    send(data: string) {
      this.sent.push(data)
    }
    close() {
      this.readyState = 3
    }
  }
  return { fakeSocket: new FakeSocket() }
})

vi.mock('@/api/terminal', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/api/terminal')>()
  return {
    ...actual,
    checkBridgeRunning: vi.fn().mockResolvedValue({ running: true }),
    checkBridgeBeforeOpen: vi.fn().mockResolvedValue({ available: true, message: '' }),
    getBridgeUrl: vi.fn(() => 'ws://localhost:9999'),
    createBridgeSocket: vi.fn(() => fakeSocket),
    buildConnectMessage: vi.fn(() => JSON.stringify({ type: 'ssh_connect' })),
    buildDisconnectMessage: vi.fn(() => JSON.stringify({ type: 'ssh_disconnect' })),
  }
})

vi.mock('@/services/acliManager', () => ({
  ensureAcliReady: vi.fn().mockResolvedValue(null),
}))

function sentMessages(): Array<Record<string, unknown>> {
  return fakeSocket.sent
    .map((raw) => {
      try {
        return JSON.parse(raw) as Record<string, unknown>
      } catch {
        return null
      }
    })
    .filter((v): v is Record<string, unknown> => Boolean(v))
}

function sftpMessages(): Array<Record<string, unknown>> {
  return sentMessages().filter((m) => m.type === 'sftp_op')
}

function pushMessage(payload: Record<string, unknown>) {
  fakeSocket.onmessage?.({ data: JSON.stringify(payload) })
}

async function connectedStore() {
  const { useChatStore } = await import('@/stores/chat')
  const store = useChatStore()
  const promise = store.connectSSH({
    host: '10.0.0.9',
    port: 22,
    username: 'admin',
    authType: 'password',
    password: 'secret',
    caseId: 'Q2026102700009',
  })
  fakeSocket.onopen?.()
  // ssh_output 会立刻把 connecting 推进到 connected（比 800ms 稳定窗口快）；
  // 注意空字符串走不进这个分支（store 里是 `&& msg.output` 判定），必须给真实输出
  pushMessage({ type: 'ssh_output', case_id: 'Q2026102700009', output: 'ready\n' })
  await promise
  return store
}

describe('chat store — SFTP 文件下载', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    fakeSocket.sent = []
    fakeSocket.readyState = 1
    const mod: any = await import('@/api/terminal')
    mod.createBridgeSocket.mockClear()
  })

  it('列目录请求只带远端路径与 node_ip，不带任何本地路径', async () => {
    const store = await connectedStore()

    await store.sftpList('/var/log')
    const [msg] = sftpMessages()
    expect(msg.sftp_action).toBe('list')
    expect(msg.sftp_path).toBe('/var/log')
    expect(msg.node_ip).toBe('10.0.0.9')
    expect(msg.case_id).toBe('Q2026102700009')
    // 反证：本地落点由 bridge 决定，前端一旦能指定就是任意文件写入
    expect(msg.local_path).toBeUndefined()
    expect(msg.dest_path).toBeUndefined()
  })

  it('空路径表示“让 bridge 给默认目录”，不应出现 sftp_path 字段', async () => {
    const store = await connectedStore()

    await store.sftpList('')
    const [msg] = sftpMessages()
    expect(msg.sftp_action).toBe('list')
    expect(msg.type === 'sftp_op' && msg.sftp_path === undefined).toBe(true)
  })

  it('收到目录回包后填充列表与落地目录，并结束 loading', async () => {
    const store = await connectedStore()
    await store.sftpList('/var/log')

    pushMessage({
      type: 'sftp_list_result',
      case_id: 'Q2026102700009',
      sftp_data: {
        path: '/var/log',
        parent: '/var',
        download_dir: '/home/engineer/Downloads/hci-sftp',
        entries: [
          { name: 'hci', path: '/var/log/hci', is_dir: true, size: 0 },
          { name: 'messages', path: '/var/log/messages', is_dir: false, size: 2048 },
        ],
        truncated: false,
      },
    })

    expect(store.sftpListLoading).toBe(false)
    expect(store.sftpRemoteDir).toBe('/var/log')
    expect(store.sftpParentDir).toBe('/var')
    expect(store.sftpDownloadDir).toBe('/home/engineer/Downloads/hci-sftp')
    expect(store.sftpEntries.map((e: any) => e.name)).toEqual(['hci', 'messages'])
    expect(store.sftpListError).toBe('')
  })

  it('目录请求失败必须落到 sftpListError，并结束 loading', async () => {
    const store = await connectedStore()
    await store.sftpList('/root')

    pushMessage({
      type: 'sftp_result',
      case_id: 'Q2026102700009',
      sftp_action: 'list',
      error_type: 'remote_io_failed',
      message: '读取远端目录失败: permission denied',
    })

    expect(store.sftpListLoading).toBe(false)
    expect(store.sftpListError).toContain('permission denied')
    // 失败不改写已有列表（此处本就没有），只报错
    expect(store.sftpEntries).toEqual([])
  })

  it('下载请求带 task_id，进度按 task_id 配对，异物进度被丢弃', async () => {
    const store = await connectedStore()
    store.sftpDownload('/var/log/hci/log.zip')
    const [msg] = sftpMessages()
    expect(msg.sftp_action).toBe('download')
    expect(msg.sftp_path).toBe('/var/log/hci/log.zip')
    const taskId = String(msg.exec_id)
    expect(taskId).toMatch(/^sftp-/)

    pushMessage({
      type: 'sftp_transfer_progress',
      case_id: 'Q2026102700009',
      sftp_action: 'download',
      sftp_data: {
        task_id: taskId,
        phase: 'downloading',
        remote_path: '/var/log/hci/log.zip',
        bytes: 512,
        total_bytes: 1024,
        percent: 50,
        transport: 'sftp',
      },
    })
    expect(store.sftpTransfer?.percent).toBe(50)
    expect(store.sftpTransfer?.transport).toBe('sftp')

    // 旧任务的迟到进度：绝不允许把新任务刷回 10%
    pushMessage({
      type: 'sftp_transfer_progress',
      case_id: 'Q2026102700009',
      sftp_action: 'download',
      sftp_data: {
        task_id: 'sftp-stale',
        phase: 'downloading',
        bytes: 100,
        total_bytes: 1024,
        percent: 10,
      },
    })
    expect(store.sftpTransfer?.percent).toBe(50)
    expect(store.sftpTransfer?.taskId).toBe(taskId)
  })

  it('下载完成展示落地路径与 sha256', async () => {
    const store = await connectedStore()
    store.sftpDownload('/var/log/hci/log.zip')
    const taskId = String(sftpMessages()[0].exec_id)

    pushMessage({
      type: 'sftp_result',
      case_id: 'Q2026102700009',
      sftp_action: 'download',
      exec_id: taskId,
      sftp_data: {
        task_id: taskId,
        phase: 'done',
        remote_path: '/var/log/hci/log.zip',
        local_path: '/home/engineer/Downloads/hci-sftp/log.zip',
        download_dir: '/home/engineer/Downloads/hci-sftp',
        bytes: 1024,
        total_bytes: 1024,
        percent: 100,
        status: 'ok',
        transport: 'sftp',
        sha256: 'abc123',
      },
    })

    const transfer = store.sftpTransfer
    expect(transfer?.phase).toBe('done')
    expect(transfer?.status).toBe('ok')
    expect(transfer?.localPath).toBe('/home/engineer/Downloads/hci-sftp/log.zip')
    expect(transfer?.sha256).toBe('abc123')
    expect(store.sftpDownloadDir).toBe('/home/engineer/Downloads/hci-sftp')
  })

  it('下载失败落到 error 状态并保留 bridge 的错误文案', async () => {
    const store = await connectedStore()
    store.sftpDownload('/var/log/hci/log.zip')
    const taskId = String(sftpMessages()[0].exec_id)

    pushMessage({
      type: 'sftp_result',
      case_id: 'Q2026102700009',
      sftp_action: 'download',
      exec_id: taskId,
      error_type: 'remote_io_failed',
      message: '打开远端文件失败: no such file',
      sftp_data: {
        task_id: taskId,
        phase: 'done',
        total_bytes: 0,
        bytes: 0,
        percent: 0,
        status: 'error',
      },
    })

    expect(store.sftpTransfer?.status).toBe('error')
    expect(store.sftpTransfer?.errorType).toBe('remote_io_failed')
    expect(store.sftpTransfer?.errorMessage).toContain('no such file')
  })

  it('取消下载发出的 cancel 携带原 task_id', async () => {
    const store = await connectedStore()
    store.sftpDownload('/var/log/hci/log.zip')
    const taskId = String(sftpMessages()[0].exec_id)

    store.sftpCancelDownload()
    const cancelMsg = sftpMessages().find((m) => m.sftp_action === 'cancel')
    expect(cancelMsg?.exec_id).toBe(taskId)
  })

  it('未连接时发起下载只报错，不发任何消息', async () => {
    const { useChatStore } = await import('@/stores/chat')
    const store = useChatStore()

    store.sftpDownload('/var/log/hci/log.zip')
    expect(store.sftpListError).toContain('请先连接 SSH 终端')
    expect(sftpMessages()).toHaveLength(0)
  })

  it('断开连接后清空目录快照，避免把上一台主机的路径发到新主机', async () => {
    const store = await connectedStore()
    await store.sftpList('/var/log')
    pushMessage({
      type: 'sftp_list_result',
      case_id: 'Q2026102700009',
      sftp_data: {
        path: '/var/log',
        parent: '/var',
        download_dir: '/tmp/hci-sftp',
        entries: [{ name: 'messages', path: '/var/log/messages', is_dir: false, size: 1 }],
        truncated: false,
      },
    })
    expect(store.sftpEntries).toHaveLength(1)

    await store.disconnectSSH()

    expect(store.sftpEntries).toEqual([])
    expect(store.sftpRemoteDir).toBe('')
    expect(store.sftpDownloadDir).toBe('')
    expect(store.sftpTransfer).toBeNull()
  })
})
