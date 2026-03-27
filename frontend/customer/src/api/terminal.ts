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
