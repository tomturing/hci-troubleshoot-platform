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