/**
 * terminal.ts
 * Bridge 模式：SSH 连接完全在 Windows 本地发起
 * 浏览器通过 ws://localhost:9999 与本地 terminal_bridge.exe 通信
 * 公网服务器不参与任何 SSH 流量
 */

const BRIDGE_URL = 'ws://localhost:9999'
const BRIDGE_CHECK_TIMEOUT = 1500

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
  case_id?: string
  output?: string
  message?: string
  detail?: string
}

/**
 * 检测本地 Bridge 是否在运行
 * 通过尝试建立 WebSocket 连接来判断
 */
export function checkBridgeRunning(): Promise<boolean> {
  return new Promise((resolve) => {
    const probe = new WebSocket(BRIDGE_URL)
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
    const probe = new WebSocket(BRIDGE_URL)
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
  return new WebSocket(BRIDGE_URL)
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
