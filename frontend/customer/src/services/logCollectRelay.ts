/**
 * 代传租约的前端判定（纯函数，便于单测）。
 *
 * 回归背景（2026-10-30 发布复盘）：
 * 代传是 case-service 进程内的后台任务，发布/重启会把进程杀掉，而没有任何人
 * 去写终态 —— `relay_status` 就永远停在 `running`。前端原先只知道「不是终态」，
 * 于是干等满 2 小时轮询上限才敢说「结果未知」，期间用户点「上传到平台」还会
 * 重传整个包（且因为 running 被幂等短路，代传依旧不会启动）。
 *
 * 服务端现在给快照加了 `stale`（running 且心跳过期），这里只负责回答
 * 「要不要发一次强制续跑」。真正的抢占仍由服务端 `trigger(force)` 裁决，
 * 多标签页同时判定也只会有一个真正启动。
 */

export interface RelayLeaseSnapshot {
  relay_status?: string | null
  stale?: boolean | null
}

/**
 * 是否应对「租约过期」的代传发起一次强制续跑。
 *
 * @param snapshot 服务端 `/relay` 快照（含 stale 标记）
 * @param alreadyAttempted 本轮轮询是否已经发过强制续跑
 *
 * 只对 `running + stale` 生效：
 * - `success/failed` 是终态，重跑没有意义；
 * - `pending/none` 尚未开始，由 complete 或用户显式操作触发；
 * - 不 stale 说明确实有实例在续租，抢跑会造成同一包被两个进程同时传。
 */
export function shouldRecoverRelayLease(
  snapshot: RelayLeaseSnapshot | null | undefined,
  alreadyAttempted: boolean,
): boolean {
  if (alreadyAttempted) return false
  if (!snapshot) return false
  if (snapshot.relay_status !== 'running') return false
  return snapshot.stale === true
}

/** axios 错误里与判定相关的最小字段（不引入 axios 类型依赖，便于纯函数单测）。 */
export interface PlatformErrorLike {
  isAxiosError?: boolean
  response?: { status?: number }
}

/**
 * 平台侧异常是否属于「这一刻问不到」，而非「业务上已失败」。
 *
 * 回归背景（2026-11-06 工单 Q2026110600940）：
 * 一次 9.59 GiB 的 P2 代传期间，case-service 因内存超限被 OOMKilled，重启的那
 * 两秒里 api-gateway 把「连接不上下游」统一兜底成 `503 Case Service unavailable`。
 * 前端把这个 503 当成业务失败写进 `status=failed`，而服务端后台代传随后成功完成
 * （relay success + 链尾摘要校验通过），于是同一单同时存在「已交付」与「失败」。
 *
 * 第一性原理：**代传是服务端后台任务，与前端这一次 HTTP 请求没有因果关系**。
 * 请求失败只能证明"状态未知"，不能证明"任务失败"，因此这类错误必须走
 * 「结果未知」分支，绝不能替服务端写终态。
 *
 * 判据只用 axios 错误自身的属性：
 * - `502/503/504`：网关或下游暂时不可用（OOM 重启正是 503）；
 * - 有 `isAxiosError` 但没有 `response`：连接被重置 / 超时 / 断网，同样只代表问不到。
 * 业务代码自己 `throw new Error(...)` 不带 `isAxiosError`，必须判为真失败，
 * 否则「任务下发失败」这类确定结论会被当成瞬时抖动而永远不落库。
 */
export function isTransientPlatformError(error: unknown): boolean {
  const candidate = error as PlatformErrorLike | null | undefined
  if (!candidate || candidate.isAxiosError !== true) return false
  const status = Number(candidate.response?.status ?? 0)
  if (status === 502 || status === 503 || status === 504) return true
  return !candidate.response
}

/** WS 通道断开的规范文案：只在一处定义，避免判据与抛错处文案漂移。 */
export const BRIDGE_CHANNEL_LOST_MESSAGE = '与 terminal_bridge 的连接已断开，节点操作未送达'

/**
 * 「结果未知」的完整判据。
 *
 * 与 `isTransientPlatformError` 分开是为了不破坏原有语义：后者只认 axios 类
 * 网关异常。WS 通道断开是业务代码主动 `throw new Error(BRIDGE_CHANNEL_LOST)`，
 * 但它与 503 在**因果上完全同类**——浏览器这一刻问不到结果，服务端后台的
 * 交付作业仍在跑。2026-11-06 Q2026110601054 就是因为把它当成确定失败，
 * 前端写了 `status=failed`，而 relay 已 success、包已落 5888。
 *
 * 判据刻意收窄到规范文案本身：其它业务 Error（任务下发失败、令牌缺失）
 * 仍是确定结论，绝不能吞成"等一下就好"。
 */
export function isOutcomeUnknownError(error: unknown): boolean {
  if (isTransientPlatformError(error)) return true
  return error instanceof Error && error.message.includes(BRIDGE_CHANNEL_LOST_MESSAGE)
}

/**
 * 交付作业失败是否源于「会话基线已作废」。
 *
 * 源包在提交后被重新采集覆盖时，平台会话仍按旧的 total_size 冻结分片几何，
 * 继续上传只会每片失败。这类错误不该让用户手动重点「上传到平台」，
 * 而应由 `runDeliveryJob` 重新量包、重新签发会话并自动续跑。
 */
export function isSessionDiverged(message: string): boolean {
  return (
    message.includes('源包已变化') ||
    message.includes('上传会话大小不一致') ||
    message.includes('上传会话需按新基线重建') ||
    message.includes('会话已被新代替代')
  )
}

/**
 * 判断两次量到的包大小是否已稳定。
 *
 * 第一性原理：分片几何在 init 时冻结，因此「包还在写」时建立会话必然失败。
 * 采集未收尾就点上传是本单第二次报「源包已变化」的直接原因，
 * 必须在 init 之前把"字节序列已确定"验证掉，而不是依赖节点状态字段。
 */
export function isArchiveSizeSettled(previousSize: number, currentSize: number): boolean {
  return previousSize > 0 && currentSize === previousSize
}
