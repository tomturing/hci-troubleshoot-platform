/**
 * 管理台浏览器 ↔ terminal_bridge 的插件部署通道。
 *
 * 与 customer-ui 的 `acli_sync` / `log_collect` **完全同一模型**：页面自己连桥、
 * 自己在「已登录的 SSH 会话」上执行命令。服务端只负责生成部署命令与规格摘要，
 * 不持有桥地址、不持有任何 SSH 凭据，也**从不主动连接桥**。
 *
 * 因此这里出现 `ws://`、`ssh_exec_process` 是契约内的正常形态；
 * 唯一的红线是页面**不得**出现口令字段，也不得发送「新建连接」类消息：
 * 页面只复用已有会话，绝不新建 SSH 连接。
 */
import type { AcliPluginSession } from './acliPlugin'

declare global {
  interface Window {
    __HCI_RUNTIME_CONFIG__?: {
      terminalBridgeUrl?: string
      terminalBridgeExecTimeoutSeconds?: number | string
    }
  }
}

export interface DeployPlan {
  exec_id: string
  trace_id: string
  commands: string[]
  timeout_seconds: number
  case_id: string
  node_ip: string
}

export interface ExecOutcome {
  execId: string
  exitCode: number
  stdout: string
  stderr: string
  timedOut: boolean
  durationMs: number
  bridgeInstanceId: string
}

const DESKTOP_PORTS = [9999, 9998]

/** 页面可能跑在 http（非安全上下文），crypto.randomUUID 不可用，这里给出等价 trace_id。 */
function randomTraceId(): string {
  const bytes = new Uint8Array(16)
  window.crypto.getRandomValues(bytes)
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('')
}
const HANDSHAKE_TIMEOUT_MS = 10_000

function normalizeConfigured(url: string): string {
  if (url.startsWith('/')) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}${url}`
  }
  if (url.startsWith('https://')) return `wss://${url.slice('https://'.length)}`
  if (url.startsWith('http://')) return `ws://${url.slice('http://'.length)}`
  return url
}

/**
 * 候选桥地址（按优先级尝试）：
 * 1. 运行时注入的 `terminalBridgeUrl`（集群托管形态由部署侧注入）；
 * 2. 本机桌面桥 `ws://localhost:9999|9998`（客户机上同时开着 bridge.exe 时）；
 * 3. 同源 `/terminal-bridge`（集群托管形态下 nginx 反代到 terminal-bridge Service）。
 *
 * 3 兜底很关键：管理台镜像没有 runtime config 注入，缺了它集群形态永远连不上。
 */
function candidateBridgeUrls(): string[] {
  const configured = window.__HCI_RUNTIME_CONFIG__?.terminalBridgeUrl?.trim() || ''
  if (configured) return [normalizeConfigured(configured)]
  return [...DESKTOP_PORTS.map((port) => `ws://localhost:${port}`), normalizeConfigured('/terminal-bridge')]
}

interface BridgeConnection {
  ws: WebSocket
  instanceId: string
}

function openBridge(url: string, timeoutMs = HANDSHAKE_TIMEOUT_MS): Promise<BridgeConnection> {
  return new Promise((resolve, reject) => {
    let instanceId = ''
    const ws = new WebSocket(url)
    const timer = window.setTimeout(() => {
      ws.close()
      reject(new Error('bridge_connect_timeout'))
    }, timeoutMs)
    ws.onmessage = (event) => {
      try {
        const message = JSON.parse(String(event.data))
        if (message?.type === 'bridge_hello') {
          instanceId = String(message.detail || '')
          window.clearTimeout(timer)
          resolve({ ws, instanceId })
        }
      } catch {
        // 非 JSON 帧（终端输出）忽略
      }
    }
    ws.onerror = () => {
      window.clearTimeout(timer)
      reject(new Error('bridge_connect_failed'))
    }
  })
}

async function connectBridge(): Promise<BridgeConnection> {
  let lastError: unknown = null
  for (const url of candidateBridgeUrls()) {
    try {
      return await openBridge(url)
    } catch (error) {
      lastError = error
    }
  }
  throw lastError instanceof Error ? lastError : new Error('bridge_connect_failed')
}

function waitFor<T extends Record<string, unknown>>(
  ws: WebSocket,
  predicate: (message: Record<string, unknown>) => boolean,
  timeoutMs: number,
): Promise<T> {
  return new Promise((resolve, reject) => {
    const timer = window.setTimeout(() => {
      ws.removeEventListener('message', onMessage)
      reject(new Error('bridge_timeout'))
    }, timeoutMs)
    function onMessage(event: MessageEvent) {
      let message: Record<string, unknown>
      try {
        message = JSON.parse(String(event.data))
      } catch {
        return
      }
      if (message?.type === 'ssh_error') {
        window.clearTimeout(timer)
        ws.removeEventListener('message', onMessage)
        reject(new Error(String(message.message || 'ssh_error')))
        return
      }
      if (predicate(message)) {
        window.clearTimeout(timer)
        ws.removeEventListener('message', onMessage)
        resolve(message as T)
      }
    }
    ws.addEventListener('message', onMessage)
  })
}

/** 读取当前桥（= 浏览器能连到的那个桥）上已登录的 SSH 会话。 */
export async function listBridgeSessions(): Promise<AcliPluginSession[]> {
  const { ws } = await connectBridge()
  try {
    ws.send(JSON.stringify({ type: 'list_sessions', trace_id: randomTraceId() }))
    const reply = await waitFor<{ sessions?: AcliPluginSession[] }>(
      ws,
      (message) => message.type === 'list_sessions_result',
      HANDSHAKE_TIMEOUT_MS,
    )
    return (reply.sessions ?? []).map((item) => ({
      case_id: String(item.case_id || ''),
      node_ip: String(item.node_ip || ''),
      host: String(item.host || ''),
      key: String(item.key || ''),
    }))
  } finally {
    ws.close()
  }
}

/**
 * 在指定会话上逐条执行部署命令。
 *
 * 大脚本已被服务端切成多条命令，每条一条独立 WS 消息：单条 1MB 的命令
 * 会在 exec 通道卡死且拿不到 exec_result（历史故障）。
 */
export async function executeDeployPlan(
  plan: DeployPlan,
  onStep?: (index: number, total: number) => void,
): Promise<ExecOutcome> {
  const { ws, instanceId } = await connectBridge()
  const startedAt = Date.now()
  try {
    let last: Record<string, unknown> = {}
    for (let index = 0; index < plan.commands.length; index += 1) {
      onStep?.(index + 1, plan.commands.length)
      const execId = plan.commands.length > 1 ? `${plan.exec_id}-${index}` : plan.exec_id
      ws.send(
        JSON.stringify({
          type: 'ssh_exec_process',
          case_id: plan.case_id,
          node_ip: plan.node_ip,
          exec_id: execId,
          command: plan.commands[index],
          container: 'host',
          timeout: plan.timeout_seconds,
          trace_id: plan.trace_id,
        }),
      )
      last = await waitFor<Record<string, unknown>>(
        ws,
        (message) => message.type === 'exec_result' && message.exec_id === execId,
        (plan.timeout_seconds + 30) * 1000,
      )
    }
    return {
      execId: String(last.exec_id || plan.exec_id),
      exitCode: Number(last.exit_code ?? last.exitCode ?? -1),
      stdout: String(last.stdout || ''),
      stderr: String(last.stderr || ''),
      timedOut: Boolean(last.timed_out),
      durationMs: Date.now() - startedAt,
      bridgeInstanceId: instanceId,
    }
  } finally {
    ws.close()
  }
}
