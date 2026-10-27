/**
 * 日志收集「取消」的两段式编排。
 *
 * 第一性原理：一次采集有两个独立状态——平台任务记录、HCI 上的插件任务。
 * 取消必须两边都落地，否则会出现两种分叉：
 *  - 只写平台：任务列表显示已取消，节点仍在采集；插件是单任务互斥，后续 create 一律
 *    被「日志收集分析任务正在执行」拒绝（Q2026102601505 连报 3 次失败即此原因）；
 *  - 只停节点：平台仍停在 collecting，前端轮询会一直看到不存在的任务，直到超时。
 *
 * 顺序也不能反：先写平台终态，一旦节点取消失败，平台侧已是终态（不可复活），
 * 用户再也没有入口把节点那侧收干净。因此固定为「先停节点 → 再写平台 → 按节点结果提示」。
 *
 * 这里抽成纯函数是为了可测：组件只负责注入两个副作用，分支判定不依赖 Vue 状态。
 */

export interface CancelNodeResult {
  /** 节点侧采集任务是否确认终止 */
  stopped: boolean
  /** 未终止的原因（用于提示用户手动执行 acli log_collect cancel） */
  error: string
}

export interface CancelDeps {
  /** 让 HCI 上的插件任务停止；抛错表示没送到（会话缺失/通道断开） */
  stopNode: () => Promise<{ status?: string; message?: string; error_type?: string } | null | undefined>
  /** 把平台任务标为取消；失败不阻断提示，由调用方决定是否忽略 */
  cancelPlatform: () => Promise<unknown>
}

/**
 * 执行取消编排，返回节点侧结论供 UI 决定提示文案。
 *
 * 对抗性审查：平台写入失败也必须返回节点结论——此时用户最需要知道的恰恰是
 * "节点到底停没停"，而不是平台接口的成功与否。
 */
export async function cancelLogCollectTask(deps: CancelDeps): Promise<CancelNodeResult> {
  let stopped = false
  let error = ''
  try {
    const result = await deps.stopNode()
    stopped = result?.status === 'ok'
    if (!stopped) {
      error = result?.message || result?.error_type || '节点未返回结果'
    }
  } catch (e: any) {
    error = e?.message ? String(e.message) : String(e)
  }

  try {
    await deps.cancelPlatform()
  } catch {
    // 平台写入失败不改变节点结论；由调用方按需提示
  }

  return { stopped, error }
}

/** 取消后的用户提示：必须区分"两边都停了"与"只有平台停了"。 */
export function buildCancelNotice(result: CancelNodeResult): { level: 'success' | 'warning'; text: string } {
  if (result.stopped) {
    return { level: 'success', text: '已取消：平台任务与 HCI 侧采集任务均已终止' }
  }
  return {
    level: 'warning',
    text:
      `平台侧已标记取消，但 HCI 上的采集任务未确认终止（${result.error}）。` +
      '请在 SSH 终端执行 acli log_collect cancel，否则后续采集会提示「任务正在执行」。',
  }
}
