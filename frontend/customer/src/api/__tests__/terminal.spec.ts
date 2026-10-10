import { afterEach, describe, expect, it, vi } from 'vitest'

import {
  buildAgentExecProcessMessage,
  checkBridgeBeforeOpen,
  checkBridgeRunning,
  getBridgeExecWaitTimeoutMs,
  getBridgeUrl,
  resetBridgeUrlCache,
  resolveBridgeUrl,
} from '../terminal'

describe('getBridgeUrl', () => {
  afterEach(() => {
    delete window.__HCI_RUNTIME_CONFIG__
  })

  it('未注入运行时配置时保持 Windows 桌面 Bridge 地址', () => {
    resetBridgeUrlCache()
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

const PRIMARY_URL = 'ws://localhost:9999'
const BACKUP_URL = 'ws://localhost:47324'

// 可控制的 WebSocket 替身：按 URL 决定行为——
// live：open 后立即下发 bridge_hello（确认为存活 Bridge）；
// openOnly：能 open 但无任何回包（模拟占用主端口的陌生进程）；
// dead：直接 onerror/onclose（端口不可达）。
type BridgeBehavior = 'live' | 'openOnly' | 'dead'

function createBridgeStub(behaviors: Record<string, BridgeBehavior>) {
  const constructed: string[] = []
  class FakeWebSocket {
    static readonly CONNECTING = 0
    static readonly OPEN = 1
    static readonly CLOSED = 3
    onopen: (() => void) | null = null
    onmessage: ((event: { data: string }) => void) | null = null
    onerror: (() => void) | null = null
    onclose: (() => void) | null = null
    readonly url: string
    constructor(url: string) {
      this.url = url
      constructed.push(url)
      const behavior = behaviors[url] ?? 'dead'
      setTimeout(() => {
        if (behavior === 'dead') {
          this.onerror?.()
          this.onclose?.()
          return
        }
        this.onopen?.()
        if (behavior === 'live') {
          this.onmessage?.({ data: JSON.stringify({ type: 'bridge_hello', message: 'test' }) })
        }
      }, 0)
    }
    close() {}
    send() {}
  }
  return { FakeWebSocket, constructed }
}

describe('terminal_bridge 端口冲突自愈探测', () => {
  afterEach(() => {
    vi.useRealTimers()
    vi.unstubAllGlobals()
    resetBridgeUrlCache()
    delete window.__HCI_RUNTIME_CONFIG__
  })

  it('无配置且主端口存活时解析为 ws://localhost:9999', async () => {
    vi.useFakeTimers()
    const { FakeWebSocket } = createBridgeStub({ [PRIMARY_URL]: 'live' })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const pending = resolveBridgeUrl()
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toBe('ws://localhost:9999')
  })

  it('主端口被陌生进程占用（可连但无 Bridge 回包）时回落到备用端口 47324', async () => {
    vi.useFakeTimers()
    const { FakeWebSocket, constructed } = createBridgeStub({
      [PRIMARY_URL]: 'openOnly',
      [BACKUP_URL]: 'live',
    })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const pending = resolveBridgeUrl()
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toBe('ws://localhost:47324')
    // 先探主端口，未确认存活才回落备用端口
    expect(constructed).toEqual([PRIMARY_URL, BACKUP_URL])
  })

  it('主备端口皆不可用时返回默认 9999 且不污染缓存', async () => {
    vi.useFakeTimers()
    const { FakeWebSocket } = createBridgeStub({})
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const pending = resolveBridgeUrl()
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toBe('ws://localhost:9999')
    expect(getBridgeUrl()).toBe('ws://localhost:9999')
  })

  it('注入集群配置时零探测直接解析同源地址', async () => {
    vi.useFakeTimers()
    const { FakeWebSocket, constructed } = createBridgeStub({})
    vi.stubGlobal('WebSocket', FakeWebSocket)
    window.__HCI_RUNTIME_CONFIG__ = { terminalBridgeUrl: '/terminal-bridge' }
    const pending = resolveBridgeUrl()
    await vi.runAllTimersAsync()
    const expectedProtocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    await expect(pending).resolves.toBe(`${expectedProtocol}//${window.location.host}/terminal-bridge`)
    expect(constructed).toEqual([])
  })

  it('checkBridgeBeforeOpen 命中存活 Bridge 返回 running 并预热缓存', async () => {
    vi.useFakeTimers()
    // 主端口未定义→dead；备用端口 live
    const { FakeWebSocket } = createBridgeStub({ [BACKUP_URL]: 'live' })
    vi.stubGlobal('WebSocket', FakeWebSocket)
    const pending = checkBridgeBeforeOpen()
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toBe('running')
    // 缓存记住备用端口，同步读取与后续连接复用同一端口
    expect(getBridgeUrl()).toBe('ws://localhost:47324')
  })

  it('目标不可用时 checkBridgeRunning 返回 false 并清除缓存', async () => {
    vi.useFakeTimers()
    // 先让主端口存活，写入缓存
    const liveStub = createBridgeStub({ [PRIMARY_URL]: 'live' })
    vi.stubGlobal('WebSocket', liveStub.FakeWebSocket)
    const warm = resolveBridgeUrl()
    await vi.runAllTimersAsync()
    await warm
    expect(getBridgeUrl()).toBe('ws://localhost:9999')
    // 随后 Bridge 关闭：所有端口均 dead
    vi.unstubAllGlobals()
    vi.stubGlobal('WebSocket', createBridgeStub({}).FakeWebSocket)
    const pending = checkBridgeRunning()
    await vi.runAllTimersAsync()
    await expect(pending).resolves.toBe(false)
    // 失效后清缓存，回到默认地址文案
    expect(getBridgeUrl()).toBe('ws://localhost:9999')
  })
})
