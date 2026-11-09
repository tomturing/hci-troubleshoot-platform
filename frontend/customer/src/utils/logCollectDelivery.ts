import type { UploadInitResult } from '@/api/logCollect'

/**
 * 日志包交付的协议层工具。
 *
 * 为什么要把这些从组件里搬出来：`delivery_submit` 的字段一旦漏传，
 * 表现的是一场完整的失败上传（现场 15 片全挂、报 x509），
 * 而组件内的构造逻辑没法被单测覆盖，只能靠"人肉记得别漏"。
 * 2026-10-27 的现场故障就是 `tls_insecure` 漏传造成的。
 */

/** 平台证书信任授权的存储前缀；作用域＝浏览器会话（见组件内注释）。 */
export const PLATFORM_CERT_TRUST_PREFIX = 'hci.logCollect.trustPlatformCert'

export interface DeliverySubmitArgs {
  taskId: string
  sourcePath: string
  totalSize: number
  session: UploadInitResult
  /** collect = 采集后自动衔接；direct = 补交付已有包 */
  mode: 'collect' | 'direct'
}

// 注意：字段名必须与 bridge 的 InMessage Transfer* 的 json tag 严格一致（snake_case）。
// bridge 用的是 `upload_token`/`source_path`/`platform_url`/`chunk_size`/`part_count`/
// `total_size`/`uploaded_parts`/`delivery_mode`/`tls_insecure`（见 terminal_bridge/main.go），
// 而非 camelCase。历史上前端发 camelCase 导致这些字段全部收不到——
// delivery 从 #1318 起就没把参数真正送到 bridge。这是契约硬约束，不能改。
export interface DeliverySubmitPayload {
  exec_id: string
  source_path: string
  platform_url: string
  upload_token: string
  chunk_size: number
  part_count: number
  total_size: number
  uploaded_parts: number[]
  delivery_mode: 'collect' | 'direct'
  tls_insecure: boolean
}

/**
 * 构造 delivery_submit 载荷。
 *
 * 每个字段都是 bridge 执行搬运的**必要输入**，缺一个就必然失败：
 *   - uploadToken/chunkSize/partCount：平台签发，bridge 不解释、只透传；
 *   - uploadedParts：续传依据，漏传会导致全量重传（20GB 级包代价极大）；
 *   - deliveryMode：collect 要等采集写完，direct 直接读已有包；
 *   - tlsInsecure：自签证书平台上唯一能让请求出得去的开关。
 */
export function buildDeliverySubmitPayload(
  args: DeliverySubmitArgs,
  platformUrl: string,
  tlsInsecure: boolean,
): DeliverySubmitPayload {
  return {
    exec_id: args.taskId,
    source_path: args.sourcePath,
    platform_url: platformUrl,
    upload_token: args.session.upload_token,
    chunk_size: args.session.chunk_size,
    part_count: args.session.part_count,
    total_size: args.totalSize,
    uploaded_parts: args.session.uploaded_parts ?? [],
    delivery_mode: args.mode,
    tls_insecure: tlsInsecure,
  }
}

/**
 * 构造 `delivery_submit` 的完整 WS 消息。
 *
 * 为什么不复用 `buildDeliveryMessage`：那一层按 **camelCase** 取字段
 * （`options.sourcePath`、`options.uploadToken`…），而协议载荷是 snake_case
 * （与 bridge 的 `InMessage` json tag 严格一致）。中间再翻译一次的结果是
 * 字段**全部丢失**，bridge 只收到 `{type, case_id, node_ip}`，于是
 * `normalizeArchivePath("")` 报 `source_path 不能为空`（2026-10-27 现场）。
 * 更要命的是 `delivery_mode` / `tls_insecure` 连 options 类型里都没有，
 * 所以 P2 交付与证书授权从未真正送到 bridge —— `bridge_relay` 从来没成功过。
 *
 * 载荷本身已经是协议的唯一权威描述，这里只做平铺，不做第二次映射。
 */
export function buildDeliverySubmitMessage(
  payload: DeliverySubmitPayload,
  caseId: string,
  nodeIp?: string,
): string {
  return JSON.stringify({
    type: 'delivery_submit',
    case_id: caseId,
    node_ip: nodeIp || undefined,
    ...payload,
  })
}

/**
 * 判断交付阶段是否已构成「不可自愈的失败」。
 *
 * `retrying` 是 bridge 的**中间态**（后台正在退避重排），不是结论；
 * 把它当失败会让"本可自愈的作业"被前端提前判死，并回写平台
 * `delivery_status=failed`，于是界面说失败、字节却还在往上传。
 * 这条判据被单测锁住，避免以后又有人用 `phase !== 'done'` 之类的近似写法。
 */
export function isTerminalDeliveryFailure(phase: string | undefined): boolean {
  return phase === 'failed'
}

/**
 * 判断错误是否属于「平台证书不受信」。
 *
 * bridge 把 Go 标准库原文直接回传，例如：
 * `tls: failed to verify certificate: x509: certificate signed by unknown authority`。
 * 这条错误在本项目里有一个确定的处理动作（确认自签证书），
 * 认不出来就只能把英文抛给现场。
 */
export function isCertificateUntrusted(message: unknown): boolean {
  const text = String(message ?? '').toLowerCase()
  return (
    text.includes('x509') ||
    text.includes('certificate signed by unknown authority') ||
    text.includes('failed to verify certificate')
  )
}

/** 读取平台证书信任授权（仅当前浏览器会话有效）。 */
export function platformCertTrusted(origin: string): boolean {
  try {
    return sessionStorage.getItem(`${PLATFORM_CERT_TRUST_PREFIX}:${origin}`) === '1'
  } catch {
    // 隐私模式/禁用存储：取不到就当未授权，宁可多问一次
    return false
  }
}

/** 记住平台证书信任授权。存不下不影响本次交付（授权已随作业下发）。 */
export function rememberPlatformCertTrust(origin: string): void {
  try {
    sessionStorage.setItem(`${PLATFORM_CERT_TRUST_PREFIX}:${origin}`, '1')
  } catch {
    /* 忽略：下次再问一次即可 */
  }
}

/**
 * 拼装「失败原因」展示文本：`[错误码] 原因`，缺失时如实说明而不是编造。
 *
 * 为什么必须读任务记录而不是就地拼一句通用文案：Q2026102801062 现场界面写的是
 * 「归档到日志服务器失败，错误码见平台任务记录」——用户照着去查，任务记录里
 * error_code / error_message 全是空，等于让用户去一个没有答案的地方找答案。
 * 代传终态的权威原因只有后端 `_finish` 写的那一份，前端只能转述它。
 *
 * 空值口径：有码无因时保留码（码本身就能报给支持），两者都无才用 fallback。
 */
export function formatFailureReason(
  detail: { error_code?: string | null; error_message?: string | null } | null | undefined,
  fallback: string,
): string {
  const code = detail?.error_code ? `[${detail.error_code}] ` : ''
  const message = String(detail?.error_message ?? '').trim()
  return message ? `${code}${message}` : `${code}${fallback}`
}

/** 代传进度快照里与「正在干什么」相关的字段（与后端 snapshot 一一对应）。 */
export interface RelayProgressView {
  relay_chunk_index?: number | null
  relay_chunk_total?: number | null
  relay_attempt?: number | null
  relay_last_error?: string | null
}

/** 平台分片上传进度快照（后端写在 `task.detail.upload_progress`）。 */
export interface UploadProgressView {
  uploaded?: number
  total?: number
  /** 服务端最后一次收片的时刻（ISO 8601，UTC） */
  updated_at?: string | null
}

/**
 * 超过这个时长没有新增分片，就提示"链路可能已停滞"。
 *
 * 取 10 分钟：正常弱网下一片（8 MiB）在 30~500 秒之间，10 分钟没动已经不是慢，
 * 而是这一轮多半又被单片超时掐断了。注意措辞是"可能"，前端不替 bridge 下结论。
 */
const UPLOAD_PROGRESS_STALL_HINT_MS = 10 * 60 * 1000

/**
 * 拼装"平台已收到第几片"。
 *
 * 第一性原理：交付卡住时，平台侧的 `delivery_status` 一直是 queued、`object_size`
 * 一直是空，界面只能说「交付已登记但未完成」——用户看不出 19/233 和 231/233 的差别，
 * 于是"再等等"和"这条链已经废了"变成同一个画面（2026-11-06 Q2026110600112
 * 卡在 19/233 十小时，现场始终无法判断）。
 * 分片计数是唯一能回答这个问题的服务端事实，`updated_at` 则回答"还在动吗"。
 */
export function describeUploadProgress(
  view: UploadProgressView | null | undefined,
  now: number = Date.now(),
): string {
  const uploaded = Number(view?.uploaded ?? 0)
  const total = Number(view?.total ?? 0)
  if (!(uploaded > 0) || !(total > 0)) return ''

  const head = `平台已收到 ${uploaded}/${total} 片`
  const stamp = Date.parse(String(view?.updated_at ?? ''))
  if (!Number.isFinite(stamp)) return head

  // 现场机器很少校时，浏览器时钟可能落后于服务端：负数一律读作"刚刚"，
  // 不能显示成"负 3 分钟前"。
  const idleMs = Math.max(0, now - stamp)
  if (idleMs < 60_000) return `${head}（刚刚仍在写入）`
  const minutes = Math.round(idleMs / 60_000)
  if (idleMs >= UPLOAD_PROGRESS_STALL_HINT_MS) {
    return `${head}（已 ${minutes} 分钟没有新增分片，链路可能已停滞）`
  }
  return `${head}（${minutes} 分钟前仍在写入）`
}

/** 最近一次错误在进度行里的展示上限：进度行是单行，长堆栈会把关键信息挤没。 */
const RELAY_ERROR_INLINE_LIMIT = 120

/**
 * 拼装代传进度行，回答「现在传到第几片、第几轮、上一轮为什么失败」。
 *
 * 第一性原理：单片上传超时 300s 期间，`relay_chunk_index` 只在成功后前进，
 * 于是"卡住"和"正在传"在界面上长得一模一样，用户唯一的结论只能是"挂了"。
 * 这三条信息是区分二者的最小集合，且全部来自服务端事实，前端不猜。
 */
export function describeRelayProgress(view: RelayProgressView | null | undefined): string {
  if (!view) return '准备中'
  const total = Number(view.relay_chunk_total ?? 0)
  const index = Number(view.relay_chunk_index ?? -1)
  const parts: string[] = []

  if (total > 0 && index >= 0) parts.push(`第 ${index + 1}/${total} 片`)
  else if (total > 0) parts.push(`共 ${total} 片，准备中`)

  const attempt = Number(view.relay_attempt ?? 0)
  // 第 1 轮是常态，写出来只是噪声；只有真的重试过才值得占位。
  if (attempt > 1) parts.push(`第 ${attempt} 次尝试`)

  const error = String(view.relay_last_error ?? '').trim()
  if (error) {
    const inline = error.length > RELAY_ERROR_INLINE_LIMIT ? `${error.slice(0, RELAY_ERROR_INLINE_LIMIT)}…` : error
    parts.push(`上次失败：${inline}`)
  }

  return parts.length > 0 ? parts.join(' · ') : '准备中'
}

/** 交付证据判定需要的最小任务字段（与 LogCollectTask 结构兼容，便于纯函数单测）。 */
export interface DeliveryEvidenceTask {
  relay_status?: string | null
  delivery_status?: string | null
  upload_mode?: string | null
  remote_url?: string | null
  archive_deleted?: boolean | null
}

/**
 * 平台是否已有**交付**证据（产物真的到了日志服务器）。
 *
 * 第一性原理：`status=completed` 只说明 acli 报采集结束，与"字节有没有出客户内网"
 * 毫无因果关系。2026-11-09 Q2026110900356 就是 `status=completed` + 平台零产物 +
 * 日志服务器 `found=false`，界面却给了绿色成功横幅——用户据此认为 P2 已成功。
 *
 * 因此只有两类证据成立：
 *   - P2：`relay_status=success`（5888 归档确认）或 `delivery_status=delivered`；
 *   - P1：`delivery_status=delivered`，或"插件直传成功后自删压缩包"这条间接证据。
 */
export function hasDeliveryEvidence(task: DeliveryEvidenceTask | null | undefined): boolean {
  if (!task) return false
  if (task.relay_status === 'success' || task.delivery_status === 'delivered') return true
  const isP1 = task.upload_mode === 'acli_direct' || Boolean(task.remote_url)
  if (!isP1) return false
  // 老记录没有 upload_mode：按 P1 口径兜底；已清理压缩包是 P1 成功的间接证据。
  return !task.upload_mode || Boolean(task.archive_deleted)
}

/**
 * 是否为「状态机拒绝这一跳」（409 + LC-PLATFORM-003）。
 *
 * 为什么必须单独识别：交付与采集状态是正交的两件事，`completed → uploading`
 * 被拒只说明采集结论已经落库，**不说明交付不能继续**。把它当致命错误会中断
 * 整条搬运链路（2026-11-09 Q2026110900356：平台已开好上传会话，0/64 片就被打断）。
 *
 * 只认这一种码：同是 409 的 `LC-PLUGIN-007`（已有进行中的任务）等业务冲突
 * 必须原样抛出，吞掉会让真正的冲突永远不明说。
 */
export function isStatusTransitionRejected(error: unknown): boolean {
  const candidate = error as
    | {
        isAxiosError?: boolean
        problem?: { code?: unknown }
        response?: { status?: number; data?: { detail?: { code?: unknown } } }
      }
    | null
    | undefined
  if (!candidate || candidate.isAxiosError !== true) return false
  if (Number(candidate.response?.status ?? 0) !== 409) return false
  const code =
    (typeof candidate.problem?.code === 'string' && candidate.problem.code) ||
    (typeof candidate.response?.data?.detail?.code === 'string' && candidate.response.data.detail.code) ||
    ''
  return code === 'LC-PLATFORM-003'
}

/**
 * 吞掉「状态机拒绝」之后，是否仍可继续交付。
 *
 * 对抗性审查：`isStatusTransitionRejected` 只认错误码，而 `cancelled → uploading`
 * 同样是 `LC-PLATFORM-003`。若无条件继续，用户取消的任务会被照常把字节发到日志
 * 服务器——那是违背用户意图的对外副作用，必须由**服务端最新状态**裁决：
 *
 *   - `completed`：采集已成功、只是状态被对账推进，交付继续（本次要修的现场）；
 *   - `cancelled` / `failed` / 其它：立即中止，绝不用交付把终态"复活"。
 *
 * 取不到状态（网络失败）同样中止：不确定时不做不可撤销的对外动作。
 */
export function canResumeDeliveryAfterReject(status?: string | null): boolean {
  return String(status ?? '').trim() === 'completed'
}

/** 结果态横幅的三态（success 只给"已交付"，采集完成未交付必须降级为 warning）。 */
export interface ResultHeadline {
  type: 'success' | 'warning' | 'error' | 'info'
  title: string
  description: string
}

/**
 * 结果态横幅的唯一判定出口。
 *
 * 旧实现只看 `status !== failed/cancelled` 就给绿色 success"已完成日志收集"，
 * 于是「采完但一个字节都没传」的任务在界面上与真成功完全同形。横幅是用户判断
 * "这次到底成没成"的第一眼入口，必须由**交付证据**决定，而不是采集结论。
 */
export function buildResultHeadline(args: {
  status?: string | null
  delivered?: boolean
  when?: string
}): ResultHeadline {
  const { status, delivered = false, when = '' } = args
  if (!status) return { type: 'info', title: '', description: '' }
  if (status === 'failed') {
    return {
      type: 'error',
      title: `该工单上次采集失败（${when}）`,
      description:
        '以下是平台记录的最近一次采集结果；失败原因见下方错误信息，可点右下角「重新收集」再试一次。',
    }
  }
  if (status === 'cancelled') {
    return {
      type: 'info',
      title: `该工单上次采集已取消（${when}）`,
      description: '以下是平台记录的最近一次采集结果；如需采集请点右下角「重新收集」。',
    }
  }
  if (!delivered) {
    return {
      type: 'warning',
      title: `该工单已完成日志收集，但日志尚未交付（${when}）`,
      description:
        '平台没有收到日志包，日志服务器上也没有归档件。压缩包仍在节点时可点「上传到平台」补交付，不需要重新采集。',
    }
  }
  return {
    type: 'success',
    title: `该工单已完成日志收集并已交付（${when}）`,
    description: '以下是平台记录的最近一次收集结果；如需再次采集，点右下角「重新收集」。',
  }
}