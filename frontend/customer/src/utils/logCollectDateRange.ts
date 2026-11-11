/**
 * 日志收集时间范围的提交前契约（纯函数）。
 *
 * 上限与 case-service 的 `MAX_DATE_RANGE_DAYS` 同源
 * （backend/case-service/app/schemas/log_collect.py）。前端这份只负责"提前告知"，
 * 权威判定始终在服务端：两处不一致时以服务端为准，且服务端 422 的原因
 * 现在已经能透出到界面（见 shared/api.ts 的数组 detail 解析）。
 *
 * 2026-11-11 Q2026111100469：客户选了 2026-05-30 ~ 2026-11-11（166 天），
 * 界面没有任何说明，点「开始收集」只弹 `Request failed with status code 422`。
 */

export const MAX_DATE_RANGE_DAYS = 30

/**
 * 起止日期（含首尾）的天数。
 *
 * 按 UTC 解析日期分量：`new Date('2026-03-01')` 在部分时区会落到前一天，
 * 用本地时区相减还会在夏令时切换那天差出一小时。日期是纯日历概念、不含时刻，
 * 只有 UTC 计算才与"选了几天"的直觉一致。
 */
export function dateRangeSpanDays(from: string, to: string): number {
  const parse = (value: string): number => {
    const [year, month, day] = value.split('-').map(Number)
    return Date.UTC(year, month - 1, day)
  }
  return Math.round((parse(to) - parse(from)) / 86400000) + 1
}

export type DateRangeCheck =
  | { ok: true; days: number }
  | { ok: false; reason: string }

/**
 * 提交前校验。
 *
 * 返回可直接展示给客户的拒绝原因：文案只在这里定义，避免组件与测试各写一份
 * 之后漂移。`ok` 为真时同时给出天数，供调用方记日志或展示。
 */
export function checkDateRange(
  from: string,
  to: string,
  maxDays: number = MAX_DATE_RANGE_DAYS,
): DateRangeCheck {
  const days = dateRangeSpanDays(from, to)
  if (!Number.isFinite(days) || days <= 0) {
    return { ok: false, reason: '时间范围不合法，请重新选择开始与结束日期' }
  }
  if (days > maxDays) {
    return {
      ok: false,
      reason: `时间范围跨 ${days} 天，超过单次上限 ${maxDays} 天，请缩小范围后重试`,
    }
  }
  return { ok: true, days }
}
