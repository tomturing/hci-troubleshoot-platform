import type { UploadInitResult } from '@/api/logCollect'
import { UI_TEXT } from '@hci/shared'

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
  detail?: Record<string, unknown> | null
}

/**
 * P1 落盘侧核验快照（后端写在 `task.detail.p1_archive_check`）。
 *
 * 这是平台能拿到的**唯一**交付证据来源：平台不留副本、也没有独立回执，
 * 除了"去日志服务器按产物名查一次"，没有任何别的办法回答"包到了没有"。
 * 因此界面必须把"这次查的结果与时刻"摆出来，而不是替外部系统下结论。
 */
export interface ArchiveCheckView {
  verified?: boolean
  /** archive_missing / size_mismatch / stale_archive / archive_size_invalid / verified */
  reason?: string | null
  checked_at?: string | null
  expected_size?: number | null
  archived_size?: number | null
  filename?: string | null
}

/** 取出最近一次 P1 归档核验快照；结构不对一律当"没有快照"，不猜。 */
export function p1ArchiveCheckOf(task: DeliveryEvidenceTask | null | undefined): ArchiveCheckView | null {
  const raw = task?.detail?.['p1_archive_check']
  if (!raw || typeof raw !== 'object') return null
  return raw as ArchiveCheckView
}

/** 体积展示：与结果态其他行一致（GB 保留两位、MB 保留一位）。 */
function formatCheckSize(size: number | null | undefined): string {
  const value = Number(size ?? 0)
  if (!(value > 0)) return '未知体积'
  const mb = value / 1024 / 1024
  return mb >= 1024 ? `${(mb / 1024).toFixed(2)} GB` : `${mb.toFixed(1)} MB`
}

/**
 * 核验快照 → 一句可读事实。
 *
 * 措辞纪律：只描述**观测**（什么时候、在日志服务器上看到了什么），
 * 绝不说"上传失败"或"平台没有收到"——单次查不到既可能是还没传完，
 * 也可能是 5888 抖动（2026-11-10 Q2026111000243 上传成功后仍被显示成交付失败）。
 */
export function describeArchiveCheck(check: ArchiveCheckView | null | undefined): string {
  if (!check) return ''
  const stamp = Date.parse(String(check.checked_at ?? ''))
  const head = Number.isFinite(stamp)
    ? `最近一次核验（${new Date(stamp).toLocaleString('zh-CN', { hour12: false })}）`
    : '最近一次核验'
  if (check.verified === true) return `${head}：日志服务器上已查到该工单的归档件`
  switch (String(check.reason ?? '')) {
    case 'archive_missing':
      return `${head}：日志服务器上暂未查到该工单的归档件`
    case 'size_mismatch':
      return `${head}：归档件体积 ${formatCheckSize(check.archived_size)}，小于平台记录的 ${formatCheckSize(check.expected_size)}`
    case 'stale_archive':
      return `${head}：日志服务器上只有采集之前就存在的同名包，不能算本次结果`
    case 'archive_size_invalid':
      return `${head}：归档件体积异常，无法确认`
    default:
      return `${head}：未核验到该工单的归档件`
  }
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

/** 按契约占位符渲染文案；占位符用 {name}，两端共用同一份模板。 */
function fmt(template: string, vars: Record<string, string>): string {
  // 用 split/join 而不是 replaceAll：当前 tsconfig 的 lib 还没到 ES2021。
  return Object.entries(vars).reduce(
    (text, [key, value]) => text.split(`{${key}}`).join(value),
    template,
  )
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
  /** 最近一次落盘侧核验快照（`task.detail.p1_archive_check`），可缺省 */
  archiveCheck?: ArchiveCheckView | null
  /** 结果态此刻是否真的提供「上传到平台」入口：按钮不存在就不能写"可点" */
  canUpload?: boolean
  /** 平台派生的三维结论；存在时优先于本地 status/delivery 拼接的文案 */
  display?: {
    conclusion: string
    conclusion_label: string
    conclusion_tone: string
    phase_label?: string
    conditions?: Array<{ label: string }>
    /** 首个异常点；存在时客户侧只呈现一句话定界 */
    fault_attribution?: { customer_text?: string | null } | null
    trace_id?: string | null
  } | null
  /** 平台派生的产物证据 */
  artifact?: { quality_label?: string; reason_text?: string; size_text?: string } | null
  /** 规范错误码：横幅必须带出"这一次是哪一类失败"，而不是笼统地说失败 */
  errorCode?: string | null
  /** 与 errorCode 对应的结论（来自诊断映射表），缺省时横幅只报码不编造结论 */
  hintTitle?: string | null
}): ResultHeadline {
  const {
    status,
    delivered = false,
    when = '',
    archiveCheck = null,
    canUpload = false,
    display = null,
    artifact = null,
    errorCode = null,
    hintTitle = null,
  } = args
  if (!status) return { type: 'info', title: '', description: '' }
  if (display) {
    // 第一性原理：界面不该有第二套判定。平台已按「交付事实 + 产物证据」给出结论，
    // 这里的旧文案只能作为旧后端降级路径，不能覆盖三维结论。
    const toneMap: Record<string, ResultHeadline['type']> = {
      success: 'success', warning: 'warning', error: 'error', info: 'info',
    }
    const tone = toneMap[display.conclusion_tone] ?? 'info'
    // 客户侧只回答三件事：哪里出了问题、什么问题、下一步做什么。
    // 阶段判据、证据表与管理员判据留在 admin-ui，不把客户淹没在诊断细节里。
    if (display.fault_attribution?.customer_text) {
      const traceId = String(display.trace_id || '').trim()
      return {
        type: tone,
        title: fmt(UI_TEXT.headline.conclusion_with_time, { conclusion: display.conclusion_label, when }),
        description: `${display.fault_attribution.customer_text}${
          traceId ? fmt(UI_TEXT.headline.trace_suffix, { trace: traceId }) : ''
        }。`,
      }
    }
    const code = String(errorCode || '').trim()
    const hint = String(hintTitle || '').trim()
    const needDiagnosis = tone === 'error' || tone === 'warning'
    const diagnosis = needDiagnosis
      ? code
        ? fmt(hint ? UI_TEXT.headline.diagnosis_with_hint : UI_TEXT.headline.diagnosis_without_hint, { code, hint })
        : UI_TEXT.fallback.no_code
      : ''
    // 观察事实由 LogCollectStatusCard 独立成行展示一次；横幅不再重复拼同一段，
    // 否则「观察事实」会在同屏出现两遍（且 fault_attribution 分支根本走不到这里）。
    const evidence = artifact?.quality_label
      ? fmt(UI_TEXT.headline.artifact_quality, { quality: artifact.quality_label, reason: artifact.reason_text ?? UI_TEXT.fallback.unknown })
      : ''
    const segments = [
      display.phase_label ? fmt(UI_TEXT.headline.current_stage, { phase: display.phase_label }) : '',
      evidence,
      diagnosis,
    ].filter(Boolean)
    const retry = tone === 'error' && diagnosis ? UI_TEXT.headline.retry_after_confirm : ''
    return {
      type: tone,
      title: fmt(UI_TEXT.headline.conclusion_with_time, { conclusion: display.conclusion_label, when }),
      description: segments.length ? `${segments.join('；')}${retry}。` : UI_TEXT.headline.no_segments,
    }
  }
  if (status === 'failed') {
    const code = String(errorCode || '').trim()
    const conclusion = String(hintTitle || '').trim()
    // 旧文案"可点重新收集再试一次"把客户推向碰运气：Q2026110302155 在 100 秒内连点 5 次，
    // 三次失败其实踩在不同阶段。横幅改成"先定位 → 再处理 → 确认后才重收"，
    // 且把错误码与结论直接摆到第一屏。
    const diagnosis = code
      ? fmt(conclusion ? UI_TEXT.headline.diagnosis_with_hint : UI_TEXT.headline.diagnosis_without_hint, { code, hint: conclusion })
      : UI_TEXT.fallback.no_code
    return {
      type: 'error',
      title: fmt(UI_TEXT.headline.last_failed, { when }),
      description: `${diagnosis}${UI_TEXT.headline.retry_after_confirm}。`,
    }
  }
  if (status === 'cancelled') {
    return {
      type: 'info',
      title: fmt(UI_TEXT.headline.last_cancelled, { when }),
      description: UI_TEXT.headline.last_cancelled_desc,
    }
  }
  if (!delivered) {
    // 这里只能说"平台未核验到"，不能说"日志服务器上没有"：
    // 单次回查（尤其采集刚结束时的那次）看到的是**瞬时**观测，上传往往才刚开始；
    // 而 P1 平台本就没有独立回执（Q2026111000243 上传成功后仍被写成"交付失败"）。
    const observed = describeArchiveCheck(archiveCheck)
    const next = canUpload
      ? UI_TEXT.headline.reverify_then_upload
      : UI_TEXT.headline.reverify_only
    return {
      type: 'warning',
      title: fmt(UI_TEXT.headline.not_delivered, { when }),
      description: `${UI_TEXT.headline.p1_direct_note}${
        observed ? `${observed}。` : ''
      }${next}`,
    }
  }
  return {
    type: 'success',
    title: fmt(UI_TEXT.headline.last_success, { when }),
    description: UI_TEXT.headline.last_success_desc,
  }
}