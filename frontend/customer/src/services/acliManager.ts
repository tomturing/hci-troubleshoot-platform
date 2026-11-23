import {
  buildAcliSyncMessage,
  parseAcliPluginSync,
  parseAcliSyncFacts,
  type AcliPluginSyncReceipt,
  type AcliRetryStage,
  type AcliSyncFacts,
  type AcliSyncOptions,
  type TerminalWsMessage,
} from '@/api/terminal'

/**
 * 阶段一（装工具）的等待预算。
 *
 * 必须**大于** bridge 侧的分段预算之和（下载 120s + 上传 300s + 安装 240s = 660s），
 * 并再留 60s 给桥开工前的“拉插件期望态”（票据 TTL 只 10 分钟，桥必须先去取，
 * 见 terminal_bridge/acli_plugin_sync.go）：
 * 前端先超时的话，bridge 还在重试，界面却已经报“超时”，用户会据此重复点击，
 * 反而把两个上传叠在一起（历史 92s×3 的现场就有这个味道）。
 */
export const ACLI_STAGE1_TIMEOUT_MS = 780_000

/**
 * 阶段二（插件差量收敛）的等待预算：与桥侧 acliPluginStageBudget 同值。
 *
 * 这两个数字必须一起看：桥的预算是“封顶”而非常态（无修改时阶段二只跑探测，秒级），
 * 但前端必须按封顶算——前端先超时就等于在桥还在装插件时把整条链路报成失败，
 * 而阶段二的失败**不回滚阶段一**，用户看到的是一个“假的没装成”。
 */
export const ACLI_PLUGIN_STAGE_TIMEOUT_MS = 1_800_000

/** acli_sync 一次往返的总等待预算（阶段一 + 阶段二）。 */
export const ACLI_SYNC_TIMEOUT_MS = ACLI_STAGE1_TIMEOUT_MS + ACLI_PLUGIN_STAGE_TIMEOUT_MS

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
  /**
   * 阶段二（插件差量同步）回执；旧桥不下发时为 null。
   *
   * 它与 status 是两个独立结论：阶段一成功而阶段二部分节点没装成，
   * 必须能在界面上分开表达，不能揉成一句“acli 已就绪”。
   */
  pluginSync: AcliPluginSyncReceipt | null
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
        // plugins 是阶段二开工提示：它不是失败，但也不该混在“正在安装工具”的文案里。
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
        const pluginSync = parseAcliPluginSync(msg)
        const result: AcliSyncResult = {
          status: (msg.status as AcliSyncResult['status']) || 'installed',
          version: msg.current_version,
          buildTime: msg.latest_version,
          architecture: msg.architecture,
          availableMb: msg.available_mb,
          requiredMb: msg.required_mb,
          message: msg.message,
          facts,
          pluginSync,
        }

        if (msg.status === 'installed' || msg.status === 'up_to_date') {
          onProgress?.({
            type: 'success',
            text: msg.message || (msg.status === 'installed' ? 'acli 安装完成' : 'acli 工具已就绪'),
            status: msg.status,
            facts,
          })
          // 阶段二没全成：工具装好了是真，插件没收敛完也是真，两条都要上屏。
          if (pluginSync && !pluginSync.ok) {
            onProgress?.({
              type: 'error',
              text: `插件同步未完成（${pluginSync.reason || 'plugin_deploy_failed'}）：${pluginSync.detail || ''}`,
              status: 'plugins',
              facts,
            })
          } else if (pluginSync) {
            onProgress?.({
              type: 'info',
              text: `插件同步：新增 ${pluginSync.installed}、覆盖 ${pluginSync.overwritten}、跳过 ${pluginSync.skipped}`,
              status: 'plugins',
              facts,
            })
          }
          // 安装降级（install_degraded）：工具确实装上了，但节点侧事实不完整。
          // 必须与"成功"分开呈现，否则用户会直接去 create，然后在节点上撞 LC-INSTALL-010。
          if (facts.degraded) {
            onProgress?.({
              type: 'warn',
              text: '安装降级：部分节点内置插件未落盘或磁盘空间未验证；请重试安装后再采集。',
              status: 'degraded',
              facts,
            })
          }
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
