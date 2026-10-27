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

export interface DeliverySubmitPayload {
  execId: string
  sourcePath: string
  platformUrl: string
  uploadToken: string
  chunkSize: number
  partCount: number
  totalSize: number
  uploadedParts: number[]
  deliveryMode: 'collect' | 'direct'
  tlsInsecure: boolean
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
    execId: args.taskId,
    sourcePath: args.sourcePath,
    platformUrl,
    uploadToken: args.session.upload_token,
    chunkSize: args.session.chunk_size,
    partCount: args.session.part_count,
    totalSize: args.totalSize,
    uploadedParts: args.session.uploaded_parts ?? [],
    deliveryMode: args.mode,
    tlsInsecure,
  }
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