import { afterEach, describe, expect, it } from 'vitest'

import { buildAgentExecProcessMessage, getBridgeExecWaitTimeoutMs, getBridgeUrl } from '../terminal'

describe('getBridgeUrl', () => {
  afterEach(() => {
    delete window.__HCI_RUNTIME_CONFIG__
  })

  it('未注入运行时配置时保持 Windows 桌面 Bridge 地址', () => {
    expect(getBridgeUrl()).toBe('ws://localhost:9999')
  })

  it('将集群相对路径解析为当前页面的同源 WebSocket 地址', () => {
    window.__HCI_RUNTIME_CONFIG__ = { terminalBridgeUrl: '/terminal-bridge' }

    const expectedProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    expect(getBridgeUrl()).toBe(`${expectedProtocol}//${window.location.host}/terminal-bridge`)
  })

  it('将 HTTP 地址转换成 WebSocket 协议', () => {
    window.__HCI_RUNTIME_CONFIG__ = { terminalBridgeUrl: 'https://bridge.example.test/ws' }

    expect(getBridgeUrl()).toBe('wss://bridge.example.test/ws')
  })

  it('浏览器等待窗口比 Bridge 权威超时多 15 秒', () => {
    window.__HCI_RUNTIME_CONFIG__ = { terminalBridgeExecTimeoutSeconds: 200 }

    expect(getBridgeExecWaitTimeoutMs()).toBe(215_000)
  })

  it('无效超时配置回退到默认 120 秒', () => {
    window.__HCI_RUNTIME_CONFIG__ = { terminalBridgeExecTimeoutSeconds: 'invalid' }

    expect(getBridgeExecWaitTimeoutMs()).toBe(135_000)
  })

  it('Agent 隔离执行帧原样保留 W3C traceparent', () => {
    const traceId = 'caa7e3e825ba4a606df189740be1118c'
    const traceparent = `00-${traceId}-cbef2f8fb7e2d3a8-03`
    const message = JSON.parse(
      buildAgentExecProcessMessage(
        'Q2026072709403',
        '3678acb4-76d5-42a1-9b7f-1ca5f0ee3858',
        'uname -a',
        undefined,
        undefined,
        traceId,
        traceparent,
        '2df15cdf-9768-4466-93e8-c7f1daf5c28d',
      ),
    )

    expect(message.trace_id).toBe(traceId)
    expect(message.traceparent).toBe(traceparent)
  })

  it('把超时、宿主机容器和安全行筛选规格传给 terminal_bridge', () => {
    const message = JSON.parse(buildAgentExecProcessMessage(
      'Q2026072747493',
      'exec-lsof-1',
      'acli system lsof',
      '172.28.24.4',
      'host',
      'trace-1',
      undefined,
      undefined,
      undefined,
      120,
      [{
        source: 'stdout',
        include: ['4359974862144'],
        exclude: [],
        include_mode: 'all',
        case_sensitive: true,
      }],
    ))

    expect(message.timeout).toBe(120)
    expect(message.output_filters[0].include).toEqual(['4359974862144'])
  })
})
