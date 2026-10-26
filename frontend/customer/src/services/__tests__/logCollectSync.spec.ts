import { describe, expect, it } from 'vitest'
import { buildSyncNotice, decideNodeSync, isCompletionTrustworthy } from '../logCollectSync'

const TASK_CREATED = 1_700_000_000
const FRESH_MTIME = TASK_CREATED + 600
const STALE_MTIME = TASK_CREATED - 3600

describe('isCompletionTrustworthy', () => {
  it('包 mtime 晚于任务创建、总数与节点数一致 → 可信', () => {
    expect(
      isCompletionTrustworthy({
        nodeState: 'completed',
        archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q1.zip',
        archiveMTime: FRESH_MTIME,
        taskCreatedAt: TASK_CREATED,
        progressTotal: 3,
        hostsCount: 3,
      }).ok,
    ).toBe(true)
  })

  it('同 ticket 的历史任务报告（旧包）不可信', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q1.zip',
      archiveMTime: STALE_MTIME,
      taskCreatedAt: TASK_CREATED,
    })
    expect(result.ok).toBe(false)
    expect(result.reason).toContain('历史任务报告')
  })

  it('done=1/total=1 与三节点不一致时不可信', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q1.zip',
      progressTotal: 1,
      hostsCount: 3,
    })
    expect(result.ok).toBe(false)
    expect(result.reason).toContain('不一致')
  })
})

describe('decideNodeSync', () => {
  it('采集中的假完成（历史报告）不落终态，只继续等待', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      archivePath: '/sf/.../Q1.zip',
      archiveMTime: STALE_MTIME,
      taskCreatedAt: TASK_CREATED,
    })
    expect(action.action).toBe('none')
    expect(action.reason).toContain('不可信')
  })

  it('采集中且节点异常结束 → 修正为失败（不再空转到 1 小时）', () => {
    expect(decideNodeSync({ platformStatus: 'collecting', nodeState: 'abnormal' })).toEqual({
      action: 'patch_failed',
      reason: '节点任务异常结束',
    })
  })

  it('采集中但节点已无任务 → 失败', () => {
    expect(decideNodeSync({ platformStatus: 'collecting', nodeState: 'none' }).action).toBe('patch_failed')
  })

  it('采集中、节点任务已结束且包已生成 → 完成', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'none',
      archivePath: '/sf/.../Q1.zip',
      archiveMTime: FRESH_MTIME,
      taskCreatedAt: TASK_CREATED,
    })
    expect(action.action).toBe('patch_completed')
  })

  // 现场工单 Q2026101401683：平台已 failed，节点仍在跑 79%
  it('平台终态 + 节点仍在跑 → 可接管', () => {
    expect(decideNodeSync({ platformStatus: 'failed', nodeState: 'running' })).toEqual({
      action: 'adopt',
      reason: '平台已判失败/取消，但节点任务仍在运行，可接管继续',
    })
    expect(decideNodeSync({ platformStatus: 'cancelled', nodeState: 'running' }).action).toBe('adopt')
  })

  it('平台终态 + 节点异常结束 → 清理并重试', () => {
    expect(decideNodeSync({ platformStatus: 'failed', nodeState: 'abnormal' }).action).toBe('cancel_retry')
  })

  it('平台终态 + 节点无任务 → 已对齐，不动', () => {
    expect(decideNodeSync({ platformStatus: 'failed', nodeState: 'none' }).action).toBe('none')
  })

  it('平台已归档时不因节点变化回退', () => {
    expect(decideNodeSync({ platformStatus: 'completed', nodeState: 'running' }).action).toBe('none')
  })

  it('平台上没有任务但节点有游离任务 → 提示', () => {
    expect(decideNodeSync({ nodeState: 'running' }).action).toBe('warn_node_busy')
  })

  it('节点态无法识别时不做任何写操作', () => {
    expect(decideNodeSync({ platformStatus: 'collecting', nodeState: 'unknown' }).action).toBe('none')
  })
})

describe('buildSyncNotice', () => {
  it('接管提示里给出两个可选动作', () => {
    const notice = buildSyncNotice({ action: 'adopt', reason: '节点仍在运行' })
    expect(notice).toContain('接管')
    expect(notice).toContain('取消并重试')
  })

  it('异常提示指向清理动作', () => {
    const notice = buildSyncNotice({ action: 'cancel_retry', reason: '节点任务异常结束' })
    expect(notice).toContain('取消并重试')
  })
})
