import { describe, expect, it, vi } from 'vitest'
import { ACLI_SYNC_TIMEOUT_MS, AcliSyncFailure, acliRetrySuggestions, ensureAcliReady } from '../acliManager'
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
})
