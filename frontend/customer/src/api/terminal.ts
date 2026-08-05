/**
 * terminal.ts
 * Bridge 双运行模式：
 * - desktop：浏览器通过 ws://localhost:9999 连接 Windows terminal_bridge.exe
 * - cluster：浏览器通过同源 /terminal-bridge 连接 K3s Pod
 * 两种模式使用相同 WebSocket 协议和同一套 Go 代码。
 */

const DEFAULT_BRIDGE_URL = 'ws://localhost:9999'
const BRIDGE_CHECK_TIMEOUT = 1500
const DEFAULT_BRIDGE_EXEC_TIMEOUT_SECONDS = 120
const BRIDGE_RESULT_TRANSPORT_GRACE_SECONDS = 15

declare global {
  interface Window {
    __HCI_RUNTIME_CONFIG__?: {
      terminalBridgeUrl?: string
      terminalBridgeExecTimeoutSeconds?: number | string
    }
  }
}

/**
 * 获取当前 Bridge WebSocket 地址。
 * Helm 通过 nginx 的 runtime-config.js 注入集群路径；未配置时保持 Windows localhost 行为。
 */
export function getBridgeUrl(): string {
  const configured =
    typeof window === 'undefined'
      ? ''
      : window.__HCI_RUNTIME_CONFIG__?.terminalBridgeUrl?.trim() || ''
  if (!configured) return DEFAULT_BRIDGE_URL

  if (configured.startsWith('/')) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}${configured}`
  }
  if (configured.startsWith('https://')) return `wss://${configured.slice('https://'.length)}`
  if (configured.startsWith('http://')) return `ws://${configured.slice('http://'.length)}`
  return configured
}

/**
 * 返回浏览器等待 Bridge 权威执行结果的窗口。
 * 浏览器必须晚于 Bridge 超时，预留 15 秒供 SSH 关闭、WebSocket 发送和事件循环调度。
 */
export function getBridgeExecWaitTimeoutMs(): number {
  const configured =
    typeof window === 'undefined'
      ? Number.NaN
      : Number(window.__HCI_RUNTIME_CONFIG__?.terminalBridgeExecTimeoutSeconds)
  const bridgeTimeoutSeconds =
    Number.isFinite(configured) && configured > 0
      ? configured
      : DEFAULT_BRIDGE_EXEC_TIMEOUT_SECONDS
  return (bridgeTimeoutSeconds + BRIDGE_RESULT_TRANSPORT_GRACE_SECONDS) * 1000
}

export type BridgeStatus = 'checking' | 'running' | 'not_running'

export type TerminalAuthType = 'password' | 'key'

export interface SshConnectOptions {
  host: string
  port?: number
  username: string
  auth_type: TerminalAuthType
  password?: string
  private_key?: string
  passphrase?: string
  case_id?: string
}

export interface TerminalWsMessage {
  type:
  | 'ssh_connected'
  | 'ssh_disconnected'
  | 'ssh_output'
  | 'ssh_error'
  | 'pong'
  | 'bridge_ready'
  | 'exec_result'  // T-TOOL-01: Agent 命令执行结果
  | 'exec_stdout'  // 双通道：隔离通道 stdout
  | 'exec_stderr'  // 双通道：隔离通道 stderr
  | 'bridge_log'   // terminal_bridge 结构化回采日志（OBS-TERMINAL-BRIDGE-001）
  case_id?: string
  output?: string
  message?: string
  detail?: string
  exec_id?: string  // exec_result 消息的执行 ID
  exit_code?: number  // exec_result 消息的退出码
  stdout?: string   // 双通道 stdout 字段
  stderr?: string   // 双通道 stderr 字段
  trace_id?: string // 端到端链路 ID（回显）
  traceparent?: string
  artifact_id?: string
  stdout_bytes?: number
  stderr_bytes?: number
  stdout_sha256?: string
  stderr_sha256?: string
  stdout_truncated?: boolean
  stderr_truncated?: boolean
  duration_ms?: number
  timed_out?: boolean
  cancelled?: boolean
  error_type?: string
  // bridge_log 消息的负载（结构化日志条目）
  seq?: number
  ts?: string
  level?: string
  event?: string
  custom_ui?: string
  node_ip?: string
  extra?: Record<string, unknown>
}

export interface OutputFilterSpec {
  source: 'stdout' | 'stderr'
  include: string[]
  exclude: string[]
  include_mode: 'all' | 'any'
  exclude_mode?: 'all' | 'any'
  case_sensitive: boolean
}

/**
 * 检测当前配置的 Bridge 是否在运行
 * 通过尝试建立 WebSocket 连接来判断
 */
export function checkBridgeRunning(): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = new WebSocket(getBridgeUrl())
    const timer = setTimeout(() => {
      probe.close()
      resolve(false)
    }, BRIDGE_CHECK_TIMEOUT)

    probe.onopen = () => {
      clearTimeout(timer)
      probe.close()
      resolve(true)
    }
    probe.onerror = () => {
      clearTimeout(timer)
      resolve(false)
    }
  })
}

/**
 * 前置 Bridge 检测（弹框弹出前调用）
 * 3s 超时，返回 'running' | 'not-running' 字符串状态
 * 供 CaseCreateDialog 和 SshConnectDialog 共用
 */
export async function checkBridgeBeforeOpen(timeoutMs = 3000): Promise<'running' | 'not-running'> {
  return new Promise((resolve) => {
    const probe = new WebSocket(getBridgeUrl())
    const timer = setTimeout(() => {
      probe.close()
      resolve('not-running')
    }, timeoutMs)

    probe.onopen = () => {
      clearTimeout(timer)
      probe.close()
      resolve('running')
    }
    probe.onerror = () => {
      clearTimeout(timer)
      probe.close()
      resolve('not-running')
    }
  })
}

/**
 * 创建 Bridge WebSocket 连接
 */
export function createBridgeSocket(): WebSocket {
  return new WebSocket(getBridgeUrl())
}

/**
 * 构建 ssh_connect 消息
 */
export function buildConnectMessage(options: SshConnectOptions): string {
  return JSON.stringify({
    type: 'ssh_connect',
    ...options,
  })
}

/**
 * 构建 ssh_inject_command 消息（AI 助手注入命令，不带 \n，等客户回车确认）
 */
export function buildInjectCommandMessage(caseId: string, command: string): string {
  return JSON.stringify({
    type: 'ssh_inject_command',
    case_id: caseId,
    command,
  })
}

/**
 * 构建 ssh_input 消息（键盘输入，包含回车）
 */
export function buildInputMessage(caseId: string, data: string): string {
  return JSON.stringify({
    type: 'ssh_input',
    case_id: caseId,
    data,
  })
}

/**
 * 构建 ssh_disconnect 消息
 */
export function buildDisconnectMessage(caseId: string): string {
  return JSON.stringify({
    type: 'ssh_disconnect',
    case_id: caseId,
  })
}

// ===== Bridge 命令协议辅助函数 =====
// 供 SshFlowPanel 等组件复用，避免在 store 外重复实现 marker 协议

export interface BridgeCommandResult {
  output: string
  exitCode: number
}

/** 转义正则特殊字符 */
export function escapeRegExp(value: string): string {
  return value.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')
}

/**
 * 构建命令完成标记（marker）
 * 格式：__HCI_DONE_{caseId}_{name}_{ts}__
 * 用于从持续输出的 WebSocket 流中精确识别命令结束位置
 */
export function buildBridgeMarker(caseId: string, name: string, index: number): string {
  const normalizedCaseId = caseId.replace(/[^a-zA-Z0-9]/g, '')
  const normalizedName = name.replace(/[^a-zA-Z0-9]/g, '_')
  return `__HCI_DONE_${normalizedCaseId}_${normalizedName}_${index}_${Date.now()}__`
}

/**
 * 构建带 marker 的命令 payload
 * 执行命令后打印 marker:exitCode，供接收端解析
 */
export function buildBridgeCommandPayload(command: string, marker: string): string {
  return `${command}; status=$?; printf '\\n${marker}:%s\\n' "$status"\n`
}

/**
 * 从输出 buffer 中解析命令结果（匹配 marker:exitCode）
 * @returns BridgeCommandResult | null（null 表示命令尚未完成）
 */
export function parseBridgeCommandResult(buffer: string, marker: string): BridgeCommandResult | null {
  const normalized = buffer.replace(/\r/g, '')
  const match = normalized.match(new RegExp(`${escapeRegExp(marker)}:(\\d+)`))
  if (!match || match.index === undefined) return null

  return {
    output: normalized.slice(0, match.index).trim(),
    exitCode: Number(match[1]),
  }
}

/**
 * 剥离 ANSI/VT100 转义码（终端颜色码等），保留纯文本
 * 避免 SSH 终端输出带控制序列时干扰 JSON 解析
 */
export function stripAnsi(output: string): string {
  // eslint-disable-next-line no-control-regex
  return output.replace(/\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~]|\][^\x07\x1b]*(?:\x07|\x1b\\))/g, '')
}

/** 解析 JSON 输出（acli --formatter json）*/
export function parseJsonOutput(output: string): unknown {
  const cleaned = stripAnsi(output)
  const trimmed = cleaned.trim()
  if (trimmed.startsWith('{') || trimmed.startsWith('[')) {
    try {
      return JSON.parse(trimmed)
    } catch { /* 继续尝试提取 */ }
  }

  const lastBrace = cleaned.lastIndexOf('}')
  const lastBracket = cleaned.lastIndexOf(']')

  if (lastBrace > lastBracket) {
    const firstBrace = cleaned.indexOf('{')
    if (firstBrace !== -1 && lastBrace > firstBrace) {
      try { return JSON.parse(cleaned.slice(firstBrace, lastBrace + 1)) } catch { /* fall through */ }
    }
  } else if (lastBracket !== -1) {
    const firstBracket = cleaned.indexOf('[')
    if (firstBracket !== -1 && lastBracket > firstBracket) {
      try { return JSON.parse(cleaned.slice(firstBracket, lastBracket + 1)) } catch { /* fall through */ }
    }
  }

  return null
}

// ===== Agent 命令执行辅助函数 =====
// 供 Agent 远程执行命令并解析结果

export function buildAgentExecMessage(
  caseId: string,
  execId: string,
  rawCommand: string,
  traceId?: string | null,
): string {
  const markerId = execId.replace(/-/g, '').substring(0, 16)
  const marker = `__EXEC_DONE_${markerId}`
  const command = `${rawCommand}; status=$?; printf '\\n${marker}:%s\\n' "$status"\n`
  return JSON.stringify({
    type: 'ssh_exec_command',
    case_id: caseId,
    exec_id: execId,
    command,
    trace_id: traceId || undefined,
  })
}

/**
 * 构造 Agent 隔离通道执行命令的 WebSocket 消息（ssh_exec_process 类型 - Scheme B）。
 * 不需要追加任何退出 Marker 字符串。
 */
export function buildAgentExecProcessMessage(
  caseId: string,
  execId: string,
  rawCommand: string,
  nodeIp?: string | null,
  container?: string | null,
  traceId?: string | null,
  traceparent?: string | null,
  conversationId?: string | null,
  toolCallId?: string | null,
  timeout?: number | null,
  outputFilters?: OutputFilterSpec[],
): string {
  return JSON.stringify({
    type: 'ssh_exec_process',
    case_id: caseId,
    exec_id: execId,
    command: rawCommand,
    node_ip: nodeIp || undefined,
    container: container || undefined,
    trace_id: traceId || undefined,
    traceparent: traceparent || undefined,
    conversation_id: conversationId || undefined,
    tool_call_id: toolCallId || undefined,
    timeout: timeout || undefined,
    output_filters: outputFilters?.length ? outputFilters : undefined,
  })
}

/**
 * 解析 exec_result 消息中的 output/stdout/stderr 和 exit_code。
 */
export function parseAgentExecResult(message: unknown): {
  execId: string
  output: string
  exitCode: number
  stdout?: string
  stderr?: string
  traceId?: string
  traceparent?: string
  artifactId?: string
  stdoutBytes?: number
  stderrBytes?: number
  stdoutSha256?: string
  stderrSha256?: string
  stdoutTruncated?: boolean
  stderrTruncated?: boolean
  durationMs?: number
  timedOut?: boolean
  cancelled?: boolean
  errorType?: string
} | null {
  if (typeof message !== 'object' || message === null) {
    return null
  }

  const msg = message as Record<string, unknown>
  if (msg.type !== 'exec_result') {
    return null
  }

  const execId = typeof msg.exec_id === 'string' ? msg.exec_id : undefined
  const output = typeof msg.output === 'string' ? msg.output : ''
  const exitCode = typeof msg.exit_code === 'number' ? msg.exit_code : 0
  const stdout = typeof msg.stdout === 'string' ? msg.stdout : undefined
  const stderr = typeof msg.stderr === 'string' ? msg.stderr : undefined

  if (!execId) {
    return null
  }

  return {
    execId,
    output,
    exitCode,
    stdout,
    stderr,
    traceId: typeof msg.trace_id === 'string' ? msg.trace_id : undefined,
    traceparent: typeof msg.traceparent === 'string' ? msg.traceparent : undefined,
    artifactId: typeof msg.artifact_id === 'string' ? msg.artifact_id : undefined,
    stdoutBytes: typeof msg.stdout_bytes === 'number' ? msg.stdout_bytes : undefined,
    stderrBytes: typeof msg.stderr_bytes === 'number' ? msg.stderr_bytes : undefined,
    stdoutSha256: typeof msg.stdout_sha256 === 'string' ? msg.stdout_sha256 : undefined,
    stderrSha256: typeof msg.stderr_sha256 === 'string' ? msg.stderr_sha256 : undefined,
    stdoutTruncated: msg.stdout_truncated === true,
    stderrTruncated: msg.stderr_truncated === true,
    durationMs: typeof msg.duration_ms === 'number' ? msg.duration_ms : undefined,
    timedOut: msg.timed_out === true,
    cancelled: msg.cancelled === true,
    errorType: typeof msg.error_type === 'string' ? msg.error_type : undefined,
  }
}
