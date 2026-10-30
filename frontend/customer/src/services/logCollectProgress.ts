/**
 * 日志收集面板的「整体进度」换算。
 *
 * 第一性原理：进度条画在「探测 / 采集 / 上传 / 归档」四步之下，它必须回答的是
 * 「整条交付链路走到哪」，而不是「节点采集到什么程度」。这两件事的分母不同：
 * 节点 `percent` 在采集（含打包）结束时就到 100，而这一刻从 HCI 拉取的一个字节都还没开始。
 *
 * 旧实现把两者塞进 `Math.max(节点百分比, 60 + 交付百分比 × 0.3)`，于是只要采集到过 100，
 * 后续拉取 + 上传 + 归档全程都被钉在 100%（2026-10-30 现场 Q2026102901049：
 * 进度条 100% 与「正在从 HCI 拉取：0 字节」同时成立，用户完全无法判断还要等多久）。
 *
 * 正确做法是按阶段分段，每段的分值只用**该阶段自己的可测数据**：
 *
 * | 阶段 | 区间 | 数据来源 |
 * |------|------|---------|
 * | 探测 / 采集 / 打包 | 0 → 60 | 节点 `acli log_collect progress` 的 percent |
 * | 拉取 + 分片上传（P2） | 60 → 90 | 交付作业 progress（拉取占前 50%，上传占后 50%） |
 * | 归档（平台代传） | 90 | 没有可分段测量的维度：如实停在分界，由文本给状态 |
 * | 完成 | 100 | 平台回写 completed |
 *
 * P1 直传是唯一例外：节点自己的 percent 本来就是「采集 + 打包 + 发送」的整条链路
 * （`日志发送中` 时已是 85%、`发送成功` 才到 100%），再套一层分段映射反而会把真实进度压扁，
 * 所以直传原样使用节点百分比。
 */

/** 采集（含装包/打包）阶段在整体进度里占到的位置。 */
export const COLLECT_BAND_END = 60
/** 拉取 + 分片上传结束（= 数据面 complete）时的位置。 */
export const DELIVERY_BAND_END = 90

export type OverallPercentStage =
  | 'idle'
  | 'probing'
  | 'installing'
  | 'creating'
  | 'collecting'
  | 'pulling'
  | 'uploading'
  | 'relaying'
  | 'done'
  | 'failed'

export interface OverallPercentInput {
  stage: OverallPercentStage | string
  /** 节点报告的百分比：采集 + 打包（P1 直传时还包含发送）。 */
  nodePercent?: number | null
  /** 交付进度百分比：拉取 0-50、上传 50-100（bridge `deliveryJob.progress()` 语义）。 */
  deliveryPercent?: number | null
  /** P1 直传：整条链路由节点报数，不做分段。 */
  direct?: boolean
}

/** 把任意输入夹到 0-100；非数字（缺省 / NaN / 字符串）一律按 0 处理。 */
export function clampPercent(value: unknown): number {
  const numeric = typeof value === 'number' ? value : Number(value)
  if (!Number.isFinite(numeric)) return 0
  return Math.min(100, Math.max(0, Math.round(numeric)))
}

/**
 * 计算进度条百分比。
 *
 * 单调性是硬约束：同一次任务里百分比不允许往回退，否则用户会以为「进度丢了」。
 * 分段映射满足它——每段的起点就是上一段的终点（60 / 90），且只有 `done` 才给 100。
 */
export function computeOverallPercent(input: OverallPercentInput): number {
  const nodePercent = clampPercent(input.nodePercent ?? 0)
  if (input.stage === 'done') return 100
  // 归档没有可分段测量的维度，停在交付段的终点；从交付段跨过来也不会倒退。
  if (input.stage === 'relaying') return DELIVERY_BAND_END

  const deliveryPercent = clampPercent(input.deliveryPercent ?? 0)
  const deliveryBand = () =>
    Math.round(
      COLLECT_BAND_END + (deliveryPercent * (DELIVERY_BAND_END - COLLECT_BAND_END)) / 100,
    )

  // P1 直传从不经历「拉取」：包由节点上的插件直接发给日志服务器，平台侧不搬字节。
  // 所以只要 stage 是 pulling，就一定是平台中转，与 direct 无关——
  // 这条判据顺带修正了「P1 失败自动降级 P2」之后 upload_mode 已改写、remote_url 仍在的情况。
  if (input.stage === 'pulling') return deliveryBand()
  if (input.stage === 'uploading') return input.direct ? nodePercent : deliveryBand()

  if (input.direct) return nodePercent
  // 采集段只占 0-60：为后面的拉取与上传留出位置，否则采集一结束进度条就顶到 100。
  return Math.round((nodePercent * COLLECT_BAND_END) / 100)
}

/**
 * 字节数的展示口径（进度文案用）。
 *
 * 传输过程里用户关心的是「还要多久」，一个精确到个位的原始字节数没有信息量；
 * 但也不能四舍五入成 `0 GB`——小于 1 KB 时保留字节原值，避免「正在拉取 0 KB」这种假进度。
 */
export function formatByteCount(value: number | null | undefined): string {
  const bytes = Number(value)
  if (!Number.isFinite(bytes) || bytes <= 0) return '0 字节'
  if (bytes < 1024) return `${Math.round(bytes)} 字节`
  const units = ['KB', 'MB', 'GB', 'TB']
  let size = bytes / 1024
  let unitIndex = 0
  while (size >= 1024 && unitIndex < units.length - 1) {
    size /= 1024
    unitIndex += 1
  }
  const digits = size >= 100 ? 0 : 1
  return `${size.toFixed(digits)} ${units[unitIndex]}`
}
