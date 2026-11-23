import { describe, expect, it, vi } from 'vitest'
import {
  ACLI_PLUGIN_STAGE_TIMEOUT_MS,
  ACLI_STAGE1_TIMEOUT_MS,
  ACLI_SYNC_TIMEOUT_MS,
  AcliSyncFailure,
  acliRetrySuggestions,
  ensureAcliReady,
} from '../acliManager'
import { buildAcliSyncMessage, parseAcliSyncFacts } from '@/api/terminal'

describe('acliManager', () => {
  it('构建默认 acli_sync 消息', () => {
    const msg = JSON.parse(buildAcliSyncMessage('case-100'))
    expect(msg.type).toBe('acli_sync')
    expect(msg.case_id).toBe('case-100')
    expect(msg.force).toBe(false)
    expect(msg.min_disk_mb).toBe(100)
  })

  it('构建自定义参数 acli_sync 消息', () => {
    const msg = JSON.parse(buildAcliSyncMessage('case-200', { force: true, minDiskMb: 500, acliUrl: 'http://custom/url' }))
    expect(msg.type).toBe('acli_sync')
    expect(msg.case_id).toBe('case-200')
    expect(msg.force).toBe(true)
    expect(msg.min_disk_mb).toBe(500)
    expect(msg.acli_url).toBe('http://custom/url')
  })

  // 回归：bridge 的会话键是 caseID@nodeIP，消息里缺 node_ip 时旧逻辑只找键为 caseID 的会话，
  // 结果恒定 session_missing —— 未装 acli 的环境从来没有下载/安装日志。
  it('带上 node_ip，避免 bridge 解析不到 caseID@nodeIP 会话', () => {
    const msg = JSON.parse(buildAcliSyncMessage('case-300', { nodeIp: '172.28.24.129' }))
    expect(msg.node_ip).toBe('172.28.24.129')
  })

  it('收到 acli_sync_result(up_to_date) 时成功 resolve 并报告日志', async () => {
    const listeners: Record<string, Function[]> = {}
    const mockSocket = {
      send: vi.fn(),
      addEventListener: (type: string, fn: any) => {
        listeners[type] = listeners[type] || []
        listeners[type].push(fn)
      },
      removeEventListener: (type: string, fn: any) => {
        listeners[type] = (listeners[type] || []).filter((f) => f !== fn)
      },
    } as unknown as WebSocket

    const logs: string[] = []
    const promise = ensureAcliReady(mockSocket, 'case-1', (l) => logs.push(l.text))

    expect(mockSocket.send).toHaveBeenCalled()

    // 触发进度
    const progressHandler = listeners['message']?.[0]
    progressHandler?.({
      data: JSON.stringify({
        type: 'acli_sync_progress',
        case_id: 'case-1',
        status: 'checking',
        message: '正在检测 acli 状态...',
      }),
    })

    expect(logs).toContain('正在检测 acli 状态...')

    // 触发成功结果
    progressHandler?.({
      data: JSON.stringify({
        type: 'acli_sync_result',
        case_id: 'case-1',
        status: 'up_to_date',
        current_version: '1.0.0',
        architecture: 'x86_64',
        message: 'acli 工具已就绪且为最新版本',
      }),
    })

    const res = await promise
    expect(res.status).toBe('up_to_date')
    expect(res.version).toBe('1.0.0')
    expect(res.architecture).toBe('x86_64')
  })

  it('收到 acli_sync_result(disk_space_insufficient) 时拒绝并抛错', async () => {
    const listeners: Record<string, Function[]> = {}
    const mockSocket = {
      send: vi.fn(),
      addEventListener: (type: string, fn: any) => {
        listeners[type] = listeners[type] || []
        listeners[type].push(fn)
      },
      removeEventListener: (type: string, fn: any) => {
        listeners[type] = (listeners[type] || []).filter((f) => f !== fn)
      },
    } as unknown as WebSocket

    const promise = ensureAcliReady(mockSocket, 'case-err')
    const progressHandler = listeners['message']?.[0]
    progressHandler?.({
      data: JSON.stringify({
        type: 'acli_sync_result',
        case_id: 'case-err',
        status: 'disk_space_insufficient',
        message: '/sf/data/local 目录可用空间不足（当前: 50 MB, 最低需要: 100 MB）',
      }),
    })

    await expect(promise).rejects.toThrow('空间不足')
  })

  it('重试消息带上 retry_stage，让 bridge 跳过已确认正确的阶段', () => {
    const upload = JSON.parse(buildAcliSyncMessage('case-400', { retryStage: 'upload', nodeIp: '1.2.3.4' }))
    expect(upload.retry_stage).toBe('upload')
    expect(upload.node_ip).toBe('1.2.3.4')

    const install = JSON.parse(buildAcliSyncMessage('case-400', { retryStage: 'install' }))
    expect(install.retry_stage).toBe('install')

    // 默认（以及显式 all）都不下发：bridge 只在“用户明确要求重做某跳”时跳过阶段。
    expect(JSON.parse(buildAcliSyncMessage('case-400')).retry_stage).toBeUndefined()
    expect(JSON.parse(buildAcliSyncMessage('case-400', { retryStage: 'all' })).retry_stage).toBeUndefined()
  })

  it('前端超时预算大于 bridge 分段预算之和（120+300+240）', () => {
    // 前端先超时会让用户以为失败并重点一次，叠出第二次上传。
    expect(ACLI_SYNC_TIMEOUT_MS).toBeGreaterThan(660_000)
  })

  it('总预算覆盖两个阶段：阶段一 720s + 阶段二封顶 30min 都要小于前端等待', () => {
    // 阶段二与阶段一串行，桥两段各自有预算；前端按总和等，否则在桥还在装插件时
    // 就把整条链路报成失败，而阶段二失败**不回滚阶段一**，用户看到的是假的“没装成”。
    expect(ACLI_STAGE1_TIMEOUT_MS).toBeGreaterThan(720_000)
    expect(ACLI_PLUGIN_STAGE_TIMEOUT_MS).toBe(1_800_000)
    expect(ACLI_SYNC_TIMEOUT_MS).toBe(ACLI_STAGE1_TIMEOUT_MS + ACLI_PLUGIN_STAGE_TIMEOUT_MS)
  })

  it('阶段二凭据成对下发；只带一半就不下发', () => {
    const pair = JSON.parse(
      buildAcliSyncMessage('case-pair', {
        nodeIp: '10.0.0.1',
        platformUrl: 'https://platform.example',
        pluginTicket: 'tk-1',
      }),
    )
    expect(pair.platform_url).toBe('https://platform.example')
    expect(pair.plugin_ticket).toBe('tk-1')

    // 只带 origin（取票失败）：下发一半等于让桥走完一次无凭据分支，不如不发。
    const half = JSON.parse(buildAcliSyncMessage('case-half', { platformUrl: 'https://platform.example' }))
    expect(half.platform_url).toBeUndefined()
    expect(half.plugin_ticket).toBeUndefined()

    const none = JSON.parse(buildAcliSyncMessage('case-none'))
    expect(none).not.toHaveProperty('platform_url')
    expect(none).not.toHaveProperty('plugin_ticket')
  })

  it('解析完整性事实：摘要、校验强度、安装输出都要能拿到', () => {
    const facts = parseAcliSyncFacts({
      status: 'error',
      stage: 'verify',
      retry_stage: 'upload',
      error_code: 'LC-INSTALL-007',
      error_retryable: 'true',
      attempts: 3,
      package_size: 18718164,
      package_sha256: 'abc',
      package_md5: 'def',
      integrity_state: 'size_only',
      remote_algo: 'md5',
      remote_digest: 'xyz',
      install_success: false,
      install_exit_code: 0,
      install_output: 'boom',
    })
    expect(facts.errorCode).toBe('LC-INSTALL-007')
    expect(facts.stage).toBe('verify')
    expect(facts.retryStage).toBe('upload')
    expect(facts.retryable).toBe(true)
    expect(facts.attempts).toBe(3)
    expect(facts.packageSize).toBe(18718164)
    expect(facts.packageSha256).toBe('abc')
    expect(facts.integrityState).toBe('size_only')
    expect(facts.remoteDigest).toBe('xyz')
    expect(facts.installOutput).toBe('boom')

    // 非法输入不得抛错（result 里可能缺字段）
    const empty = parseAcliSyncFacts(null)
    expect(empty.packageSha256).toBe('')
    expect(empty.retryStage).toBe('all')
    expect(empty.retryable).toBe(false)
  })

  it('重试建议与失败阶段一致：包未证明正确→重传，包已校验→重装', () => {
    const base = parseAcliSyncFacts({ status: 'error', error_retryable: 'true' })
    expect(acliRetrySuggestions({ ...base, stage: 'upload' })).toEqual(['upload', 'all'])
    // 落盘校验不符：包本身还没被证明正确，不能只重装
    expect(acliRetrySuggestions({ ...base, stage: 'verify' })).toEqual(['upload', 'all'])
    expect(acliRetrySuggestions({ ...base, stage: 'download' })).toEqual(['upload', 'all'])
    // 包已校验通过，安装失败只需重装
    expect(acliRetrySuggestions({ ...base, stage: 'install' })).toEqual(['install', 'all'])
    // 装完但版本探测失败：包是好的
    expect(acliRetrySuggestions({ ...base, stage: 'verify', installSuccess: true })).toEqual([
      'install',
      'all',
    ])
    // 不可重试：不给按钮
    expect(acliRetrySuggestions({ ...base, retryable: false, stage: 'upload' })).toEqual([])
  })

  it('失败结果携带可重试信息（界面据此渲染兜底按钮）', async () => {
    const listeners: Record<string, Function[]> = {}
    const mockSocket = {
      send: vi.fn(),
      addEventListener: (type: string, fn: any) => {
        listeners[type] = listeners[type] || []
        listeners[type].push(fn)
      },
      removeEventListener: (type: string, fn: any) => {
        listeners[type] = (listeners[type] || []).filter((f) => f !== fn)
      },
    } as unknown as WebSocket

    const promise = ensureAcliReady(mockSocket, 'case-fail')
    listeners['message']?.[0]?.({
      data: JSON.stringify({
        type: 'acli_sync_result',
        case_id: 'case-fail',
        status: 'error',
        stage: 'upload',
        error_code: 'LC-INSTALL-004',
        error_retryable: 'true',
        package_sha256: 'abc123',
        message: '上传超时',
      }),
    })

    const error = await promise.catch((e) => e)
    expect(error).toBeInstanceOf(AcliSyncFailure)
    expect(error.retrySuggestions).toEqual(['upload', 'all'])
    expect(error.facts.packageSha256).toBe('abc123')
    expect(error.facts.errorCode).toBe('LC-INSTALL-004')
  })

  it('进度消息把完整性事实透传给回调（上传 % 与摘要不再丢）', async () => {
    const listeners: Record<string, Function[]> = {}
    const mockSocket = {
      send: vi.fn(),
      addEventListener: (type: string, fn: any) => {
        listeners[type] = listeners[type] || []
        listeners[type].push(fn)
      },
      removeEventListener: (type: string, fn: any) => {
        listeners[type] = (listeners[type] || []).filter((f) => f !== fn)
      },
    } as unknown as WebSocket

    const seen: any[] = []
    const promise = ensureAcliReady(mockSocket, 'case-progress', (log) => seen.push(log))

    listeners['message']?.[0]?.({
      data: JSON.stringify({
        type: 'acli_sync_progress',
        case_id: 'case-progress',
        status: 'uploading',
        uploaded_bytes: 10485760,
        percent: 56,
        package_size: 18718164,
        package_sha256: 'ff00',
        attempts: 1,
        message: '正在上传安装包：10485760/18718164 字节（56%）',
      }),
    })

    expect(seen[0]?.facts.percent).toBe(56)
    expect(seen[0]?.facts.uploadedBytes).toBe(10485760)
    expect(seen[0]?.facts.packageSha256).toBe('ff00')
    expect(seen[0]?.status).toBe('uploading')

    // 收尾：发一条结果消息把 promise 结掉，避免悬挂的 780s 定时器影响其他用例
    listeners['message']?.[0]?.({
      data: JSON.stringify({ type: 'acli_sync_result', case_id: 'case-progress', status: 'installed' }),
    })
    await promise
  })

  /** 把一套“监听 + 推消息”的 mock 骨架抽出来：下面两个阶段二用例只差一条结果消息。 */
  function mockSocketWithListeners() {
    const listeners: Record<string, Function[]> = {}
    const mockSocket = {
      send: vi.fn(),
      addEventListener: (type: string, fn: any) => {
        listeners[type] = listeners[type] || []
        listeners[type].push(fn)
      },
      removeEventListener: (type: string, fn: any) => {
        listeners[type] = (listeners[type] || []).filter((f) => f !== fn)
      },
    } as unknown as WebSocket
    return { mockSocket, listeners }
  }

  it('阶段二未完成时仍 resolve（阶段一结论不被改写），但单独上屏一条 error', async () => {
    const { mockSocket, listeners } = mockSocketWithListeners()
    const seen: any[] = []
    const promise = ensureAcliReady(mockSocket, 'case-plugin-partial', (log) => seen.push(log))

    listeners['message']?.[0]?.({
      data: JSON.stringify({
        type: 'acli_sync_result',
        case_id: 'case-plugin-partial',
        status: 'installed',
        message: 'acli 安装完成',
        plugin_sync: {
          stage: 'plugin',
          ok: false,
          reason: 'plugin_deploy_failed',
          detail: '10.0.0.2 log_collect 安装失败',
          desired_count: 2,
          installed: 1,
          overwritten: 0,
          skipped: 1,
          failed: 1,
          nodes: ['10.0.0.1', '10.0.0.2'],
          plugins: [
            {
              name: 'log_collect',
              action: 'failed',
              hosts: [{ ip: '10.0.0.2', action: 'failed', reason: 'smoke_empty', device_status: 'error' }],
            },
          ],
        },
      }),
    })

    const res = await promise
    // 阶段一成功是真的：不能因为插件没收敛完就把整次报成失败（那会诱导用户重传安装包）。
    expect(res.status).toBe('installed')
    expect(res.pluginSync?.ok).toBe(false)
    expect(res.pluginSync?.failed).toBe(1)
    expect(res.pluginSync?.plugins?.[0]?.hosts?.[0]?.device_status).toBe('error')

    // 但阶段二的未完成必须单独上屏一条 error，不能只活在 result 里。
    expect(seen.filter((l) => l.type === 'success')).toHaveLength(1)
    const pluginLine = seen.find((l) => l.status === 'plugins')
    expect(pluginLine?.type).toBe('error')
    expect(pluginLine?.text).toContain('plugin_deploy_failed')
    expect(pluginLine?.text).toContain('10.0.0.2 log_collect 安装失败')
  })

  it('旧桥不下发 plugin_sync：pluginSync 为 null，不渲染插件区块', async () => {
    const { mockSocket, listeners } = mockSocketWithListeners()
    const seen: any[] = []
    const promise = ensureAcliReady(mockSocket, 'case-old-bridge', (log) => seen.push(log))

    listeners['message']?.[0]?.({
      data: JSON.stringify({
        type: 'acli_sync_result',
        case_id: 'case-old-bridge',
        status: 'up_to_date',
        message: 'acli 工具已就绪且为最新版本',
      }),
    })

    const res = await promise
    expect(res.pluginSync).toBeNull()
    // 不能假补一条“插件同步：新增 0、覆盖 0、跳过 0”把旧桥显示成已收敛。
    expect(seen.some((l) => l.status === 'plugins')).toBe(false)
  })
})

describe('安装降级（install_degraded）', () => {
  const makeSocket = () => {
    const listeners: Record<string, Function[]> = {}
    const socket = {
      send: vi.fn(),
      addEventListener: (type: string, fn: any) => {
        listeners[type] = listeners[type] || []
        listeners[type].push(fn)
      },
      removeEventListener: (type: string, fn: any) => {
        listeners[type] = (listeners[type] || []).filter((f) => f !== fn)
      },
    } as unknown as WebSocket
    return { socket, listeners }
  }

  it('降级仍然 resolve，但必须显式提示而不冒充成功', async () => {
    const { socket, listeners } = makeSocket()
    const logs: { text: string; type: string }[] = []
    const promise = ensureAcliReady(socket, 'case-degraded', (l) => logs.push({ text: l.text, type: l.type }))

    listeners['message']?.[0]?.({
      data: JSON.stringify({
        type: 'acli_sync_result',
        case_id: 'case-degraded',
        status: 'installed',
        current_version: '1.0.3',
        architecture: 'x86_64',
        message: 'acli 工具安装成功',
        degraded: true,
      }),
    })

    const result = await promise
    // 降级不是失败：工具确实装上了，失败会把用户引去重装。
    expect(result.facts.degraded).toBe(true)
    expect(logs.some((l) => l.text.includes('安装降级'))).toBe(true)
    expect(logs.some((l) => l.type === 'warn')).toBe(true)
  })

  it('未降级时不产生警告噪音', async () => {
    const { socket, listeners } = makeSocket()
    const logs: { text: string }[] = []
    const promise = ensureAcliReady(socket, 'case-ok', (l) => logs.push({ text: l.text }))

    listeners['message']?.[0]?.({
      data: JSON.stringify({
        type: 'acli_sync_result',
        case_id: 'case-ok',
        status: 'installed',
        current_version: '1.0.3',
        architecture: 'x86_64',
        message: 'acli 工具安装成功',
      }),
    })

    const result = await promise
    expect(result.facts.degraded).toBe(false)
    expect(logs.some((l) => l.text.includes('安装降级'))).toBe(false)
  })
})
