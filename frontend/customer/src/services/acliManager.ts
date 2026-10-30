import {
  buildAcliSyncMessage,
  parseAcliSyncFacts,
  type AcliRetryStage,
  type AcliSyncFacts,
  type AcliSyncOptions,
  type TerminalWsMessage,
} from '@/api/terminal'

/**
 * 前端等待 acli 同步的总预算。
 *
 * 必须**大于** bridge 侧的分段预算之和（下载 120s + 上传 300s + 安装 240s = 660s）：
 * 前端先超时的话，bridge 还在重试，界面却已经报“超时”，用户会据此重复点击，
 * 反而把两个上传叠在一起（历史 92s×3 的现场就有这个味道）。
 */
export const ACLI_SYNC_TIMEOUT_MS = 780_000

export interface AcliProgressCallback {
  (log: {
    type: 'info' | 'success' | 'warn' | 'error'
    text: string
    status?: string
    /** 消息里携带的完整性事实：上传进度、校验结论、安装输出都靠它 */
    facts?: AcliSyncFacts
  }): void
}

export interface AcliSyncResult {
  status: 'installed' | 'up_to_date' | 'disk_space_insufficient' | 'error'
  version?: string
  buildTime?: string
  architecture?: string
  availableMb?: number
  requiredMb?: number
  message?: string
  /** 安装包完整性事实：包多大、摘要是什么、校验强度、安装输出 */
  facts: AcliSyncFacts
}

/**
 * acli 同步失败（携带可重试信息）。
 *
 * 为什么不用普通 Error：失败后前端要渲染「重试上传 / 重试安装」兜底按钮，
 * 而这些按钮是否出现取决于**哪一跳**失败、是否可重试。只带一句 message
 * 的错误链会把这些信息全部丢掉，界面只能给出“报错后不管”。
 */
export class AcliSyncFailure extends Error {
  readonly facts: AcliSyncFacts
  readonly retrySuggestions: AcliRetryStage[]

  constructor(message: string, facts: AcliSyncFacts) {
    super(message)
    this.name = 'AcliSyncFailure'
    this.facts = facts
    this.retrySuggestions = acliRetrySuggestions(facts)
  }
}

/**
 * 推断「该从哪一跳重试」。
 *
 * 第一性原理：重试的价值 = 跳过已确认正确的那部分工作，而“哪部分已正确”
 * 只能由 bridge 回传的事实回答：
 *  - install_success=true → 安装命令已跑完，包已被证明可用，只重启安装（重传反而多余）；
 *  - download/upload/verify → 包还没被证明正确（含“落盘校验不符”），必须重传；
 *  - install 阶段失败 → 包已校验通过，直接重装；
 *  - 不可重试（如磁盘不足）→ 不给按钮，避免诱导无效重试。
 */
export function acliRetrySuggestions(facts: AcliSyncFacts): AcliRetryStage[] {
  if (!facts.retryable) return []
  if (facts.installSuccess) return ['install', 'all']
  switch (facts.stage) {
    case 'install':
      return ['install', 'all']
    case 'download':
    case 'upload':
    case 'verify':
      return ['upload', 'all']
    default:
      // 阶段未知（旧版 bridge）：只能建议完整重来，不赌“跳过某一跳”一定成立。
      return ['all']
  }
}

/**
 * 确保目标 HCI 集群上的 acli 工具已安装且就绪。
 * 监听 WebSocket 消息，向 terminal_bridge 发送 acli_sync 指令，并向回调函数报告细粒度进度。
 *
 * options.retryStage 非 all 时表示「用户点了兜底按钮」：bridge 会跳过已完成的阶段，
 * 并复用已校验的安装包（缓存或节点上的包），不再从头下载。
 */
export function ensureAcliReady(
  socket: WebSocket,
  caseId: string,
  onProgress?: AcliProgressCallback,
  options?: AcliSyncOptions,
  timeoutMs = ACLI_SYNC_TIMEOUT_MS,
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
        // install_output 是安装命令的完整回执：用 warn/error 呈现，避免读成“成功”。
        const type: 'info' | 'warn' | 'error' =
          msg.status === 'install_output' ? (msg.install_success ? 'info' : 'error') : 'info'
        onProgress?.({
          type,
          text,
          status: msg.status,
          facts: parseAcliSyncFacts(msg),
        })
      } else if (msg.type === 'acli_sync_result') {
        resolved = true
        cleanup()

        const facts = parseAcliSyncFacts(msg)
        const result: AcliSyncResult = {
          status: (msg.status as AcliSyncResult['status']) || 'installed',
          version: msg.current_version,
          buildTime: msg.latest_version,
          architecture: msg.architecture,
          availableMb: msg.available_mb,
          requiredMb: msg.required_mb,
          message: msg.message,
          facts,
        }

        if (msg.status === 'installed' || msg.status === 'up_to_date') {
          onProgress?.({
            type: 'success',
            text: msg.message || (msg.status === 'installed' ? 'acli 安装完成' : 'acli 工具已就绪'),
            status: msg.status,
            facts,
          })
          resolve(result)
        } else if (msg.status === 'disk_space_insufficient') {
          const text = msg.message || '/sf/data/local 空间不足'
          onProgress?.({ type: 'error', text, status: msg.status, facts })
          reject(new AcliSyncFailure(text, facts))
        } else {
          const text = msg.message || 'acli 检查或安装失败'
          onProgress?.({ type: 'error', text, status: msg.status, facts })
          reject(new AcliSyncFailure(text, facts))
        }
      }
    }

    timer = window.setTimeout(() => {
      if (!resolved) {
        resolved = true
        cleanup()
        const timeoutMsg = `acli 自动检测与安装超时 (${Math.round(timeoutMs / 1000)}秒)`
        onProgress?.({ type: 'error', text: timeoutMsg, status: 'error' })
        // 超时同样要可重试：超时恰恰是最常见的可恢复失败（链路慢、节点忙）。
        // stage 归为 upload，让兜底按钮直接给出「重试上传」（包已下载时无需重下）。
        reject(
          new AcliSyncFailure(timeoutMsg, parseAcliSyncFacts({ status: 'error', stage: 'upload' })),
        )
      }
    }, timeoutMs)

    socket.addEventListener('message', handleMessage)
    socket.send(buildAcliSyncMessage(caseId, options))
  })
}
