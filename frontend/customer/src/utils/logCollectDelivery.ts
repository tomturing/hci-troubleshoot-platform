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