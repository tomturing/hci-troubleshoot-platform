/**
 * 两端状态对齐（custom-ui ↔ HCI 上的 acli 插件任务）。
 *
 * 第一性原理：节点是任务状态的唯一真相源，平台 DB 里的任务只是"投影"。
 * 投影随时可能因异步而失真（面板关闭、网络断、插件异常结束），
 * 因此必须有一个显式的对账函数：读节点态 → 与平台态比较 → 给出唯一动作。
 *
 * 这个文件只做判定，不产生副作用，便于穷举测试状态组合。
 */

/** 节点态：由 bridge 从 acli 中文输出归一化而来 */
export type NodeState = 'none' | 'running' | 'completed' | 'abnormal' | 'unknown'

export type PlatformStatus =
  | 'pending'
  | 'probing'
  | 'installing'
  | 'collecting'
  | 'packaging'
  | 'uploading'
  | 'completed'
  | 'failed'
  | 'cancelled'
  | (string & {})

export interface SyncInput {
  /** 平台当前任务状态；为空表示平台上没有任务记录 */
  platformStatus?: PlatformStatus | null
  nodeState: NodeState
  /** 节点上报的压缩包路径 */
  archivePath?: string
  /** 压缩包 mtime（Unix 秒），由 bridge 提供 */
  archiveMTime?: number
  /** 本次平台任务的创建时间（Unix 秒） */
  taskCreatedAt?: number
  /** 节点 progress 里的采集总数 */
  progressTotal?: number
  /** 探测到的在线节点数 */
  hostsCount?: number
}

export type SyncActionKind =
  | 'none'
  | 'adopt'
  | 'cancel_retry'
  | 'patch_completed'
  | 'patch_failed'
  | 'warn_node_busy'

export interface SyncAction {
  action: SyncActionKind
  /** 判定依据，直接进提示文案与日志，避免"为什么给出这个建议"无从追溯 */
  reason: string
}

const ACTIVE_STATUSES = new Set(['pending', 'probing', 'installing', 'collecting', 'packaging', 'uploading'])
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

/** 包先于任务创建 60 秒以上，判定为上一次任务留下的旧包（容忍时钟偏差） */
const ARCHIVE_FRESHNESS_TOLERANCE_SEC = 60

/**
 * 完成态是否可信。
 *
 * 对抗性审查：同 ticket 重复 create 时 acli 会返回**上一次任务**的完成报告
 * （实测 done=1/total=1，而集群有 3 个节点）。只看 status/percent 会把旧报告当成本次完成，
 * 于是平台写 completed、节点其实还在跑。两条硬判据：
 *   1. 包 mtime 必须晚于本次任务创建时间；
 *   2. progress 的 total 必须与探测到的节点数一致（拿不到任一侧就不作为否决条件）。
 */
export function isCompletionTrustworthy(input: SyncInput): { ok: boolean; reason: string } {
  if (!input.archivePath) {
    return { ok: false, reason: '节点报告完成但没有压缩包路径' }
  }
  if (input.archiveMTime && input.taskCreatedAt) {
    if (input.archiveMTime < input.taskCreatedAt - ARCHIVE_FRESHNESS_TOLERANCE_SEC) {
      return { ok: false, reason: '压缩包早于本次任务创建时间，判定为历史任务报告' }
    }
  }
  if (input.progressTotal && input.hostsCount && input.progressTotal !== input.hostsCount) {
    return {
      ok: false,
      reason: `采集总数 ${input.progressTotal} 与在线节点数 ${input.hostsCount} 不一致，判定为历史任务报告`,
    }
  }
  return { ok: true, reason: '完成态通过校验' }
}

/**
 * 对账判定：给定两端状态，返回唯一应当执行的动作。
 *
 * 幂等约定：只做"修正投影"与"清理僵尸"，绝不自动 create/重试——
 * 重复采集的代价远大于状态短暂不准。
 */
export function decideNodeSync(input: SyncInput): SyncAction {
  const { platformStatus, nodeState } = input

  // 认不出节点态时一律不动。宁可保留不一致让用户点「刷新进度」，
  // 也不要按猜测去 cancel 一个可能正常的采集任务。
  if (nodeState === 'unknown') {
    return { action: 'none', reason: '节点状态无法识别，保持平台现状' }
  }

  // 平台上没有任务记录：节点有游离任务时只能提示，没有可修正的投影
  if (!platformStatus) {
    if (nodeState === 'running' || nodeState === 'abnormal') {
      return {
        action: 'warn_node_busy',
        reason: nodeState === 'running' ? '节点上存在游离的采集任务' : '节点上存在异常结束的任务',
      }
    }
    return { action: 'none', reason: '两端均无任务' }
  }

  if (TERMINAL_STATUSES.has(platformStatus)) {
    if (platformStatus === 'completed') {
      // 已完成是最终归档态，不再因节点变化而回退
      return { action: 'none', reason: '平台已归档，保持现状' }
    }
    // failed / cancelled：节点仍在跑 → 接管；节点异常 → 清理后重试；节点干净 → 已对齐
    if (nodeState === 'running') {
      return { action: 'adopt', reason: '平台已判失败/取消，但节点任务仍在运行，可接管继续' }
    }
    if (nodeState === 'abnormal') {
      return { action: 'cancel_retry', reason: '节点任务异常结束，需清理后重试' }
    }
    if (nodeState === 'completed') {
      const trust = isCompletionTrustworthy(input)
      if (trust.ok) {
        return { action: 'patch_completed', reason: '节点任务已完成，修正平台终态' }
      }
      return { action: 'cancel_retry', reason: `节点完成态不可信：${trust.reason}` }
    }
    return { action: 'none', reason: '节点无任务，平台终态与之一致' }
  }

  if (ACTIVE_STATUSES.has(platformStatus)) {
    if (nodeState === 'completed') {
      const trust = isCompletionTrustworthy(input)
      if (trust.ok) {
        return { action: 'patch_completed', reason: '节点任务已完成且完成态可信' }
      }
      // 关键分支：不信任就不能落终态，否则会把历史报告当成本次成果
      return { action: 'none', reason: `节点完成态不可信（${trust.reason}），继续等待` }
    }
    if (nodeState === 'abnormal') {
      return { action: 'patch_failed', reason: '节点任务异常结束' }
    }
    if (nodeState === 'none') {
      if (input.archivePath) {
        const trust = isCompletionTrustworthy(input)
        if (trust.ok) {
          return { action: 'patch_completed', reason: '节点任务已结束且包已生成' }
        }
      }
      return { action: 'patch_failed', reason: '节点上已无任务，采集中断' }
    }
    return { action: 'none', reason: '节点任务进行中' }
  }

  return { action: 'none', reason: `未定义的状态组合：${platformStatus}/${nodeState}` }
}

/**
 * 前端兜底归一化：旧版 bridge 不返回 node_state 时，由前端按同一套规则识别。
 *
 * 必须保留这段：bridge 与前端是两个独立发布物，只升级前端时若没有兜底，
 * 「异常结束」仍会掉进空转一小时的老坑。
 */
export function fallbackNodeState(snapshot: {
  status?: string
  percent?: number
  done?: number
  hosts?: unknown[]
}): NodeState {
  const status = (snapshot.status || '').trim()
  if (status.includes('异常结束') || status.includes('失败') || status.includes('错误')) return 'abnormal'
  if (status.includes('完成')) return 'completed'
  if (status.includes('进行中') || status.includes('收集中') || status.includes('上传中') || status.includes('打包')) {
    return 'running'
  }
  if (status.includes('不存在') || status.includes('无任务') || status.includes('暂无')) return 'none'
  if ((snapshot.percent ?? 0) > 0 || (snapshot.done ?? 0) > 0 || (snapshot.hosts?.length ?? 0) > 0) return 'running'
  return 'unknown'
}

/** 对账结论 → 用户可读提示（含下一步动作） */
export function buildSyncNotice(action: SyncAction): string {
  switch (action.action) {
    case 'adopt':
      return `${action.reason}。可点「接管」继续采集，或「取消并重试」重新开始。`
    case 'cancel_retry':
      return `${action.reason}。请点「取消并重试」清理节点任务后重新采集。`
    case 'warn_node_busy':
      return `${action.reason}。请先取消节点上的任务，再重新采集。`
    case 'patch_completed':
      return `已按节点状态修正为完成：${action.reason}`
    case 'patch_failed':
      return `已按节点状态修正为失败：${action.reason}`
    default:
      return action.reason
  }
}
