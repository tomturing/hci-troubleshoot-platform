/**
 * 两端状态对齐（custom-ui ↔ HCI 上的 acli 插件任务）。
 *
 * 第一性原理（2026-10-26 重写）：
 *   节点是任务的唯一真相源，平台 DB 里的任务只是它的**投影**。
 *   因此这里只回答两个问题，不做任何"二次印证"：
 *     1. 归属：眼前这份报告属于本次下发吗？—— 只认「下发锚定」的报告指纹；
 *     2. 映射：归属确认后，节点态直接映射平台态，不再附加"完成是否可信"的条件。
 *
 * 为什么删掉 isCompletionTrustworthy（#1277 / #1284 的实现）：
 *   旧模型要求"节点说完成"时必须拿出正向证据（压缩包 mtime 或报告已刷新），
 *   而 P1 直传成功后插件会**主动删除**压缩包 → `stat` 必然失败 → mtime 恒缺失。
 *   于是真实完成被永久判成"无法确认完成归属"，平台停在 collecting，用户被卡死
 *   （工单 Q2026101401683：节点 100%、上传成功，面板却提示"继续等待"）。
 *
 * 正确方向不是"证明节点是对的"，而是"识别报告是旧的"：
 *   只要报告不是下发前那一份，它就是本次任务的报告；此时没有理由不信它。
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
  /**
   * 节点**报告过**的压缩包路径。
   *
   * 注意它只证明"曾经生成过这个路径"，不证明文件现在还在：P1 直传成功后插件立刻自删，
   * 报告里却仍带着这个路径（工单 Q2026102701193，2026-10-27 现场）。
   * 因此判断"包还在不在"必须看 `archiveExists`，不能用本字段。
   */
  archivePath?: string
  /**
   * bridge 的 stat 结论：压缩包此刻是否真的在节点上。
   *
   * - `true`：文件存在 → 补交付在物理上成立；
   * - `false`：文件不存在 → P1 成功后自删，补交付不成立（传无可传）；
   * - `undefined`：没探测 / 旧版 bridge → **不得**据此给出补交付入口。
   */
  archiveExists?: boolean
  /**
   * 节点报告是否明确"已成功交付"（P1 的 `发送成功`）。
   *
   * 平台对 P1 直传是**零可观测**的：没有字节、没有分片、没有回执。唯一证据就是
   * acli 的这句报告。所以 P1 的交付结论只能采信它，不能套用 P2 的"平台有记录"标准。
   */
  nodeDelivered?: boolean
  /** 节点 progress 里的采集总数 */
  progressTotal?: number
  /**
   * 本次任务实际的目标节点数：`ips` 含 all 时取集群在线数，否则取 `ips.length`。
   *
   * 只用于**否决**（旧报告的规模与本任务不符），从不用于放行。
   */
  expectedHosts?: number
  /**
   * 报告指纹是否已刷新（与 create 前锚定的基线比较）。
   *
   * - `true`：报告变了 → 必然是本次任务产出的报告；
   * - `false`：报告与下发前逐字相同 → 一定是上一次任务的旧报告；
   * - `undefined`：没有基线可比（历史任务、旧 bridge、基线读取失败）。
   *
   * 由调用方从 `baselineReportHash` 与当前 `raw` 计算，纯数据入参便于测试。
   */
  reportRefreshed?: boolean
  /** 报告里列出的目标主机（无指纹时用于归属兜底判定） */
  reportHosts?: string[]
  /** 本次任务的目标主机（`ips` 展开后的实际 IP 列表） */
  targetHosts?: string[]
  /**
   * 平台侧是否已有**交付证据**（`delivery_status=delivered` 或 `relay_status=success`）。
   *
   * 为什么不能用 `platformStatus==='completed'` 代替：`completed` 只说明 acli 报采集结束，
   * 与"包是否到了日志服务器"没有因果关系（2026-10-27 现场：completed + 平台零产物）。
   * 省略时按"未交付"处理——宁可多给一次上传入口，也不要把没交付的任务说成已归档。
   */
  platformDelivered?: boolean
}

/** 报告归属：本次任务 / 上一次任务的旧报告 / 无从判断 */
export type Ownership = 'current' | 'stale' | 'unknown'

export type SyncActionKind =
  | 'none'
  /** 节点报告仍是下发前那份：不落任何终态，等本次任务的报告 */
  | 'wait_refresh'
  | 'adopt'
  | 'cancel_retry'
  | 'patch_completed'
  | 'patch_failed'
  /** 平台已完成、节点报告 P1 直传成功：把交付证据补写进平台（status 不动） */
  | 'patch_delivered'
  | 'warn_node_busy'
  /** 平台已归档但节点仍在跑：提示冲突，必要时允许"以节点为准纠正" */
  | 'warn_completed_conflict'

export interface SyncAction {
  action: SyncActionKind
  /** 判定依据，直接进提示文案与日志，避免"为什么给出这个建议"无从追溯 */
  reason: string
  /** 本次判定看到的报告归属，供调用方展示"为什么在等" */
  ownership?: Ownership
  /**
   * 仅 warn_completed_conflict 使用：为 true 时才允许把 completed 纠正回 collecting。
   * 默认不自动执行——纠正会清掉本条任务的产物记录，必须由用户显式确认。
   */
  canCorrect?: boolean
  /**
   * 仅 adopt 使用，决定按钮语义：
   * - `resume`：节点还在跑，接管后继续跟进采集进度；
   * - `deliver`：节点已采完但平台没交付，接管后直接继续交付（不重新采集）。
   */
  adoptMode?: 'resume' | 'deliver'
}

const ACTIVE_STATUSES = new Set(['pending', 'probing', 'installing', 'collecting', 'packaging', 'uploading'])

/**
 * 报文指纹：FNV-1a 32 位 + 原文长度。
 *
 * 用途只是"比较两次报告是否同一份"，不做安全用途，因此不需要加密散列。
 * 带上长度可以进一步降低碰撞概率，且长度本身就是人可读的排障线索。
 */
export function hashReport(raw: string): string {
  if (!raw) return ''
  let hash = 0x811c9dc5
  for (let i = 0; i < raw.length; i++) {
    hash ^= raw.charCodeAt(i)
    // FNV 质数 16777619，用移位避免大数乘法在某些引擎上的精度问题
    hash = (hash + (hash << 1) + (hash << 4) + (hash << 7) + (hash << 8) + (hash << 24)) >>> 0
  }
  return `${raw.length}-${hash.toString(16).padStart(8, '0')}`
}

/**
 * 判断节点报告是否属于本次下发。
 *
 * 优先级：明确指纹 > 规模否决 > 主机集合否决 > 无从判断。
 * 只有"拿到过度证据"才判 stale；信息不足时返回 unknown，由上层**按节点态为准**，
 * 绝不因为"证明不了"就停在采集中——那正是本轮要修的故障。
 */
export function resolveOwnership(input: SyncInput): { ownership: Ownership; reason: string } {
  if (input.reportRefreshed === true) {
    return { ownership: 'current', reason: '节点报告已刷新，属于本次任务' }
  }
  if (input.reportRefreshed === false) {
    return { ownership: 'stale', reason: '节点报告与下发前完全相同，仍是上一次任务的报告' }
  }

  // 没有指纹可比（历史任务 / 旧 bridge）：只用能证伪的规模与主机集合做否决
  if (input.progressTotal && input.expectedHosts && input.progressTotal !== input.expectedHosts) {
    return {
      ownership: 'stale',
      reason: `采集总数 ${input.progressTotal} 与本次目标节点数 ${input.expectedHosts} 不一致`,
    }
  }
  const targetHosts = (input.targetHosts ?? []).filter((ip) => ip && ip !== 'all')
  const reportHosts = (input.reportHosts ?? []).filter(Boolean)
  if (reportHosts.length && targetHosts.length) {
    const overlap = reportHosts.filter((ip) => targetHosts.includes(ip))
    if (!overlap.length) {
      return { ownership: 'stale', reason: '节点报告中的主机与本任务目标无交集' }
    }
  }
  return { ownership: 'unknown', reason: '没有可比的下发基线，以节点态为准' }
}

/**
 * 对账判定：给定节点报告与平台状态，返回唯一应当执行的动作。
 *
 * 幂等约定：只做"修正投影"与"清理僵尸"，绝不自动 create/重试——
 * 重复采集的代价远大于状态短暂不准。
 */
export function decideNodeSync(input: SyncInput): SyncAction {
  const { platformStatus, nodeState } = input

  // 认不出节点态时一律不动：宁可保留不一致让用户点「刷新进度」，
  // 也不要按猜测去 cancel 一个可能正常的采集任务。
  if (nodeState === 'unknown') {
    return { action: 'none', reason: '节点状态无法识别，保持平台现状', ownership: 'unknown' }
  }

  // 曾经这里有一个 `adoptedDeliver` 绕过口：接管交付时跳过归属判定直接 patch_completed。
  // 它已随"补交付不再轮询 progress"一起删除——那个绕过口存在的前提是
  // "补交付要先走一遍采集等待"，而现在的补交付只读物理事实（包在不在、多大、完整否），
  // 归属判定对它没有任何输入。保留一个没人需要的绕过口，只会成为下次误判的入口。

  const { ownership, reason: ownershipReason } = resolveOwnership(input)
  if (ownership === 'stale') {
    // 旧报告：不落终态、不计数、不空转，等本次任务的报告写出来。
    // 这是"重新收集"后最常见的几秒到几十秒状态。
    return {
      action: 'wait_refresh',
      reason: `${ownershipReason}，等待本次任务的报告`,
      ownership,
    }
  }

  // 平台上没有任务记录：节点有游离任务时只能提示，没有可修正的投影
  if (!platformStatus) {
    if (nodeState === 'running' || nodeState === 'abnormal') {
      return {
        action: 'warn_node_busy',
        reason:
          nodeState === 'running'
            ? '节点上存在非本平台下发的采集任务（只能读进度/主机，模块与时间范围无法获取）'
            : '节点上存在异常结束的任务',
        ownership,
      }
    }
    if (nodeState === 'completed') {
      return {
        action: 'none',
        reason: '节点任务已完成，但平台上没有对应任务记录（可能已清理）',
        ownership,
      }
    }
    return { action: 'none', reason: '两端均无任务', ownership }
  }

  if (platformStatus === 'completed') {
    // 归档是既成事实：节点上的 running 只可能是"归档之后又发起了新任务"，
    // 不自动回退，交给用户点「重新收集」。
    if (nodeState === 'running') {
      // 只有"报告已确认刷新"才允许用户把归档纠正回采集中：
      // 报告没刷新时已经在上面按 wait_refresh 拦住了，这条分支的 current 意味着
      // 归档之后节点上确实跑起了新任务，纠正与否交给用户点一次。
      return {
        action: 'warn_completed_conflict',
        canCorrect: ownership === 'current',
        reason: '平台已归档，节点上有新的采集任务在运行；如需重收请点「重新收集」',
        ownership,
      }
    }
    if (nodeState === 'abnormal') {
      return {
        action: 'warn_completed_conflict',
        canCorrect: false,
        reason: '平台已归档，但节点任务异常结束，建议核对归档产物',
        ownership,
      }
    }

    // 「采集完成」≠「已交付」，但「未登记交付」也≠「未交付」：
    // P1 的交付对平台不可观测，节点报告是唯一证据。报告说发送成功、平台却没记录，
    // 只能说明**平台漏记**，不能据此让用户重传一个已被插件删除的包
    // （工单 Q2026102701193，2026-10-27 现场：acli 报发送成功、zip 已自删，
    //  面板却一直提示「压缩包仍在节点，请点上传到平台」，点下去 stat 必然 exit 1）。
    if (!input.platformDelivered && input.nodeDelivered) {
      return {
        action: 'patch_delivered',
        reason: '节点报告 P1 直传日志服务器成功，平台缺这条记录；按已交付补登（平台不留副本属 P1 的已知代价）',
        ownership,
      }
    }

    // 未交付：补交付的物理前提只有一条——**文件此刻真的在节点上**。
    // 注意判据是 archiveExists（stat 结论），不是 archivePath（报告里的字符串）。
    if (!input.platformDelivered) {
      if (nodeState === 'completed' && input.archiveExists === true) {
        return {
          action: 'adopt',
          reason: '平台已完成采集但日志尚未交付（压缩包仍在节点）；可经平台中转直接上传，不会重新采集',
          ownership,
          adoptMode: 'deliver',
        }
      }
      if (nodeState === 'completed') {
        return {
          action: 'none',
          reason:
            input.archiveExists === false
              ? '节点已采集完成且压缩包已不在（P1 发送成功后插件会自删），无需再上传；如需产物请重新采集'
              : '节点已采集完成，但未确认压缩包是否仍在节点（缺少 stat 结论）；如需产物请重新采集',
          ownership,
        }
      }
      return {
        action: 'none',
        reason: '平台已完成采集但无交付记录，节点上也没有可交付的压缩包；如需归档请重新采集',
        ownership,
      }
    }
    return { action: 'none', reason: '平台已归档，节点无任务', ownership }
  }

  if (platformStatus === 'failed' || platformStatus === 'cancelled') {
    // failed / cancelled：节点仍在跑 → 接管；节点异常 → 清理后重试
    if (nodeState === 'running') {
      return {
        action: 'adopt',
        reason: '平台已判失败/取消，但节点任务仍在运行，可接管继续',
        ownership,
        adoptMode: 'resume',
      }
    }
    if (nodeState === 'abnormal') {
      return { action: 'cancel_retry', reason: '节点任务异常结束，需清理后重试', ownership }
    }
    if (nodeState === 'completed') {
      // 这里曾经直接给 patch_completed，与后端状态机正面冲突：
      // failed 是终态，唯一合法出口是 → collecting（接管）。于是每次对账都被
      // 409 拒绝，30 秒一轮无限刷屏，节点上已经采好的包永远交付不出去
      // （工单 Q2026102701294：节点 100%、包 289 MiB 在盘，平台 failed）。
      // 正确做法不是"硬把终态改成完成"（那会说谎：平台根本没有产物），
      // 而是承认节点真相 → 走合法边回到采集中，随后立刻按 completed 收尾并交付。
      if (input.archiveExists === true) {
        return {
          action: 'adopt',
          reason: '节点已采集完成且压缩包仍在，平台只是没交付；接管后继续交付，不会重新采集',
          ownership,
          adoptMode: 'deliver',
        }
      }
      // 无残留包：P1 直传成功时插件会自删，此时没有可交付的物证。
      // 强行接管只会拉包失败再挂一次，所以只提示、不写库、也不产生 409。
      return {
        action: 'none',
        reason: '节点已采集完成，但压缩包已不在且平台无产物记录；如需产物请重新采集',
        ownership,
      }
    }
    return { action: 'none', reason: '节点无任务，平台终态与之一致', ownership }
  }

  if (ACTIVE_STATUSES.has(platformStatus)) {
    // 归属已确认（或无从证伪）→ 节点说什么就是什么，不再附加任何完成判据
    if (nodeState === 'completed') {
      return { action: 'patch_completed', reason: '节点任务已完成', ownership }
    }
    if (nodeState === 'abnormal') {
      return { action: 'patch_failed', reason: '节点任务异常结束', ownership }
    }
    if (nodeState === 'none') {
      if (input.archivePath) {
        return { action: 'patch_completed', reason: '节点任务已结束且压缩包路径已生成', ownership }
      }
      return { action: 'patch_failed', reason: '节点上已无任务，采集中断', ownership }
    }
    return { action: 'none', reason: '节点任务进行中', ownership }
  }

  return {
    action: 'none',
    reason: `未定义的状态组合：${platformStatus}/${nodeState}`,
    ownership,
  }
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

/**
 * 下发新任务前的守卫：这个对账结论是否应该**阻止** create。
 *
 * 只拦"节点上确实有活、会与新 create 抢同一会话"的情形：
 *   - `cancel_retry` / `warn_node_busy`：节点任务未结束或游离，create 必被插件互斥拒绝；
 *   - `adopt(resume)`：节点还在跑，同样会撞互斥。
 *
 * 刻意**不拦** `adopt(deliver)`：它表示"节点已采完、包还在盘、平台没交付"，
 * 而 acli 的 completed 任务并不与新的 create 互斥。把它也拦下来就会得到
 * "既不能新建、也补交付不了"的死锁（Q2026102701193，2026-10-27 现场）。
 */
export function blocksCreate(action: SyncAction | null | undefined): boolean {
  if (!action) return false
  if (action.action === 'cancel_retry' || action.action === 'warn_node_busy') return true
  return action.action === 'adopt' && action.adoptMode !== 'deliver'
}

/** 对账结论 → 用户可读提示（含下一步动作） */
export function buildSyncNotice(action: SyncAction): string {
  switch (action.action) {
    case 'wait_refresh':
      return `${action.reason}（进度与状态暂不更新，自动刷新会继续跟进）`
    case 'adopt':
      // 补交付只有一条物理路径（acli 没有独立重传接口，只能经平台中转），
      // 因此这里不再给"重新采集"的选项：包已经在盘上，重采是纯粹的浪费。
      return action.adoptMode === 'deliver'
        ? `${action.reason}。请点「上传到平台」经平台中转交付（不会重新采集）。`
        : `${action.reason}。可点「接管」继续采集，或「取消并重试」重新开始。`
    case 'cancel_retry':
      return `${action.reason}。请点「取消并重试」清理节点任务后重新采集。`
    case 'warn_node_busy':
      return `${action.reason}。请先取消节点上的任务，再重新采集。`
    case 'patch_completed':
      return `已按节点状态修正为完成：${action.reason}`
    case 'patch_failed':
      return `已按节点状态修正为失败：${action.reason}`
    case 'patch_delivered':
      // 不是"已归档"的高调宣告：平台只是补上节点早已给出的结论，产物并不在平台侧。
      return `日志已交付：${action.reason}`
    case 'warn_completed_conflict':
      return action.canCorrect
        ? `${action.reason}。可点「以节点为准纠正」把平台状态改回采集中并继续跟进（不会删除节点或日志服务器上的压缩包）。`
        : action.reason
    default:
      return action.reason
  }
}
