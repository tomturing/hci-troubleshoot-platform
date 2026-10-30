import { describe, expect, it } from 'vitest'
import {
  COLLECT_BAND_END,
  DELIVERY_BAND_END,
  clampPercent,
  computeOverallPercent,
  formatByteCount,
} from '../logCollectProgress'

/**
 * 回归背景（2026-10-30 现场 Q2026102901049）：
 * 进度条显示 100%，同一屏的状态文案是「正在从 HCI 拉取：0 字节」。
 * 根因是节点 percent 在采集结束时就到 100，旧实现用 Math.max 把它带进了整段交付。
 */
describe('computeOverallPercent 分段映射', () => {
  it('采集到 100 只代表整体走到采集段终点，不是任务完成', () => {
    expect(computeOverallPercent({ stage: 'collecting', nodePercent: 100 })).toBe(COLLECT_BAND_END)
  })

  it('刚开始拉取（0 字节）必须落在交付段起点，不能是 100', () => {
    expect(
      computeOverallPercent({ stage: 'pulling', nodePercent: 100, deliveryPercent: 0 }),
    ).toBe(COLLECT_BAND_END)
  })

  it('拉取结束（作业进度 50）落在交付段中点', () => {
    expect(
      computeOverallPercent({ stage: 'pulling', nodePercent: 100, deliveryPercent: 50 }),
    ).toBe(75)
  })

  it('分片上传结束（作业进度 100）到交付段终点，仍不是 100', () => {
    expect(
      computeOverallPercent({ stage: 'uploading', nodePercent: 100, deliveryPercent: 100 }),
    ).toBe(DELIVERY_BAND_END)
  })

  it('归档阶段停在交付段终点，由完成事件给 100', () => {
    expect(computeOverallPercent({ stage: 'relaying', nodePercent: 100 })).toBe(DELIVERY_BAND_END)
    expect(computeOverallPercent({ stage: 'done', nodePercent: 100 })).toBe(100)
  })

  it('全链路单调不倒退：采集 → 拉取 → 上传 → 归档 → 完成', () => {
    const samples = [
      computeOverallPercent({ stage: 'collecting', nodePercent: 40 }),
      computeOverallPercent({ stage: 'collecting', nodePercent: 100 }),
      computeOverallPercent({ stage: 'pulling', nodePercent: 100, deliveryPercent: 25 }),
      computeOverallPercent({ stage: 'uploading', nodePercent: 100, deliveryPercent: 80 }),
      computeOverallPercent({ stage: 'relaying', nodePercent: 100 }),
      computeOverallPercent({ stage: 'done', nodePercent: 100 }),
    ]
    for (let i = 1; i < samples.length; i += 1) {
      expect(samples[i]).toBeGreaterThanOrEqual(samples[i - 1])
    }
  })

  it('P1 直传原样使用节点百分比（节点数字本来就是采集 + 打包 + 发送）', () => {
    expect(
      computeOverallPercent({ stage: 'uploading', nodePercent: 85, deliveryPercent: 0, direct: true }),
    ).toBe(85)
    expect(computeOverallPercent({ stage: 'collecting', nodePercent: 100, direct: true })).toBe(100)
  })

  it('pulling 一定是平台中转，即使 direct 仍为真（P1 降级 P2 后 remote_url 未清）', () => {
    expect(
      computeOverallPercent({ stage: 'pulling', nodePercent: 100, deliveryPercent: 0, direct: true }),
    ).toBe(COLLECT_BAND_END)
  })

  it('缺省 / 越界 / 非数字一律夹到 0-100', () => {
    expect(computeOverallPercent({ stage: 'pulling', deliveryPercent: -20 })).toBe(COLLECT_BAND_END)
    expect(computeOverallPercent({ stage: 'pulling', deliveryPercent: 500 })).toBe(DELIVERY_BAND_END)
    expect(computeOverallPercent({ stage: 'collecting', nodePercent: Number.NaN })).toBe(0)
    expect(computeOverallPercent({ stage: 'collecting' })).toBe(0)
    expect(clampPercent('42')).toBe(42)
    expect(clampPercent(undefined)).toBe(0)
  })
})

describe('formatByteCount 进度文案口径', () => {
  it('小于 1 KB 保留字节原值，不四舍五入成 0 KB', () => {
    expect(formatByteCount(0)).toBe('0 字节')
    expect(formatByteCount(900)).toBe('900 字节')
  })

  it('按量级选单位，避免「1129000 字节」这种无信息量的原文', () => {
    expect(formatByteCount(2048)).toBe('2.0 KB')
    expect(formatByteCount(3 * 1024 * 1024)).toBe('3.0 MB')
    expect(formatByteCount(2.5 * 1024 * 1024 * 1024)).toBe('2.5 GB')
    expect(formatByteCount(150 * 1024 * 1024 * 1024)).toBe('150 GB')
    expect(formatByteCount(undefined)).toBe('0 字节')
  })
})
