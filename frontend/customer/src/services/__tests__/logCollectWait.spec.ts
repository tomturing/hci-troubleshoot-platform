import { describe, expect, it } from 'vitest'
import {
  buildObserveHandoffAudit,
  COLLECT_FOREGROUND_BUDGET_MS,
  hasObserveHandoff,
  OBSERVE_POLL_INTERVAL_MS,
  planCollectWaitExpiry,
} from '../logCollectWait'

/**
 * 回归背景（2026-11-24，同日两起）：
 * - Q2026112400468：9.86 GiB P1 直传，节点 8/8 采完、发送中、exit_code=0，
 *   前端主流程第 720 次轮询耗尽后抛「采集未完成」，后端按关键词归因成
 *   LC-PLUGIN-010（PLUGIN 层：插件返回非零），页面出现「平台已判失败，
 *   但节点任务仍在运行」；节点两小时后自己传完，任务最终 completed。
 * - Q2026091700436：9.84 GiB，同一路径在 17:24:43 复现（721 次调用）。
 *
 * 结论：预算耗尽只能宣布「本端不再等待」，不能替节点下结论。
 */
describe('planCollectWaitExpiry 预算耗尽后的结论', () => {
  it('节点自报完成才 finalize，交给既有收尾', () => {
    const plan = planCollectWaitExpiry({
      nodeState: 'completed',
      uploadState: '发送成功',
      done: 9,
      total: 9,
      archiveSize: 10563438951,
    })
    expect(plan.mode).toBe('finalize')
  })

  it('节点仍在发送（9/9 完成、包已合成）转观察，不判失败', () => {
    const plan = planCollectWaitExpiry({
      nodeState: 'running',
      uploadState: '发送中',
      done: 9,
      total: 9,
      archiveSize: 10563438951,
    })
    expect(plan.mode).toBe('observe')
    // 文案只陈述可核对事实：台数、包大小、平台接下来做什么。
    expect(plan.notice).toContain('9/9')
    expect(plan.notice).toContain('9.8 GB')
    expect(plan.notice).toContain('正在发送到日志服务器')
    expect(plan.notice).toContain('后台跟踪')
    // 必须明确告诉用户"不要重新采集"——现场最贵的误操作就发生在这里。
    expect(plan.notice).toContain('不需要重新采集')
  })

  it('采集还没跑完（5/9）同样只观察，不替节点宣布失败', () => {
    const plan = planCollectWaitExpiry({
      nodeState: 'running',
      uploadState: '',
      done: 5,
      total: 9,
      archiveSize: 0,
    })
    expect(plan.mode).toBe('observe')
    expect(plan.notice).toContain('5/9')
    expect(plan.notice).toContain('节点仍在采集')
  })

  it('一次快照都没采到也不判失败（无证据 ≠ 失败证据）', () => {
    expect(planCollectWaitExpiry({ nodeState: 'unknown' }).mode).toBe('observe')
    expect(planCollectWaitExpiry({ nodeState: null }).mode).toBe('observe')
    expect(planCollectWaitExpiry({}).mode).toBe('observe')
  })

  it('异常态同样走观察（异常结论由节点证据给出，不由本地预算给出）', () => {
    expect(planCollectWaitExpiry({ nodeState: 'abnormal' }).mode).toBe('observe')
  })

  it('文案里不得出现后端会再归因成插件错误的措辞', () => {
    // 后端词表：("采集未完成" -> LC-PLUGIN-010)。
    // 若这里的提示被写进 error_message，就会把平台侧等待超时再次翻译成"插件返回非零"。
    const plan = planCollectWaitExpiry({
      nodeState: 'running',
      uploadState: '发送中',
      done: 9,
      total: 9,
      archiveSize: 1024,
    })
    expect(plan.notice).not.toContain('采集未完成')
    expect(plan.notice).not.toContain('执行失败')
    expect(plan.notice).not.toContain('%')
  })

  it('后台观察间隔必须远小于前台预算（降频但不失联）', () => {
    expect(COLLECT_FOREGROUND_BUDGET_MS).toBe(60 * 60 * 1000)
    expect(OBSERVE_POLL_INTERVAL_MS).toBeLessThan(COLLECT_FOREGROUND_BUDGET_MS / 60)
  })
})

describe('观察交接审计与页面重开恢复', () => {
  it('只记录可核对事实，不把提示语写成失败结论', () => {
    const audit = buildObserveHandoffAudit({
      nodeState: 'running',
      uploadState: '发送中',
      done: 9,
      total: 9,
      archiveSize: 10592819584,
      notice: '正在发送到日志服务器（已采集 9/9 台，日志包 9.9 GB）',
      traceId: 'lc-Q2026112400468-1795491016665',
      at: '2026-11-24T04:42:15Z',
    })
    expect(audit).toMatchObject({
      at: '2026-11-24T04:42:15Z',
      node_state: 'running',
      upload_state: '发送中',
      done: 9,
      total: 9,
      archive_size: 10592819584,
      trace_id: 'lc-Q2026112400468-1795491016665',
    })
    expect(audit.notice).not.toContain('采集未完成')
    expect(audit.notice).not.toContain('失败')
  })

  it('缺快照时写 unknown，不伪造 running 或失败', () => {
    const audit = buildObserveHandoffAudit({ notice: '节点仍在采集' })
    expect(audit.node_state).toBe('unknown')
    expect(audit.upload_state).toBeNull()
    expect(audit.done).toBeNull()
    expect(audit.total).toBeNull()
    expect(audit.archive_size).toBeNull()
  })

  it('页面重开只认 detail.observation_handoff 对象', () => {
    expect(hasObserveHandoff({ observation_handoff: { at: 'x' } })).toBe(true)
    expect(hasObserveHandoff({ observation_handoff: null })).toBe(false)
    expect(hasObserveHandoff({})).toBe(false)
    expect(hasObserveHandoff(null)).toBe(false)
  })
})
