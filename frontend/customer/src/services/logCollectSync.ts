/**
 * 两端状态对齐（custom-ui ↔ HCI 上的 acli 插件任务）。
 *
 * 第一性原理：节点是任务的唯一真相源，平台 DB 里的任务只是它的**投影**。
 * 但"真相源"不等于"可以无条件采信"——**必须先证明这份报告属于哪一次下发**，
 * 否则投影会把上一轮的状态写到新任务上，比不更新更糟。
 *
 * 三层判据（2026-11-06 重建，见 Q2026110666667）：
 *
 *   L1 归属正向前置门（`SESSION_ADOPT_GRACE_MS`）：
 *     新任务必须**观测到节点 running（采集起点）**，才允许把 completed 映射为完成。
 *     根因：acli 是节点级单任务，`create` 不返回句柄、`progress` 不带任务 ID，
 *     下发后到插件真正接单之间读到的仍是上一次任务的报告。窗口 10 分钟兜底，
 *     兼顾"页面重开接管已跑完任务"的历史场景（Q2026101401683）。
 *
 *   L2 否决性证据（只拦不放行）：
 *     - 时间一致性：报告自称 `Total time` > 任务年龄 + 120s → 必是更早任务的报告；
 *     - 逐字相同：报告与下发前完全一致 → 必是旧报告；
 *     - 假 Success：从未观测到上传过程且压缩包已不在 → 不写 delivered。
 *
 *   L3 状态机单调：
 *     终态（completed/failed/cancelled）不会被节点报告自动改写，只能由用户显式操作离开。
 *
 * 为什么删掉"指纹变化 = 属于本次任务"（#1284 的实现）：
 *   它假设 create 后 acli 只是**逐字复读**上一份报告（两次 raw 完全一致）。实测同一任务
 *   在上传阶段会持续改写完成报告（`Total time` 03:09.0 → 03:38.9、raw 长度 2485 → 2776），
 *   指纹随之变化，"报告变了"于是既可能是新任务、也可能是同一任务的演进，无法区分。
 *   归属的正确锚点是**观测到的采集起点**，文本指纹只保留"完全相同 → 必是旧的"这一侧。
 *
 * 为什么也不能要求"完成必须自证"（#1277 / #1284 的旧模型）：
 *   P1 直传成功后插件会主动删除压缩包 → `stat` 必然失败 → mtime 恒缺失，
 *   真完成会被永久判成"无法确认"，平台停在 collecting（Q2026101401683）。
 *   所以 L2 只做否决，L1 才是放行依据，且 L1 有 10 分钟兜底出口。
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

/**
 * 交付证据有界等待窗口（10 分钟）。
 *
 * 从「采集完成」起算：acli 的完成报告可以在包落盘、上传完成之前发出，
 * 因此在完成瞬间既不能写 delivered，也不能写 failed。窗口内只做幂等观察，
 * 超时后收敛为「结果未知」并保留人工入口，绝不等成死循环、也不把未知写成失败。
 * 固定常量而非自适应：先用指标观察真实打包耗时，再考虑调整，避免引入新复杂度。
 */
export const DELIVERY_EVIDENCE_WAIT_MS = 10 * 60 * 1000

/**
 * 冷启动窗口（L1）：create 后这段时间内，未见「采集起点」的完成报告一律不采信。
 *
 * 为什么需要：acli 是**节点级单任务**且 `create` 不返回任何句柄，`progress` 也不带任务 ID。
 * 新任务下发后到插件真正切换任务之间，存在一段"读到的仍是上一次任务报告"的真空期
 * （2026-11-06 Q2026110666667：22:08:42 建任务，22:08:45 就拿到上一轮的 100% 完成报告，
 * 3 秒被判完成，而节点 22:09:31 才从 0% 真正开始）。
 *
 * 判据不靠"报告变化"（同一任务在上传阶段会持续改写 Total time，指纹必然变化），
 * 而是要求**正向证据**：必须观测到节点 `running` 才算本次任务接管了会话。
 *
 * 窗口取 10 分钟而不是几秒：acli 一次采集最短也在 3 分钟量级（现场实测 00:03:38.9），
 * 5 秒轮询必然能抓到 running。窗口同时是**兜底出口**——页面关闭后重开、接管一个
 * 早已跑完的任务时，永远看不到 running，超窗后按节点态放行，避免重演
 * Q2026101401683「真完成被永久卡在采集中」。
 */
export const SESSION_ADOPT_GRACE_MS = 10 * 60 * 1000

/**
 * create 下发后的接单宽限（90 秒）。
 *
 * 与 10 分钟的 L1 窗口不是一回事：那道门防的是"把上一轮完成报告当成本轮"，
 * 这里防的是"插件还没注册任务，平台就宣布采集中断"。create 是同步命令，
 * 正常接单在秒级可见，所以窗口必须短且是硬上限——窗口太长会把真实的
 * 接单失败掩盖成永久转圈（2026-11-09 Q2026110901673：create 后 6 秒即写 failed）。
 *
 * 只对「本次会话亲自下发、且从未观测到 running」的任务生效；接管历史任务
 * 与已经见过起点的任务都不等，避免 Q2026101401683 那种真完成被卡住。
 */
export const CREATE_ACK_GRACE_MS = 90 * 1000

/**
 * `Total time` 否决的时钟松弛（秒）。
 *
 * 平台的 `created_at` 与 acli 内部计时起点之间隔着"命令下发 → 插件接单"的几秒到几十秒，
 * 再叠加轮询采样延迟，因此只有当报告总耗时**明显超过**任务年龄时才判旧报告。
 * 取 120 秒：足以覆盖下发延迟，又远小于一次真实采集（3 分钟起）。
 */
export const TOTAL_TIME_SLACK_SECONDS = 120

/** 落盘侧核验结论（服务端按产物名回查日志服务器归档件），三态不接受布尔 */
export type ArchiveVerification = 'delivered' | 'failed' | 'none'

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
   * - `true`：报告变了；
   * - `false`：报告与下发前逐字相同 → 一定是上一次任务的旧报告；
   * - `undefined`：没有基线可比（历史任务、旧 bridge、基线读取失败）。
   *
   * **重要（2026-11-06 订正）**：`true` 只是"报告变了"，**不再等于"属于本次任务"**。
   * acli 在同一任务的上传阶段会持续改写完成报告（`Total time` 从 03:09.0 涨到 03:38.9，
   * 还会追加发送状态行），同一份任务的报告指纹因此会反复变化。归属的正向证据改由
   * `sessionAdopted`（L1）承担，指纹只保留"逐字相同 → 必是旧报告"这个否决语义。
   */
  reportRefreshed?: boolean
  /**
   * L1 正向证据：是否已观测到**本次任务的采集起点**（节点 `running`）。
   *
   * acli 无任务 ID、无句柄，`progress` 又不带归属，所以"新任务已被接单"这件事
   * 只能靠一次 running 观测来证明。
   */
  sessionAdopted?: boolean
  /**
   * 是否**要求**先拿到起点证据才允许写完成态（L1 门的开关）。
   *
   * 只在"本次会话亲自 create 的任务"上为 true。历史任务接管必须为 false：
   * 用户关掉页面、十分钟后重开去接管一个早已跑完的任务时，永远看不到 running，
   * 一旦误开这个门就会重演 Q2026101401683「真完成被永久卡在采集中」。
   *
   * 缺省 false —— 宁可少拦，也不能把合法的接管路径挡死。
   */
  requireSessionAdopt?: boolean
  /** 平台任务的年龄（毫秒），仅用于 `SESSION_ADOPT_GRACE_MS` 兜底放行 */
  taskAgeMs?: number
  /**
   * 报告自称的总耗时（秒），由 `parseAcliTotalTime` 从 `Total time: 00:03:38.9` 解析。
   *
   * 它同时是**独立于文本指纹**的归属证据：任务只存在 3 秒，报告却自称跑了 3 分 38 秒，
   * 这份报告必然属于更早的任务。缺省表示报告里没这一段，不做否决。
   */
  reportTotalSeconds?: number
  /** 平台任务已运行的秒数，与 `reportTotalSeconds` 配对做时间一致性否决 */
  taskAgeSeconds?: number
  /**
   * 本次下发过程中是否**观测到过上传阶段**（`upload_state` 为 sending/uploading）。
   *
   * 用于识别「采集完成瞬间的假 Success」：acli 在采集结束、上传尚未开始时就会报一次
   * `Status: Success`，随后回落到 `Sending`（2026-11-06 实测 Success → Sending → Success）。
   * 从未见过上传过程、且压缩包已不在节点时，这个 Success 不足以证明已交付。
   */
  uploadSeen?: boolean
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
  /**
   * 落盘侧归档核验结论。它是**唯一权威的交付证据源**：
   * 节点自述（`nodeDelivered`）只是 acli 的一面之词，包在盘（`archiveExists`）只证明可补交付。
   *
   * `failed` 表示"这一刻日志服务器上没有"，**不等于最终失败**——大包可能还没传完。
   * 因此 `failed` 不触发写库，只作为继续等待的依据；`none` 表示接口不可用/没结论。
   */
  archiveVerified?: ArchiveVerification
  /**
   * 自「采集完成」起已等待的毫秒数。仅用于 `platformStatus === 'completed'` 的收敛判定，
   * 由调用方从单调起点计算，纯数据入参便于穷举测试边界。
   */
  deliveryWaitElapsedMs?: number
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
  /**
   * 已完成但尚无交付证据：不写任何终态，继续幂等观察（10 分钟窗口内）。
   * 与「不要求完成态自证」的历史修复（#1277/#1284，Q2026101401683）不冲突：
   * 本动作只约束**交付结论**，绝不拦 `status=completed` 的采集结论。
   */
  | 'wait_delivery'
  /** 等待窗口耗尽仍无证据：结果未知。不写 failed，保留人工核验与补交付入口 */
  | 'delivery_unknown'
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
  /**
   * 失败时的规范错误码（LC-<LAYER>-<NNN>）。
   *
   * 判定层比调用方更清楚"这次到底败在哪一层"：节点从未接单不能沿用
   * 桥侧的旧码（往往为空），必须由判定直接给出 LC-PLUGIN-008。
   */
  errorCode?: string
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
 * 解析 acli 报告的 `Total time`（形如 `00:03:38.9`、`58:11`、`1:02:03`）为秒。
 *
 * 解析失败返回 `null`：拿不到就**不做否决**，绝不把"没解析出来"当成"时间对得上"。
 * 这段文本是 acli 唯一的自证耗时字段（`Total time` / `总耗时` 两种语言，
 * bridge 已按标签归一），与包名、工单号都无关，因此同一工单重复采集时仍然可用。
 */
export function parseAcliTotalTime(raw?: string | null): number | null {
  const text = (raw || '').trim()
  if (!text) return null
  // 兼容 `HH:MM:SS.s` 与 `MM:SS.s`：acli 不足一小时时省略小时段。
  const match = /^(?:(\d+):)?(\d{1,2}):(\d{1,2}(?:\.\d+)?)$/.exec(text)
  if (!match) return null
  const hours = match[1] ? Number(match[1]) : 0
  const minutes = Number(match[2])
  const seconds = Number(match[3])
  if (!Number.isFinite(hours) || !Number.isFinite(minutes) || !Number.isFinite(seconds)) return null
  return hours * 3600 + minutes * 60 + seconds
}

/** 秒 → `Xm Ys` 的可读文案，只用于提示语，不参与判定 */
function formatDuration(totalSeconds: number): string {
  const safe = Math.max(0, Math.round(totalSeconds))
  if (safe < 60) return `${safe} 秒`
  return `${Math.floor(safe / 60)} 分 ${safe % 60} 秒`
}

/**
 * 判断节点报告是否属于本次下发。
 *
 * 优先级（逐级都是**否决**，最后才给 unknown）：
 *   1. 时间一致性（L2）：报告总耗时 > 任务年龄 → 必是更早任务的报告；
 *   2. 逐字相同 → 必是旧报告；
 *   3. 指纹变化 → 只记"报告变了"，**不再直接判 current**（见下方注释）；
 *   4. 规模否决 > 主机集合否决 > 无从判断。
 *
 * 只有"拿到过度证据"才判 stale；信息不足时返回 unknown，由上层按节点态为准，
 * 绝不因为"证明不了"就停在采集中——那正是 Q2026101401683 的故障。
 * 但"放行完成态"另有 L1 门（必须先看到采集起点），不依赖本函数。
 */
export function resolveOwnership(input: SyncInput): { ownership: Ownership; reason: string } {
  // L2-1：时间一致性否决。它不依赖文本是否变化，是当前最强的一条归属证据。
  // 2026-11-06 Q2026110666667：新任务创建 3 秒，报告自称 Total time 00:03:38.9，
  // 而旧实现只看"指纹变了"就放行，直接把 3 秒的任务写成完成。
  if (
    input.reportTotalSeconds != null &&
    input.taskAgeSeconds != null &&
    input.reportTotalSeconds > input.taskAgeSeconds + TOTAL_TIME_SLACK_SECONDS
  ) {
    return {
      ownership: 'stale',
      reason: `节点报告总耗时 ${formatDuration(input.reportTotalSeconds)} 超过本任务已运行 ${formatDuration(
        input.taskAgeSeconds,
      )}，不可能是本次任务的报告`,
    }
  }
  if (input.reportRefreshed === false) {
    return { ownership: 'stale', reason: '节点报告与下发前完全相同，仍是上一次任务的报告' }
  }
  // 注意这里**不再**返回 current：同一任务在上传阶段会持续改写报告（Total time 递增、
  // 追加发送状态行），"变了"既可能是新任务、也可能是同一任务的演进，无法区分。
  // 归属的正向证据交给 L1（decideNodeSync 里的采集起点门），本函数只负责否决。

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
  if (input.reportRefreshed === true) {
    return {
      ownership: 'unknown',
      reason: '节点报告已变化，但无法排除它是上一次任务上传阶段的报告演进，需等采集起点确认',
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
function decideNodeSyncUnchecked(input: SyncInput): SyncAction {
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
      // 只有"报告确实不是旧的"才允许用户把归档纠正回采集中：
      // 报告逐字未变时已经在上面按 wait_refresh 拦住了，能走到这里的要么报告已变、
      // 要么无从比对，都属于"归档之后节点上可能真跑起了新任务"，纠正与否交给用户点一次。
      // 注意 L3 只禁止**自动**回退，用户显式点「纠正」是状态机允许的边。
      return {
        action: 'warn_completed_conflict',
        // 报告逐字未变已在上面按 wait_refresh 拦掉，走到这里的都不是旧报告
        canCorrect: true,
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

    // 「采集完成」≠「已交付」。证据优先级严格按可观测强度排序：
    //   落盘侧归档核验 > 节点自述「发送成功」 > 包仍在盘（只是可补交付，不是已交付）。
    // 有界等待只作用于交付结论，不拦 status=completed：
    // 历史故障 Q2026101401683 就是"要求完成态自证"把真实完成永久卡在 collecting，
    // 这个坑不能借本次改动复活（#1277/#1284）。
    const elapsed = input.deliveryWaitElapsedMs ?? 0
    const waitForEvidence = (reason: string): SyncAction =>
      elapsed < DELIVERY_EVIDENCE_WAIT_MS
        ? { action: 'wait_delivery', reason, ownership }
        : {
            action: 'delivery_unknown',
            reason: `${reason}；已等待 ${Math.round(elapsed / 60000)} 分钟仍无归档证据，结果未知，请手动「刷新进度」或点「上传到平台」确认`,
            ownership,
          }

    // 落盘侧明确否定：也不能立刻写 failed。大包可能还在打包/上传途中，
    // 此刻"服务器上没有"只是瞬时观测（Q2026101201081：16:17 判完成、16:18 包才落盘）。
    if (!input.platformDelivered && input.archiveVerified === 'failed') {
      return waitForEvidence('日志服务器上暂未查到归档件，正在等待上传/归档确认')
    }
    // 落盘侧确认存在：最强证据，与节点态无关
    if (!input.platformDelivered && input.archiveVerified === 'delivered') {
      return {
        action: 'patch_delivered',
        reason: '日志服务器上已查到一致的归档件，平台缺这条记录；按已交付补登',
        ownership,
      }
    }
    // 核验不可用（旧后端 / 网络异常）时保留节点自述这条兼容路径：
    // 报告说发送成功、平台却没记录，只能说明平台漏记，不能让用户重传已被插件删除的包
    // （工单 Q2026102701193，2026-10-27 现场）。
    if (!input.platformDelivered && input.nodeDelivered && input.archiveVerified !== 'failed') {
      // L2-3：从未观测到上传过程、且压缩包已不在节点时，这一句 Success 不足以证明已交付。
      //
      // acli 的状态序列是 `Success → Sending → Success`（2026-11-06 实测）：采集阶段一结束
      // 就先报一次 Success，真正上传却要等到之后，中途回落到 Sending 跑 55 分钟。
      // 若把第一次 Success 当交付，平台会记下一条**没有任何产物**的 delivered
      // （5ad6842a：22:07:47 标 delivered，archive_exists=false、size=0，22:10:45 才真正开始传）。
      //
      // 判据刻意用"没见过 Sending"而不是"包不存在"单独成立：包被插件自删是 P1 成功后的
      // 正常现象，只有**同时**满足"没见过上传"才能断定这个 Success 是采集阶段的假信号。
      //
      // 这条门只在"本次会话亲自下发"时生效（requireSessionAdopt）：接管一个早已结束的历史任务
      // 永远不可能看到上传过程，一旦误开此门就会把真交付永远判成"待确认"
      // （Q2026102701193 的教训——插件自删包 + 平台漏记，只能靠节点自述补登）。
      if (
        input.requireSessionAdopt === true &&
        input.archiveExists === false &&
        input.uploadSeen !== true
      ) {
        return waitForEvidence(
          '节点报告发送成功，但从未观测到上传过程且压缩包已不在节点；可能是采集完成瞬间的状态，正在确认日志服务器归档件',
        )
      }
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
        return waitForEvidence(
          input.archiveExists === false
            ? '采集已完成，压缩包已不在节点且日志服务器暂未查到归档件；正在确认插件是否已直传成功'
            : '采集已完成，正在确认日志服务器归档件（尚未拿到交付证据）',
        )
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
      //
      // 三种输入必须给三种说法：`false` 是"量过、确实不在"；`undefined` 是"没量到"
      // （旧版 bridge 不返回该字段 / 探测被路径白名单拒 / 会话不可用）。
      // 曾经两者共用一句"压缩包已不在"，于是 Q2026102701403 现场（包 455 MiB 在盘、
      // 桥版本早于 archive_exists 字段）被界面断言成"包已不在"，用户只能再采一遍。
      return {
        action: 'none',
        reason:
          input.archiveExists === false
            ? '节点已采集完成，压缩包已不在且平台无产物记录；如需产物请重新采集'
            : '节点已采集完成，但未确认压缩包是否仍在节点（桥未上报探测结论或探测失败）；如需产物请先在节点上确认压缩包，或重新采集',
        ownership,
      }
    }
    return { action: 'none', reason: '节点无任务，平台终态与之一致', ownership }
  }

  if (ACTIVE_STATUSES.has(platformStatus)) {
    // L1：采集起点门。新任务下发后到插件真正接单之间，`progress` 读到的可能仍是
    // 上一次任务的完成报告（acli 无任务 ID、无句柄，progress 也不带归属）。
    // 唯一能证明"这份报告属于本次任务"的正向证据，是**看到过它从 running 起步**。
    //
    // 为什么不能只看"报告变了"：同一任务在上传阶段会持续改写报告
    // （2026-11-06 Q2026110666667，Total time 03:09.0 → 03:38.9，raw 长度 2485 → 2776），
    // 指纹必然变化，旧判据因此把上一轮的完成报告放行，3 秒的任务被写成完成。
    //
    // 窗口是**兜底出口**：页面关闭后重开、接管早已跑完的任务时永远看不到 running，
    // 超窗后按节点态放行，避免重演 Q2026101401683「真完成被永久卡住」。
    const taskAgeMs = input.taskAgeMs
    if (
      nodeState === 'completed' &&
      input.requireSessionAdopt === true &&
      input.sessionAdopted !== true &&
      taskAgeMs != null &&
      taskAgeMs < SESSION_ADOPT_GRACE_MS
    ) {
      return {
        action: 'wait_refresh',
        reason: `尚未观测到本次任务的采集起点（节点始终未出现 running），完成报告可能来自上一次任务；继续等待（本任务已运行 ${formatDuration(
          taskAgeMs / 1000,
        )}，最多等待 ${formatDuration(SESSION_ADOPT_GRACE_MS / 1000)}）`,
        ownership,
      }
    }
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
      // 本次会话亲自下发、且从未见过采集起点时，先给插件一段注册任务的时间。
      // 三个条件缺一不可：接管/重开页面不等待，见过 running 的任务不等待，
      // 超窗不等待——否则真实接单失败会被掩盖成永久转圈。
      if (
        input.requireSessionAdopt === true &&
        input.sessionAdopted !== true &&
        input.taskAgeMs != null &&
        input.taskAgeMs < CREATE_ACK_GRACE_MS
      ) {
        return {
          action: 'wait_refresh',
          reason: '任务已下发，节点尚未接单；正在等待采集起点（acli 注册任务可能有秒级延迟）',
          ownership,
        }
      }
      // 文案必须区分"曾经跑过之后消失"与"这次根本没接单"：
      // 现场 Q2026110901673 的 create 从未落地，界面却断言「采集中断」，
      // 把用户引向"重新采集"而不是"检查节点 acli"。
      const adopted = input.sessionAdopted === true
      return {
        action: 'patch_failed',
        reason: adopted
          ? '节点上已无任务，采集中断'
          : '任务下发后节点未建立采集任务；请核对节点 acli 版本与所选专项，完整节点输出见任务详情',
        // 015 专指"下发后未建立"；adopted 是"跑过之后消失"，仍归 008（任务不存在）。
        // 两者都写 008 会让统计把"从没接单"算成"任务不存在"，处置动作完全不同。
        errorCode: adopted ? 'LC-PLUGIN-008' : 'LC-PLUGIN-015',
        ownership,
      }
    }
    return { action: 'none', reason: '节点任务进行中', ownership }
  }

  return {
    action: 'none',
    reason: `未定义的状态组合：${platformStatus}/${nodeState}`,
    ownership,
  }
}

/** 平台侧终态：只能由用户显式操作离开，判定层不得自动回退 */
const TERMINAL_STATUSES = new Set(['completed', 'failed', 'cancelled'])

/**
 * L3：状态机单调性守卫。
 *
 * 第一性原理：终态一旦写入就是既成事实，后续读到什么报告都不能把它"自动改正"回来——
 * 那等于平台自己推翻自己已经给用户的结论，而且每一次抖动都会被下一轮对账再推翻一次。
 * 要离开终态只有两条路，且必须由用户触发：「重新收集」新建任务、或「接管」走状态机
 * 允许的 failed → collecting 边。
 *
 * 因此判定层对终态任务只允许两类动作：
 *   - 写**交付证据**（`patch_delivered`、`uploading` 阶段推进）：改的是交付维度，不是 status；
 *   - 灰度提示（`warn_completed_conflict`、`wait_delivery`、`delivery_unknown`）。
 *
 * 任何试图把终态改写成 completed/failed 的动作都在这里被降级为提示，并明确告诉用户
 * 下一步该点哪个按钮，避免"界面自己跳来跳去"却没人知道发生了什么。
 */
function enforceMonotonicStatus(input: SyncInput, action: SyncAction): SyncAction {
  if (!input.platformStatus || !TERMINAL_STATUSES.has(input.platformStatus)) return action
  if (action.action !== 'patch_completed' && action.action !== 'patch_failed') return action
  const pending = action.action === 'patch_completed' ? '完成' : '失败'
  return {
    action: 'warn_completed_conflict',
    canCorrect: false,
    reason: `平台状态已是终态（${input.platformStatus}），不会因节点报告自动改写为${pending}；如需重新采集请点「重新收集」`,
    ownership: action.ownership,
  }
}

/**
 * 对账判定的唯一出口。
 *
 * 内部实现只回答"节点报告看起来该做什么"，本函数负责套上状态机不变量（L3），
 * 保证任何一条路径都不会把终态自动改回活跃态。
 */
export function decideNodeSync(input: SyncInput): SyncAction {
  return enforceMonotonicStatus(input, decideNodeSyncUnchecked(input))
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
  // 文案语言随运行环境变化（同一 acli 版本在不同机器上分别输出中文与英文），
  // 因此这里只能按词义判定。判定顺序必须与 bridge 的 classifyLogCollectNodeState 一致。
  const lower = status.toLowerCase()
  if (
    status.includes('异常结束') ||
    status.includes('失败') ||
    status.includes('错误') ||
    lower.includes('fail') ||
    lower.includes('error') ||
    lower.includes('abnormal')
  ) {
    return 'abnormal'
  }
  if (
    status.includes('完成') ||
    lower.includes('complete') ||
    lower.includes('success') ||
    lower.includes('finish') ||
    lower.includes('done')
  ) {
    return 'completed'
  }
  // 必须先判无任务：英文 `Collect task does not exist!` 里含 collect，
  // 放在 running 之后会被抢先命中，平台就会一直等下去。
  if (
    status.includes('不存在') ||
    status.includes('无任务') ||
    status.includes('暂无') ||
    lower.includes('not exist') ||
    lower.includes('no task')
  ) {
    return 'none'
  }
  if (
    status.includes('进行中') ||
    status.includes('收集中') ||
    status.includes('上传中') ||
    status.includes('打包') ||
    lower.includes('progress') ||
    lower.includes('running') ||
    lower.includes('collect')
  ) {
    return 'running'
  }
  if ((snapshot.percent ?? 0) > 0 || (snapshot.done ?? 0) > 0 || (snapshot.hosts?.length ?? 0) > 0) return 'running'
  return 'unknown'
}

/**
 * 节点上报的上传阶段语义（与文案语言无关）。
 *
 * `unknown` 表示这一串文本不是可识别的上传状态，调用方必须按"拿不到证据"处理，
 * 不能自行猜测成成功或失败。
 */
export type UploadStatePhase = 'delivered' | 'failed' | 'uploading' | 'unknown'

/**
 * 归一化 acli progress 里的 `upload_state` 文本。
 *
 * 第一性原理：这段文本是给人读的，中文「发送成功 / 发送中 / 发送失败」与英文
 * `Success / Sending / Sending failed` 只是同一状态的不同表示。判定必须按词义，
 * 不能按某一种语言的字面量——2026-11-06 英文环境实测 `upload_state="Success"`，
 * 旧代码用 `includes('成功')` 判断，P1 明明归档成功却会被判成失败并阻断收尾。
 *
 * 顺序硬约束：`failed` 必须最先判。英文 `Sending failed` 同时含 sending 与 failed，
 * 先判 uploading 会把失败吞掉。
 */
export function classifyUploadState(raw?: string | null): UploadStatePhase {
  const text = (raw || '').trim()
  if (!text) return 'unknown'
  const lower = text.toLowerCase()

  if (
    text.includes('失败') ||
    text.includes('错误') ||
    text.includes('异常') ||
    lower.includes('fail') ||
    lower.includes('error') ||
    lower.includes('unauthorized') ||
    lower.includes('timeout') ||
    lower.includes('abnormal')
  ) {
    return 'failed'
  }
  if (
    text.includes('成功') ||
    lower.includes('success') ||
    lower.includes('succeed') ||
    lower.includes('complete') ||
    lower.includes('finished') ||
    lower.includes('done')
  ) {
    return 'delivered'
  }
  if (
    text.includes('发送中') ||
    text.includes('上传中') ||
    text.includes('进行中') ||
    lower.includes('sending') ||
    lower.includes('uploading') ||
    lower.includes('progress') ||
    lower.includes('pending')
  ) {
    return 'uploading'
  }
  return 'unknown'
}

/**
 * P1 收尾是否放行。
 *
 * 刻意保留旧语义：`upload_state` 缺失（节点没给上传块）时放行，由平台侧其它证据
 * 兜底；只有**明确给了状态且不是成功**才阻断。单独抽出来是为了让这条非显然
 * 规则可测，而不是埋在组件的条件表达式里。
 */
export function isP1UploadDelivered(raw?: string | null): boolean {
  const state = (raw || '').trim()
  if (!state) return true
  return classifyUploadState(state) === 'delivered'
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

/** bridge `log_collect` 的 stat 回包（只取交付链路关心的字段）。 */
export interface ArchiveProbeReply {
  status?: string
  error_code?: string
  message?: string
  log_data?: {
    exists?: boolean | null
    size?: number
    probe_level?: string
    probe_diag?: string
  } | null
}

export interface ArchiveProbeOutcome {
  /** 节点量到的字节数；0 表示没量到（不代表可交付）。 */
  size: number
  /** 三态存在性：true/false 为确认结论，null 为无法确认。 */
  exists: boolean | null
  /** 非空表示不可继续交付，直接把它抛给用户/写库。 */
  failure: { message: string; errorCode: string } | null
}

/**
 * 把 bridge 的 stat 回包翻译成交付链路唯一需要的三态结论。
 *
 * 第一性原理：界面只能说出三种话——「在」「确定不在」「无法确认」。旧代码把
 * 「status !== 'ok'」与「exists === false」当两个分支各判一次，而 bridge 又把
 * 探测失败一律报成 status=ok + exists=false，两者一叠加就无法表达「未知」。
 * Q2026102900165（2026-10-29）现场：aSV 受限 shell 没有 stat（exit 127），
 * 29.9 MB 的包就在盘上，面板却断言「压缩包已不在节点上（P1 发送成功后插件会自删）」，
 * 把用户直接推向一次必然白跑的重新采集。
 *
 * 判据必须收拢到一个纯函数里：只有它被穷举测试，下一处调用点才不会又写错。
 *
 * 错误码与文案的对应关系：
 * - `LC-TRANSFER-006`（探测失败）：说「无法确认」并带上节点给的诊断；
 * - `LC-TRANSFER-001`（确认不存在）：说「已不在」，并指向重新采集；
 * - 其余错误：原样回显桥的 message。
 */
export function interpretArchiveProbe(reply: ArchiveProbeReply | null | undefined): ArchiveProbeOutcome {
  const rawExists = reply?.log_data?.exists
  // 三态归一：只有严格的 true/false 才是结论，其余（undefined/null）一律是未知。
  const exists = rawExists === true ? true : rawExists === false ? false : null
  const size = Number(reply?.log_data?.size || 0)
  const diag = String(reply?.log_data?.probe_diag || '')
  const errorCode = String(reply?.error_code || '')

  // 探测失败优先：此时桥根本没给出结论，不得拿缺省的 exists 去猜。
  if (reply?.status === 'error') {
    if (errorCode === 'LC-TRANSFER-006') {
      return {
        size: 0,
        exists: null,
        failure: {
          message: `无法确认节点上的压缩包状态（${diag || reply.message || '探测命令不可用'}），请稍后重试`,
          errorCode,
        },
      }
    }
    return { size: 0, exists: null, failure: { message: reply.message || '读取压缩包状态失败', errorCode } }
  }

  if (exists === false) {
    return {
      size: 0,
      exists: false,
      failure: {
        message: '压缩包已不在节点上（P1 发送成功后插件会自动删除），无法补交付；如需产物请重新采集',
        errorCode: errorCode || 'LC-TRANSFER-001',
      },
    }
  }

  return { size, exists, failure: null }
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
    case 'wait_delivery':
      return `${action.reason}（采集结论已完成，自动刷新会继续确认，最多等待 10 分钟；包仍在节点时可直接点「上传到平台」）`
    case 'delivery_unknown':
      return `${action.reason}。不会判成失败，也不会要求重新采集`
    case 'warn_completed_conflict':
      return action.canCorrect
        ? `${action.reason}。可点「以节点为准纠正」把平台状态改回采集中并继续跟进（不会删除节点或日志服务器上的压缩包）。`
        : action.reason
    default:
      return action.reason
  }
}
