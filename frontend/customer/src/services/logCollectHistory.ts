/**
 * 结果态「展示哪一条记录」的取舍。
 *
 * 第一性原理：用户刚发起的那次操作，其结果（成功或失败）才是他打开面板要看的东西。
 * 所以选择依据是**最近的终态任务**，而不是"任意一条已完成"。
 *
 * 反例（Q2026102701193，2026-10-27 现场）：用户选 P2 新建任务、任务 failed，
 * 面板却展示早上那条 P1 的"已由插件直传"结果 —— 用户看到"P1 直传日志服务器"，
 * 第一反应是"我明明选的 P2，你怎么判的"。展示错了任务，交付路径、归档状态、
 * 有没有上传入口全部跟着错。
 */

export interface DisplayTaskLike {
  status: string
  created_at?: string | null
  finished_at?: string | null
}

/** 与组件内 TERMINAL_TASK_STATUSES 保持同一语义：采集已结束、可展示结论 */
const TERMINAL = new Set(['completed', 'failed', 'cancelled'])

/**
 * 时间基准是 `created_at`（**发起时间**），不是 `finished_at`。
 *
 * 用户说的"这次"由发起顺序定义。现场就有一条跑了 8 小时 36 分的长采集，18:05 才结束；
 * 若按结束时间排，它会顶掉用户 16:40 刚发起、16:45 就失败的另一次尝试 ——
 * 用户打开面板看到"已完成"，会以为自己的 P2 成功了。
 *
 * 结束时间只在缺发起时间时兜底。
 */
function timeOf(task: DisplayTaskLike): number {
  const raw = task.created_at || task.finished_at || ''
  const ts = Date.parse(raw)
  return Number.isFinite(ts) ? ts : 0
}

/**
 * 取最近一条终态任务；没有终态任务时返回 null。
 *
 * 刻意**不依赖入参顺序**：后端换排序策略（今天按 created_at desc，明天可能按
 * updated_at）时，这里不能跟着错，否则又会挑错一条展示。
 */
export function pickDisplayTask<T extends DisplayTaskLike>(items: T[]): T | null {
  const terminal = items.filter((item) => TERMINAL.has(item.status))
  if (!terminal.length) return null
  return terminal.reduce((latest, item) => (timeOf(item) > timeOf(latest) ? item : latest))
}
