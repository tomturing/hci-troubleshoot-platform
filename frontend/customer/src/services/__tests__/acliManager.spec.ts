import { describe, expect, it, vi } from 'vitest'
import { ensureAcliReady } from '../acliManager'
import { buildAcliSyncMessage } from '@/api/terminal'

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
})
