/**
 * terminal.ts
 * Bridge 双运行模式：
 * - desktop：浏览器通过 ws://localhost:9999 连接 Windows terminal_bridge.exe
 * - cluster：浏览器通过同源 /terminal-bridge 连接 K3s Pod
 * 两种模式使用相同 WebSocket 协议和同一套 Go 代码。
 */

const DEFAULT_BRIDGE_URL = 'ws://localhost:9999'
// 桌面模式下与 terminal_bridge Go 端内置常量一致的两个已知端口：
// 主端口被陌生进程占用时 Bridge 会回退到备用端口，浏览器只需探测这两个端口即可定位，
// 无需服务端端口发现通道。
const PRIMARY_BRIDGE_PORT = 9999
const BACKUP_BRIDGE_PORT = 47324
const BRIDGE_CHECK_TIMEOUT = 1500
const BRIDGE_PROBE_TIMEOUT = 1500
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

// 桌面模式端口探测结果缓存：记住“上一次确认存活的 Bridge 地址”，
// 让后续连接与错误文案复用同一端口，避免每次双端口探测。cluster（有注入配置）不写缓存。
let bridgeUrlCache: string | null = null

function readConfiguredBridgeUrl(): string {
  if (typeof window === 'undefined') return ''
  return window.__HCI_RUNTIME_CONFIG__?.terminalBridgeUrl?.trim() || ''
}

function normalizeConfiguredBridgeUrl(configured: string): string {
  if (configured.startsWith('/')) {
    const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:'
    return `${protocol}//${window.location.host}${configured}`
  }
  if (configured.startsWith('https://')) return `wss://${configured.slice('https://'.length)}`
  if (configured.startsWith('http://')) return `ws://${configured.slice('http://'.length)}`
  return configured
}

function desktopBridgeUrl(port: number): string {
  return `ws://localhost:${port}`
}

/**
 * 同步获取当前 Bridge WebSocket 地址。
 * - 注入配置（cluster）：原样解析，行为不变。
 * - 桌面：优先返回探测缓存（上一次确认存活的端口），否则回退默认 9999，
 *   保持既有“期望 9999”的错误文案与同步签名，供 `${getBridgeUrl()}` 类调用点使用。
 */
export function getBridgeUrl(): string {
  const configured = readConfiguredBridgeUrl()
  if (configured) return normalizeConfiguredBridgeUrl(configured)
  return bridgeUrlCache ?? DEFAULT_BRIDGE_URL
}

/** 清除桌面探测缓存（连接确认失效或需要重新发现端口时调用）。 */
export function resetBridgeUrlCache(): void {
  bridgeUrlCache = null
}

/** 从 WebSocket 消息中解析 type 字段（非 JSON 或缺字段返回空串）。 */
function parseBridgeMessageType(data: unknown): string {
  if (typeof data !== 'string') return ''
  try {
    const parsed = JSON.parse(data) as { type?: unknown }
    return typeof parsed?.type === 'string' ? parsed.type : ''
  } catch {
    return ''
  }
}

/**
 * 探测给定地址上是否为“存活的 terminal_bridge”。
 * 关键：不能只看 WS 能否 open（占用主端口的陌生进程同样能让 WS 打开），
 * 必须收到 Bridge 可识别的回包（连接即下发的 bridge_hello，或 ping/pong/bridge_ready）
 * 才判定为存活 Bridge。任何超时/错误/关闭均视为非存活。
 */
function probeLiveBridge(url: string, timeoutMs: number): Promise<boolean> {
  return new Promise((resolve) => {
    let settled = false
    let ws: WebSocket
    try {
      ws = new WebSocket(url)
    } catch {
      resolve(false)
      return
    }
    const finish = (live: boolean) => {
      if (settled) return
      settled = true
      clearTimeout(timer)
      try {
        if (ws && typeof ws.close === 'function') ws.close()
      } catch { /* 忽略关闭异常 */ }
      resolve(live)
    }
    const timer = setTimeout(() => finish(false), timeoutMs)
    ws.onmessage = (event) => {
      const type = parseBridgeMessageType(event?.data)
      if (type === 'bridge_hello' || type === 'ping' || type === 'pong' || type === 'bridge_ready') {
        finish(true)
      }
    }
    ws.onerror = () => finish(false)
    ws.onclose = () => finish(false)
  })
}

/**
 * 解析桌面模式下 Bridge 的真实地址：
 * - 有注入配置（cluster）→ 原样返回，零探测，行为不变。
 * - 命中缓存 → 直接返回（避免重复双端口探测）。
 * - 否则按主 9999 → 备 47324 顺序探测“存活 Bridge”，命中即缓存；都不可用则返回默认 9999
 *   但**不缓存**（保持既有报错文案，且下次仍会重新发现）。
 */
export async function resolveBridgeUrl(): Promise<string> {
  const configured = readConfiguredBridgeUrl()
  if (configured) return normalizeConfiguredBridgeUrl(configured)
  if (bridgeUrlCache) return bridgeUrlCache

  const primary = desktopBridgeUrl(PRIMARY_BRIDGE_PORT)
  if (await probeLiveBridge(primary, BRIDGE_PROBE_TIMEOUT)) {
    bridgeUrlCache = primary
    return primary
  }
  const backup = desktopBridgeUrl(BACKUP_BRIDGE_PORT)
  if (await probeLiveBridge(backup, BRIDGE_PROBE_TIMEOUT)) {
    bridgeUrlCache = backup
    return backup
  }
  return DEFAULT_BRIDGE_URL
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
  | 'bridge_hello' // 连接握手首包：供端口冲突探测识别对端确为 terminal_bridge
  | 'ping'         // WebSocket 保活心跳（服务端发送）
  | 'pong'
  | 'bridge_ready'
  | 'exec_result'  // T-TOOL-01: Agent 命令执行结果
  | 'exec_stdout'  // 双通道：隔离通道 stdout
  | 'exec_stderr'  // 双通道：隔离通道 stderr
  | 'bridge_log'   // terminal_bridge 结构化回采日志（OBS-TERMINAL-BRIDGE-001）
  | 'vm_console_result'  // qkv_vm_console 固定操作元数据结果（不含图片字节）
  | 'acli_sync_progress' // acli 检查/安装进度反馈
  | 'acli_sync_result'   // acli 检查/安装最终结果
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
  // acli_sync 专用字段
  status?: string
  architecture?: string
  current_version?: string
  latest_version?: string
  available_mb?: number
  required_mb?: number
  // WebSocket 保活心跳字段
  ping?: number     // 服务端发送心跳时间戳（Unix 毫秒）
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
 * 检测当前配置的 Bridge 是否在运行。
 * 先经 resolveBridgeUrl 定位真实端口（桌面双端口探测/缓存），再以“存活 Bridge”
 * 回包为准确认；若目标已不可用则清除缓存，下次重新发现端口。
 */
export async function checkBridgeRunning(): Promise<boolean> {
  const url = await resolveBridgeUrl()
  const live = await probeLiveBridge(url, BRIDGE_CHECK_TIMEOUT)
  if (!live) resetBridgeUrlCache()
  return live
}

/**
 * 前置 Bridge 检测（弹框弹出前调用）
 * 3s 超时，返回 'running' | 'not-running' 字符串状态
 * 供 CaseCreateDialog 和 SshConnectDialog 共用
 */
export async function checkBridgeBeforeOpen(timeoutMs = 3000): Promise<'running' | 'not-running'> {
  const url = await resolveBridgeUrl()
  const live = await probeLiveBridge(url, timeoutMs)
  if (!live) resetBridgeUrlCache()
  return live ? 'running' : 'not-running'
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

export interface AcliSyncOptions {
  force?: boolean
  minDiskMb?: number
  acliUrl?: string
}

/**
 * 构建 acli_sync 消息（检测与自动更新 acli 工具）
 */
export function buildAcliSyncMessage(caseId: string, options?: AcliSyncOptions): string {
  return JSON.stringify({
    type: 'acli_sync',
    case_id: caseId,
    force: options?.force || false,
    min_disk_mb: options?.minDiskMb || 100,
    acli_url: options?.acliUrl || undefined,
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
 * 构造虚拟机控制台固定操作的 WebSocket 消息（vm_console_op 类型）。
 *
 * 与 ssh_exec_process 的根本差异：不携带自由文本命令——operation 仅允许
 * capture_baseline / wake_down_key，vtpsh 等价操作由 Bridge 内部固定常量构造。
 * 二进制 PPM 由 Bridge 直传平台制品端点，WS 只回传元数据（vm_console_result）。
 */
export function buildVmConsoleOpMessage(
  caseId: string,
  captureId: string,
  execId: string,
  operation: 'capture_baseline' | 'wake_down_key',
  hostNodeId: string,
  vmId: string,
  options?: {
    nodeIp?: string | null
    timeoutSeconds?: number | null
    role?: 'baseline' | 'recapture' | 'wake' | null
    artifactPolicy?: string | null
    catalogRevision?: string | null
    traceId?: string | null
    traceparent?: string | null
    conversationId?: string | null
  },
): string {
  return JSON.stringify({
    type: 'vm_console_op',
    case_id: caseId,
    capture_id: captureId,
    exec_id: execId,
    operation,
    host_node_id: hostNodeId,
    vm_id: vmId,
    node_ip: options?.nodeIp || undefined,
    timeout_seconds: options?.timeoutSeconds || undefined,
    role: options?.role || undefined,
    artifact_policy: options?.artifactPolicy || undefined,
    catalog_revision: options?.catalogRevision || undefined,
    trace_id: options?.traceId || undefined,
    traceparent: options?.traceparent || undefined,
    conversation_id: options?.conversationId || undefined,
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
