import { describe, expect, it, vi } from 'vitest'

/**
 * 错误提示不自动关闭的策略契约。
 *
 * 用户实测反馈：「顶部的红色报错弹框来不及看就消失了，还没看完具体的报错就没有了」。
 * 报错文案通常带路径 / 退出码 / 命令原文，3s 根本读不完，所以统一改成
 * duration=0 + 关闭按钮，由人确认后自行关闭。
 *
 * originError 是 patch 之前的原始 error，用来断言"包装后真正传下去的参数"。
 */
const { messageStub, originError } = vi.hoisted(() => {
  const error = vi.fn()
  const stub: any = Object.assign(vi.fn(), {
    error,
    success: vi.fn(),
    warning: vi.fn(),
    info: vi.fn(),
    closeAll: vi.fn(),
    close: vi.fn(),
  })
  return { messageStub: stub, originError: error }
})

vi.mock('element-plus', () => ({ ElMessage: messageStub }))

describe('installPersistentErrorMessages', () => {
  it('字符串入参：转成 duration=0 + showClose 的配置', async () => {
    const { installPersistentErrorMessages } = await import('@/utils/notify')
    installPersistentErrorMessages()

    originError.mockClear()
    messageStub.error('容器内命令执行失败：exit code 1')
    expect(originError).toHaveBeenCalledWith({
      message: '容器内命令执行失败：exit code 1',
      showClose: true,
      duration: 0,
    })
  })

  it('调用点显式传 3s 也会被策略覆盖（否则问题会从这个调用点复发）', async () => {
    const { installPersistentErrorMessages } = await import('@/utils/notify')
    installPersistentErrorMessages()

    originError.mockClear()
    messageStub.error({ message: '可用空间仅为 1.42GB', duration: 3000 })
    expect(originError).toHaveBeenCalledWith({
      message: '可用空间仅为 1.42GB',
      showClose: true,
      duration: 0,
    })
  })

  it('重复安装不会套娃包装', async () => {
    const { installPersistentErrorMessages } = await import('@/utils/notify')
    installPersistentErrorMessages()
    const before = messageStub.error

    installPersistentErrorMessages()
    installPersistentErrorMessages()
    expect(messageStub.error).toBe(before)
  })

  it('只接管 error，success / warning 仍按默认时长消失', async () => {
    const { installPersistentErrorMessages } = await import('@/utils/notify')
    installPersistentErrorMessages()

    messageStub.success.mockClear()
    messageStub.success('日志已归档')
    // 未被包装：调用参数原样透传，不会带上 duration=0
    expect(messageStub.success).toHaveBeenCalledWith('日志已归档')
  })
})
