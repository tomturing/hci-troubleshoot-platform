import { describe, expect, it } from 'vitest'

import { normalizeToolEvent } from '../toolEvent'

describe('normalizeToolEvent', () => {
  it('把旧 tool_args/tool_result/completed 协议归一化为卡片标准字段', () => {
    expect(normalizeToolEvent({
      exec_id: 'exec-1',
      tool_args: { keyword: '启动虚拟机', limit: 1 },
      tool_result: 'matched=1',
      status: 'completed',
    })).toMatchObject({
      args: { keyword: '启动虚拟机', limit: 1 },
      result: 'matched=1',
      status: 'success',
    })
  })

  it('新协议优先于兼容别名并把 error 状态归一化为 failed', () => {
    expect(normalizeToolEvent({
      args: { command: 'lsof' },
      tool_args: { command: 'old' },
      result: 'new',
      tool_result: 'old',
      status: 'error',
    })).toMatchObject({
      args: { command: 'lsof' },
      result: 'new',
      status: 'failed',
    })
  })
})
