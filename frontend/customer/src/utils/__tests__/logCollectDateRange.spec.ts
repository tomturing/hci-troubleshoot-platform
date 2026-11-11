/**
 * 时间范围契约的单测。
 *
 * 回归来源：2026-11-11 Q2026111100469 —— 客户选了 2026-05-30 ~ 2026-11-11
 * （166 天），点「开始收集」被服务端以 422 拒绝，界面只显示
 * `Request failed with status code 422`，既不知道错在哪也不知道上限是多少。
 */

import { describe, expect, it } from 'vitest'
import {
  MAX_DATE_RANGE_DAYS,
  checkDateRange,
  dateRangeSpanDays,
} from '@/utils/logCollectDateRange'

describe('dateRangeSpanDays', () => {
  it('同一天算 1 天（含首尾）', () => {
    expect(dateRangeSpanDays('2026-11-11', '2026-11-11')).toBe(1)
  })

  it('跨月边界按日历天数计算', () => {
    expect(dateRangeSpanDays('2026-10-31', '2026-11-01')).toBe(2)
  })

  it('跨夏令时切换不因时区差一小时而错算一天', () => {
    // 2026-03-08 是美国夏令时切换日；本地时区相减会得到 29.958 天
    expect(dateRangeSpanDays('2026-03-01', '2026-03-30')).toBe(30)
  })

  it('现场跨度可复现（166 天）', () => {
    expect(dateRangeSpanDays('2026-05-30', '2026-11-11')).toBe(166)
  })
})

describe('checkDateRange', () => {
  it('上限边界值 30 天放行', () => {
    const result = checkDateRange('2026-11-01', '2026-11-30')
    expect(result).toEqual({ ok: true, days: MAX_DATE_RANGE_DAYS })
  })

  it('31 天拒绝，并给出实际天数与上限', () => {
    const result = checkDateRange('2026-11-01', '2026-12-01')
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.reason).toContain('31 天')
    expect(result.reason).toContain('30 天')
  })

  it('现场跨度（166 天）拒绝', () => {
    const result = checkDateRange('2026-05-30', '2026-11-11')
    expect(result.ok).toBe(false)
  })

  it('结束早于开始时拒绝，而不是报成"跨度超限"', () => {
    const result = checkDateRange('2026-11-11', '2026-11-10')
    expect(result.ok).toBe(false)
    if (result.ok) return
    expect(result.reason).toContain('不合法')
  })

  it('非法日期串拒绝（防止脏回填把 NaN 当合法）', () => {
    expect(checkDateRange('', '2026-11-11').ok).toBe(false)
    expect(checkDateRange('2026-11-11', 'not-a-date').ok).toBe(false)
  })

  it('可显式放宽上限（供将来配置化）', () => {
    expect(checkDateRange('2026-11-01', '2026-11-30', 60).ok).toBe(true)
  })
})
