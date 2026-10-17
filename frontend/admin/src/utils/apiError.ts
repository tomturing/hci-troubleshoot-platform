// 后端错误响应 → 可读文案的统一解析（纯前端逻辑，便于单测）。
//
// 背景：FastAPI 的 HTTPException.detail 既可能是字符串，也可能是结构化对象
// （如 422 {code, message, review:{issues:[...]}}）。若直接 `new Error(errData.detail)`，
// 对象会被渲染成 [object Object]，把真实失败原因（信号审查被拦、候选全拒等）整个吞掉。
// 这里按优先级提取人类可读信息，兜底才回退到 HTTP 状态码。

export function resolveApiErrorMessage(errData: unknown, status: number): string {
  const detail = (errData as { detail?: unknown })?.detail
  if (typeof detail === 'string' && detail.trim()) {
    return detail
  }
  if (detail && typeof detail === 'object') {
    const record = detail as {
      message?: unknown
      review?: { issues?: Array<{ message?: unknown }> }
    }
    const reviewMsg = Array.isArray(record.review?.issues)
      ? record.review!.issues!.map((issue) => issue?.message).filter((m): m is string => Boolean(m)).join('；')
      : ''
    const message = typeof record.message === 'string' && record.message.trim() ? record.message : ''
    return message || reviewMsg || JSON.stringify(detail)
  }
  return `HTTP ${status}`
}
