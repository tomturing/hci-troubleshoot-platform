import { describe, expect, it } from 'vitest'
import { isTransientPlatformError, shouldRecoverRelayLease } from '../logCollectRelay'

/**
 * 回归背景（2026-10-30 发布复盘）：
 * case-service 发布（Recreate）会杀掉进程内的代传任务，`relay_status` 永久停在
 * `running`。服务端给快照加了 `stale`，前端据此发一次强制续跑；判定错一次就是
 * 「重复抢跑导致同包双传」或「继续干等 2 小时」。
 */
describe('shouldRecoverRelayLease', () => {
  it('running 且租约过期 → 发一次强制续跑', () => {
    expect(shouldRecoverRelayLease({ relay_status: 'running', stale: true }, false)).toBe(true)
  })

  it('已经发过强制续跑 → 不再重复，避免轮询风暴与互相夺权', () => {
    expect(shouldRecoverRelayLease({ relay_status: 'running', stale: true }, true)).toBe(false)
  })

  it('running 但心跳新鲜 → 不抢跑：别的实例正在续租', () => {
    expect(shouldRecoverRelayLease({ relay_status: 'running', stale: false }, false)).toBe(false)
  })

  it('缺少 stale 字段的老服务端 → 保持旧行为，不抢跑', () => {
    expect(shouldRecoverRelayLease({ relay_status: 'running' }, false)).toBe(false)
    expect(shouldRecoverRelayLease({ relay_status: 'running', stale: null }, false)).toBe(false)
  })

  it('终态与未开始都不该重跑', () => {
    expect(shouldRecoverRelayLease({ relay_status: 'success', stale: true }, false)).toBe(false)
    expect(shouldRecoverRelayLease({ relay_status: 'failed', stale: true }, false)).toBe(false)
    expect(shouldRecoverRelayLease({ relay_status: 'pending', stale: true }, false)).toBe(false)
    expect(shouldRecoverRelayLease({ relay_status: 'none', stale: true }, false)).toBe(false)
  })

  it('空快照 → false', () => {
    expect(shouldRecoverRelayLease(null, false)).toBe(false)
    expect(shouldRecoverRelayLease(undefined, false)).toBe(false)
  })
})

/** 构造 axios 风格错误：字段与真实错误一致，避免为纯函数引入 axios 依赖。 */
function axiosError(status?: number) {
  return {
    isAxiosError: true,
    ...(status === undefined ? {} : { response: { status } }),
    message: status === undefined ? 'Network Error' : `Request failed with status code ${status}`,
  }
}

/**
 * 回归背景（2026-11-06 工单 Q2026110600940）：
 * 9.59 GiB 的 P2 代传期间 case-service 被 OOMKilled，重启的两秒里网关把「连接不上
 * 下游」兜底成 `503 Case Service unavailable`。前端把它当成业务失败写进
 * `status=failed`，而服务端代传随后成功（relay success + 链尾摘要校验通过）。
 *
 * 这组用例锁定边界：只有「这一刻问不到」才算结果未知，确定的业务结论
 * （404/400/业务 Error）绝不能被吞成"等一下就好"。
 */
describe('isTransientPlatformError', () => {
  it('网关 503（case-service OOM 重启时的兜底）属于瞬时不可用', () => {
    expect(isTransientPlatformError(axiosError(503))).toBe(true)
  })

  it('502 / 504 同样只代表这一刻问不到', () => {
    expect(isTransientPlatformError(axiosError(502))).toBe(true)
    expect(isTransientPlatformError(axiosError(504))).toBe(true)
  })

  it('没有 response 的 axios 错误（断网 / 连接重置 / 超时）属于瞬时', () => {
    expect(isTransientPlatformError(axiosError())).toBe(true)
  })

  it('404 / 400 / 401 / 403 是确定结论，不能当成瞬时', () => {
    expect(isTransientPlatformError(axiosError(404))).toBe(false)
    expect(isTransientPlatformError(axiosError(400))).toBe(false)
    expect(isTransientPlatformError(axiosError(401))).toBe(false)
    expect(isTransientPlatformError(axiosError(403))).toBe(false)
  })

  it('500 不纳入：平台内部错误可能是确定性缺陷，必须暴露而不是静默重试', () => {
    expect(isTransientPlatformError(axiosError(500))).toBe(false)
  })

  it('业务代码自己 throw 的 Error 必须判为真失败', () => {
    expect(isTransientPlatformError(new Error('任务下发失败'))).toBe(false)
    expect(isTransientPlatformError(new Error('上传会话令牌缺失或已过期'))).toBe(false)
  })

  it('非对象输入不抛异常', () => {
    expect(isTransientPlatformError(null)).toBe(false)
    expect(isTransientPlatformError(undefined)).toBe(false)
    expect(isTransientPlatformError('503')).toBe(false)
  })
})
