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
  /**
   * 本次任务实际的目标节点数：`ips` 含 all 时取集群在线数，否则取 `ips.length`。
   *
   * 不能拿"集群在线节点数"当基准：任务可以只选 1 台（`ips=['172.28.24.130']`），
   * 而历史报告里的 total 是上一次 all（3 台）。基准取错会让假完成照样通过校验
   * ——工单 Q2026101401683 的 19:55 就是 3 对 3 通过、但本次只采 1 台。
   */
  expectedHosts?: number
  /**
   * `acli log_collect create` **下发之前**节点打的那份 progress 原文。
   *
   * 一次 create 不会让报告立刻刷新：下发后的第一次（甚至前几十秒）`progress`
   * 仍是上一次任务的完成报告。报告原文是唯一能证明"这次看到的完成是新的"的证据——
   * 2026-10-26 20:38 的假完成，两次报告的 raw 长度完全相同（2485）。
   */
  baselineRaw?: string
  /** 当前这份完成报告的原文，与 baselineRaw 比较判断报告是否已刷新 */
  currentRaw?: string
  /**
   * 压缩包是否真实存在：由 bridge 明确告知。
   *
   * P1 直传模式下插件上传成功后会自删压缩包，此时 `archiveMTime` 必然拿不到，
   * 不能把"包不存在"误当成"时间未知"再拿规模证据放行。
   */
  archiveExists?: boolean
}

export type SyncActionKind =
  | 'none'
  | 'adopt'
  | 'cancel_retry'
  | 'patch_completed'
  | 'patch_failed'
  | 'warn_node_busy'
  /** 平台已归档但节点仍在跑：提示冲突，必要时允许"以节点为准纠正" */
  | 'warn_completed_conflict'

export interface SyncAction {
  action: SyncActionKind
  /** 判定依据，直接进提示文案与日志，避免"为什么给出这个建议"无从追溯 */
  reason: string
  /**
   * 仅 warn_completed_conflict 使用：为 true 时才允许把 completed 纠正回 collecting。
   * 默认不自动执行——纠正会清掉本条任务的产物记录，必须由用户显式确认。
   */
  canCorrect?: boolean
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
 * 于是平台写 completed、节点其实还在跑。
 *
 * 判据分两类，缺一不可：
 *
 * 一、否决（拿到即不成立，属于"明确证伪"）
 *   1. 包时间存在且早于任务创建时间 − 容忍值；
 *   2. total 与本次任务目标节点数不一致；
 *   3. 报告原文与 create 前完全一致——下发后报告没刷新过，看到的必然是上一次的结论。
 *
 * 二、正向证据（必须至少拿到一条，否则只能算"无法确认"）
 *   1. 报告原文已变化（说明插件为本次任务产出了新报告）；
 *   2. 包存在且 mtime 晚于任务创建时间（包是这次写出来的）。
 *
 * 早先的实现把"规模证据通过"当成放行条件（OR），结果在
 * 「同工单只收同一台主机」时 total 与 expectedHosts 恒等（1 对 1）、
 * 而 P1 直传后压缩包被插件自删导致 mtime 恒为 0，两条判据同时失效——
 * Q2026101401683 的 20:38 任务就是这样在 2.8 秒内被写成 completed，
 * 而节点上实际还在 0% 采集。
 */
export function isCompletionTrustworthy(input: SyncInput): {
  ok: boolean
  reason: string
  /**
   * 是否拿到了"明确证伪"的证据（包早于任务、total 与目标数不符、报告没刷新）。
   *
   * 与"无法确认"必须区分：`refuted=true` 才允许把已完成纠正回采集中；
   * 只是缺证据时只提示、不给按钮——误把真归档改回采集的代价，比多等一次刷新大得多。
   */
  refuted?: boolean
} {
  if (!input.archivePath) {
    // 缺字段不等于数据矛盾（历史记录可能没落路径），归入"无法确认"而不是"证伪"
    return { ok: false, refuted: false, reason: '完成态没有压缩包路径，无法核对' }
  }

  const hasCreatedAt = Boolean(input.taskCreatedAt)
  // mtime 有值且包确实存在，才算拿到"包是这次写的"这条时间证据
  const freshMTime =
    Boolean(input.archiveMTime && hasCreatedAt) && input.archiveExists !== false
      ? (input.archiveMTime as number)
      : 0
  if (freshMTime > 0 && freshMTime < (input.taskCreatedAt as number) - ARCHIVE_FRESHNESS_TOLERANCE_SEC) {
    return { ok: false, refuted: true, reason: '压缩包早于本次任务创建时间，判定为历史任务报告' }
  }

  if (input.progressTotal && input.expectedHosts && input.progressTotal !== input.expectedHosts) {
    return {
      ok: false,
      refuted: true,
      reason: `采集总数 ${input.progressTotal} 与本次任务目标节点数 ${input.expectedHosts} 不一致，判定为历史任务报告`,
    }
  }

  const hasReportPair = Boolean(input.baselineRaw && input.currentRaw)
  if (hasReportPair && input.baselineRaw === input.currentRaw) {
    return {
      ok: false,
      refuted: true,
      reason: '节点报告与本次任务下发前完全相同（报告未刷新），判定为上一次任务的完成报告',
    }
  }

  const hasPositiveEvidence = (hasReportPair && input.baselineRaw !== input.currentRaw) || freshMTime > 0
  if (!hasPositiveEvidence) {
    return {
      ok: false,
      refuted: false,
      reason: '既没有新的节点报告，也拿不到压缩包时间，无法确认完成归属',
    }
  }
  return { ok: true, reason: hasReportPair ? '节点报告已刷新且规模/时间判据通过' : '压缩包时间属于本次任务' }
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
      // 归档态默认不动，但"节点仍在跑"必须先分清是哪种情况：
      //   - 完成判据全过 → 是完成之后又发起的新任务，归档无误，只提示；
      //   - 判据不过 → 平台完成是假完成（历史报告），允许用户以节点为准纠正。
      if (nodeState === 'running') {
        const trust = isCompletionTrustworthy(input)
        if (trust.ok) {
          return {
            action: 'warn_completed_conflict',
            canCorrect: false,
            reason: '平台已归档，节点上有新的采集任务在运行',
          }
        }
        // 只有拿到明确证伪证据才允许纠正；"无法确认"只提示
        return {
          action: 'warn_completed_conflict',
          canCorrect: trust.refuted === true,
          reason: trust.refuted
            ? `平台完成判定不可信（${trust.reason}），节点任务仍在运行`
            : `平台完成判定无法确认（${trust.reason}），节点任务仍在运行`,
        }
      }
      if (nodeState === 'abnormal') {
        return {
          action: 'warn_completed_conflict',
          canCorrect: false,
          reason: '平台已归档，但节点任务异常结束，建议核对归档产物',
        }
      }
      return { action: 'none', reason: '平台已归档，节点无任务' }
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
    case 'warn_completed_conflict':
      return action.canCorrect
        ? `${action.reason}。可点「以节点为准纠正」把平台状态改回采集中并继续跟进（不会删除节点或日志服务器上的压缩包）。`
        : action.reason
    default:
      return action.reason
  }
}
