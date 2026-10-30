import { describe, expect, it } from 'vitest'
import { shouldRecoverRelayLease } from '../logCollectRelay'

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
