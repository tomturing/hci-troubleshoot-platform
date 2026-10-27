import { describe, expect, it, vi } from 'vitest'
import { buildCancelNotice, cancelLogCollectTask } from '../logCollectCancel'

describe('logCollectCancel', () => {
  it('节点取消成功后平台才写终态，并按成功提示', async () => {
    const order: string[] = []
    const result = await cancelLogCollectTask({
      stopNode: async () => {
        order.push('stopNode')
        return { status: 'ok' }
      },
      cancelPlatform: async () => {
        order.push('cancelPlatform')
      },
    })

    expect(result).toEqual({ stopped: true, error: '' })
    // 顺序不可反：先写平台终态后，一旦节点取消失败就再无入口把节点收干净
    expect(order).toEqual(['stopNode', 'cancelPlatform'])
    expect(buildCancelNotice(result).level).toBe('success')
  })

  it('节点返回 error 时仍写平台终态，但提示需要人工收尾并带上原因', async () => {
    const cancelPlatform = vi.fn().mockResolvedValue(undefined)
    const result = await cancelLogCollectTask({
      stopNode: async () => ({ status: 'error', error_type: 'session_missing', message: 'SSH 会话不存在' }),
      cancelPlatform,
    })

    expect(result.stopped).toBe(false)
    expect(result.error).toBe('SSH 会话不存在')
    expect(cancelPlatform).toHaveBeenCalledTimes(1)

    const notice = buildCancelNotice(result)
    expect(notice.level).toBe('warning')
    expect(notice.text).toContain('acli log_collect cancel')
    expect(notice.text).toContain('SSH 会话不存在')
  })

  it('节点通道抛错（bridge 断开）时不让取消流程整体失败', async () => {
    const result = await cancelLogCollectTask({
      stopNode: async () => {
        throw new Error('bridge 连接已断开')
      },
      cancelPlatform: async () => undefined,
    })

    expect(result.stopped).toBe(false)
    expect(result.error).toBe('bridge 连接已断开')
  })

  it('平台写入失败不影响节点侧的结论', async () => {
    const result = await cancelLogCollectTask({
      stopNode: async () => ({ status: 'ok' }),
      cancelPlatform: async () => {
        throw new Error('500')
      },
    })

    expect(result).toEqual({ stopped: true, error: '' })
    expect(buildCancelNotice(result).level).toBe('success')
  })
})
