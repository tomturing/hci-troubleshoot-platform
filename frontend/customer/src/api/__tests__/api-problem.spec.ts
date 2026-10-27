/**
 * 错误提示契约：不允许把后端 problem 退化成
 * `Request failed with status code 500`。
 *
 * 现场（2026-10-27 Q2026102701294）：上传接口抛 PermissionError，
 * 前端只显示 axios 默认消息，工单里既看不出是存储、权限还是网络。
 */

import { createApiClient, extractApiProblem, formatApiProblem } from '@hci/shared'
import { AxiosError } from 'axios'
import { describe, expect, it } from 'vitest'

const STORAGE_PROBLEM = {
  type: 'urn:hci:log-collect:error:LC-PLATFORM-007',
  title: '日志存储目录不可写',
  status: 503,
  code: 'LC-PLATFORM-007',
  layer: 'PLATFORM',
  owner: 'platform',
  retryable: 'manual',
  detail:
    '日志存储目录不可写：/var/lib/log-collect（运行用户 uid=1000，原因：Permission denied）。' +
    '平台需要该目录保存日志包分片与成品；请确认容器已挂载可写卷、目录属主与权限（fsGroup=1000）正确后重试。',
  user_hint: '平台日志存储目录不可写，请联系运维确认容器已挂载可写卷后重试。',
}

describe('formatApiProblem', () => {
  it('输出「码 + 标题 + 具体原因 + 建议」', () => {
    const text = formatApiProblem(STORAGE_PROBLEM)
    expect(text).toContain('[LC-PLATFORM-007]')
    expect(text).toContain('/var/lib/log-collect')
    expect(text).toContain('uid=1000')
    expect(text).toContain('建议：')
  })

  it('detail 已以标题开头时不重复标题', () => {
    const text = formatApiProblem(STORAGE_PROBLEM)
    expect(text).not.toContain('日志存储目录不可写 日志存储目录不可写')
    expect(text.startsWith('[LC-PLATFORM-007] 日志存储目录不可写：')).toBe(true)
  })

  it('没有标题/建议时只输出码与详情', () => {
    expect(formatApiProblem({ code: 'LC-ENV-003', detail: 'acli 未安装' })).toBe(
      '[LC-ENV-003] acli 未安装',
    )
  })

  it('全部字段缺失时退回请求编号，而不是空串', () => {
    expect(formatApiProblem({ request_id: 'req-1' })).toContain('req-1')
  })
})

describe('extractApiProblem', () => {
  it('识别 case-service 的 RFC 7807 detail', () => {
    expect(extractApiProblem({ detail: STORAGE_PROBLEM })?.code).toBe('LC-PLATFORM-007')
  })

  it('识别 FastAPI 字符串 detail', () => {
    expect(extractApiProblem({ detail: 'Not Found' })).toEqual({ detail: 'Not Found' })
  })

  it('识别 shared 兜底 error.message 结构', () => {
    const problem = extractApiProblem({
      error: { code: 'INTERNAL_ERROR', message: '服务内部错误（POST /x）', request_id: 'req-2' },
    })
    expect(problem?.code).toBe('INTERNAL_ERROR')
    expect(problem?.detail).toContain('POST /x')
    expect(problem?.request_id).toBe('req-2')
  })

  it('非结构化响应返回 null（保持 axios 原行为）', () => {
    expect(extractApiProblem('<html>502 Bad Gateway</html>')).toBeNull()
    expect(extractApiProblem(undefined)).toBeNull()
  })
})

/** 模拟 axios 内置 adapter 的失败语义：抛带 response 的 AxiosError。 */
function failingAdapter(status: number, data: unknown) {
  return async (config: any) => {
    const response = { data, status, statusText: 'Error', headers: {}, config }
    throw new AxiosError(
      `Request failed with status code ${status}`,
      'ERR_BAD_RESPONSE',
      config,
      undefined,
      response,
    )
  }
}

describe('createApiClient 错误增强', () => {
  it('把 503 problem 提升为可读 message 并挂到 error.problem', async () => {
    const client = createApiClient('/api')
    client.defaults.adapter = failingAdapter(503, { detail: STORAGE_PROBLEM })

    await expect(client.get('/log-collect/tasks/x/uploads')).rejects.toMatchObject({
      message: expect.stringContaining('[LC-PLATFORM-007]'),
      problem: expect.objectContaining({ code: 'LC-PLATFORM-007' }),
    })
  })

  it('后端无结构化 problem 时不改写 axios 原始消息（防回归）', async () => {
    const client = createApiClient('/api')
    client.defaults.adapter = failingAdapter(500, 'oops')

    await expect(client.get('/x')).rejects.toMatchObject({
      message: expect.stringContaining('Request failed with status code 500'),
    })
  })
})
