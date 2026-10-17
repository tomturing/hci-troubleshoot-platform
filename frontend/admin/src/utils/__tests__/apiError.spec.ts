// resolveApiErrorMessage 纯逻辑单测：确保后端结构化 detail 不再被渲染成 [object Object]。
import { describe, expect, it } from 'vitest'
import { resolveApiErrorMessage } from '../apiError'

describe('resolveApiErrorMessage', () => {
  it('detail 为字符串时直接返回', () => {
    expect(resolveApiErrorMessage({ detail: '服务未就绪' }, 503)).toBe('服务未就绪')
  })

  it('detail 对象优先取 message（回归：不再出现 [object Object]）', () => {
    const errData = {
      detail: {
        code: 'SIGNAL_ALL_REJECTED',
        message: '模型产出的 4 条候选信号全部未通过契约/门禁校验',
        review: { issues: [{ message: '无关问题' }] },
      },
    }
    expect(resolveApiErrorMessage(errData, 422)).toBe('模型产出的 4 条候选信号全部未通过契约/门禁校验')
  })

  it('detail 对象无 message 时回退到 review.issues 文案', () => {
    const errData = {
      detail: {
        code: 'SIGNAL_REVIEW_BLOCKED',
        review: { issues: [{ message: '画像变更后正反例不再通过' }, { message: '命令不在 catalog' }] },
      },
    }
    expect(resolveApiErrorMessage(errData, 422)).toBe('画像变更后正反例不再通过；命令不在 catalog')
  })

  it('detail 对象既无 message 也无 issues 文案时 JSON 兜底', () => {
    const errData = { detail: { code: 'X', retryable: true } }
    expect(resolveApiErrorMessage(errData, 422)).toBe(JSON.stringify(errData.detail))
  })

  it('空/缺失 detail 回退到 HTTP 状态码', () => {
    expect(resolveApiErrorMessage({}, 500)).toBe('HTTP 500')
    expect(resolveApiErrorMessage(undefined, 0)).toBe('HTTP 0')
    expect(resolveApiErrorMessage({ detail: '   ' }, 404)).toBe('HTTP 404')
  })
})
