import { describe, expect, it } from 'vitest'

import { buildAgentExecProcessMessage } from '../terminal'

describe('buildAgentExecProcessMessage', () => {
  it('把超时、宿主机容器和安全行筛选规格传给 terminal_bridge', () => {
    const message = JSON.parse(buildAgentExecProcessMessage(
      'Q2026072747493',
      'exec-lsof-1',
      'acli system lsof',
      '172.28.24.4',
      'host',
      120,
      'trace-1',
      [{
        source: 'stdout',
        include: ['4359974862144'],
        exclude: [],
        include_mode: 'all',
        case_sensitive: true,
      }],
    ))

    expect(message).toEqual({
      type: 'ssh_exec_process',
      case_id: 'Q2026072747493',
      exec_id: 'exec-lsof-1',
      command: 'acli system lsof',
      node_ip: '172.28.24.4',
      container: 'host',
      timeout: 120,
      trace_id: 'trace-1',
      output_filters: [{
        source: 'stdout',
        include: ['4359974862144'],
        exclude: [],
        include_mode: 'all',
        case_sensitive: true,
      }],
    })
  })
})
