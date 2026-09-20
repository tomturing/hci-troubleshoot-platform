import { buildAcliSyncMessage, type AcliSyncOptions, type TerminalWsMessage } from '@/api/terminal'

export interface AcliProgressCallback {
  (log: { type: 'info' | 'success' | 'warn' | 'error'; text: string; status?: string }): void
}

export interface AcliSyncResult {
  status: 'installed' | 'up_to_date' | 'disk_space_insufficient' | 'error'
  version?: string
  buildTime?: string
  architecture?: string
  availableMb?: number
  requiredMb?: number
  message?: string
}

/**
 * 确保目标 HCI 集群上的 acli 工具已安装且就绪。
 * 监听 WebSocket 消息，向 terminal_bridge 发送 acli_sync 指令，并向回调函数报告细粒度进度。
 */
export function ensureAcliReady(
  socket: WebSocket,
  caseId: string,
  onProgress?: AcliProgressCallback,
  options?: AcliSyncOptions,
  timeoutMs = 180000,
): Promise<AcliSyncResult> {
  return new Promise((resolve, reject) => {
    let resolved = false
    let timer: number | null = null

    function cleanup() {
      if (timer !== null) {
        clearTimeout(timer)
        timer = null
      }
      socket.removeEventListener('message', handleMessage)
    }

    function handleMessage(event: MessageEvent) {
      let msg: TerminalWsMessage
      try {
        msg = JSON.parse(String(event.data || ''))
      } catch {
        return
      }

      if (msg.case_id && msg.case_id !== caseId) {
        return
      }

      if (msg.type === 'acli_sync_progress') {
        const text = msg.message || '正在处理 acli...'
        onProgress?.({
          type: 'info',
          text,
          status: msg.status,
        })
      } else if (msg.type === 'acli_sync_result') {
        resolved = true
        cleanup()

        const result: AcliSyncResult = {
          status: (msg.status as any) || 'installed',
          version: msg.current_version,
          buildTime: msg.latest_version,
          architecture: msg.architecture,
          availableMb: msg.available_mb,
          requiredMb: msg.required_mb,
          message: msg.message,
        }

        if (msg.status === 'installed' || msg.status === 'up_to_date') {
          onProgress?.({
            type: 'success',
            text: msg.message || (msg.status === 'installed' ? 'acli 安装完成' : 'acli 工具已就绪'),
            status: msg.status,
          })
          resolve(result)
        } else if (msg.status === 'disk_space_insufficient') {
          onProgress?.({
            type: 'error',
            text: msg.message || '/sf/data/local 空间不足',
            status: msg.status,
          })
          reject(new Error(msg.message || '/sf/data/local 空间不足'))
        } else {
          onProgress?.({
            type: 'error',
            text: msg.message || 'acli 检查或安装失败',
            status: msg.status,
          })
          reject(new Error(msg.message || 'acli 检查或安装失败'))
        }
      }
    }

    timer = window.setTimeout(() => {
      if (!resolved) {
        resolved = true
        cleanup()
        const timeoutMsg = 'acli 自动检测与安装超时 (180秒)'
        onProgress?.({ type: 'error', text: timeoutMsg, status: 'error' })
        reject(new Error(timeoutMsg))
      }
    }, timeoutMs)

    socket.addEventListener('message', handleMessage)
    socket.send(buildAcliSyncMessage(caseId, options))
  })
}
