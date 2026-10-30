import { describe, expect, it, vi } from 'vitest'
import {
  BRIDGE_VERSION_MANIFEST_URL,
  isBridgeVersionStale,
  loadPublishedBridgeVersion,
  normalizeBridgeVersion,
} from '../bridgeVersion'

describe('normalizeBridgeVersion', () => {
  it('非字符串一律折成空串（bridge_hello 的老回包可能没有 message）', () => {
    expect(normalizeBridgeVersion(undefined)).toBe('')
    expect(normalizeBridgeVersion(null)).toBe('')
    expect(normalizeBridgeVersion(123)).toBe('')
    expect(normalizeBridgeVersion({})).toBe('')
  })

  it('去掉首尾空白，避免同一个版本因格式差异被误判', () => {
    expect(normalizeBridgeVersion('  v2.33.1\n')).toBe('v2.33.1')
  })
})

describe('isBridgeVersionStale', () => {
  it('版本一致时不提示', () => {
    expect(isBridgeVersionStale('v2.33.1', { version: 'v2.33.1' })).toBe(false)
  })

  it('版本不一致时提示（平台已发新版，本机仍在跑旧版）', () => {
    expect(isBridgeVersionStale('v2.32.0', { version: 'v2.33.1' })).toBe(true)
  })

  // 以下三条是"无从判断"，必须与"不一致"严格区分：把判断不了当成不一致，
  // 会让老版本桥、清单 404、离线三种情况全部变成全量误报。
  it('本机版本缺失（老桥不带 message）时不提示', () => {
    expect(isBridgeVersionStale('', { version: 'v2.33.1' })).toBe(false)
    expect(isBridgeVersionStale(undefined, { version: 'v2.33.1' })).toBe(false)
  })

  it('清单缺失时不提示', () => {
    expect(isBridgeVersionStale('v2.33.1', null)).toBe(false)
    expect(isBridgeVersionStale('v2.33.1', { version: '' })).toBe(false)
  })

  it('只比版本串，不比空白差异', () => {
    expect(isBridgeVersionStale(' v2.33.1 ', { version: 'v2.33.1 ' })).toBe(false)
  })
})

describe('loadPublishedBridgeVersion', () => {
  it('正常读取清单', async () => {
    const fetcher = vi.fn().mockResolvedValue({
      ok: true,
      json: async () => ({ version: 'v2.33.1', commit: 'abc123', build_time: '2026-10-30T09:09:49Z' }),
    })
    const got = await loadPublishedBridgeVersion(fetcher as unknown as typeof fetch)
    expect(got).toEqual({
      version: 'v2.33.1',
      commit: 'abc123',
      build_time: '2026-10-30T09:09:49Z',
    })
    expect(fetcher).toHaveBeenCalledWith(BRIDGE_VERSION_MANIFEST_URL, { cache: 'no-store' })
  })

  it('404 时返回 null（不提示，不阻断）', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: false, json: async () => ({}) })
    await expect(loadPublishedBridgeVersion(fetcher as unknown as typeof fetch)).resolves.toBeNull()
  })

  it('清单内容缺 version 时返回 null', async () => {
    const fetcher = vi.fn().mockResolvedValue({ ok: true, json: async () => ({ commit: 'abc' }) })
    await expect(loadPublishedBridgeVersion(fetcher as unknown as typeof fetch)).resolves.toBeNull()
  })

  it('请求抛异常时返回 null（离线 / 被拦截）', async () => {
    const fetcher = vi.fn().mockRejectedValue(new Error('network down'))
    await expect(loadPublishedBridgeVersion(fetcher as unknown as typeof fetch)).resolves.toBeNull()
  })
})
