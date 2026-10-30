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
