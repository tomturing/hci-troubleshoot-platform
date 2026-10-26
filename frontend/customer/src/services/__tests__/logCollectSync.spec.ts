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
        expectedHosts: 3,
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
      expectedHosts: 3,
    })
    expect(result.ok).toBe(false)
    expect(result.reason).toContain('不一致')
  })
})

describe('isCompletionTrustworthy 目标节点数基准', () => {
  // 现场 Q2026101401683 的 19:55：任务只选 1 台，progress 却是上一次 all（3 台）的报告。
  // 用"集群在线数"当基准会 3 对 3 通过，必须用本次任务的目标节点数。
  it('total=3 但本次只选 1 台 → 不可信', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/.../Q2026101401683.zip',
      progressTotal: 3,
      expectedHosts: 1,
    })
    expect(result.ok).toBe(false)
    expect(result.reason).toContain('目标节点数')
  })

  it('缺少包时间与采集总数时不可信，但不算"证伪"', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/.../Q.zip',
    })
    expect(result.ok).toBe(false)
    expect(result.refuted).toBe(false)
    expect(result.reason).toContain('无法确认完成归属')
  })

  it('完成态没有包路径属于"无法核对"，不算证伪', () => {
    const result = isCompletionTrustworthy({ nodeState: 'completed' })
    expect(result.ok).toBe(false)
    expect(result.refuted).toBe(false)
  })
})

describe('isCompletionTrustworthy 报告指纹（2026-10-26 Q2026101401683 假完成）', () => {
  const STALE_REPORT = '状态：收集任务已完成！\n归档状态：发送成功'

  // 现场：只选 1 台，上一次报告也是 1 台 → total 与 target 恒等（1 对 1）；
  // P1 直传后插件自删压缩包 → mtime 恒为 0。旧实现因此直接放行。
  it('包时间缺失 + total 恰好相等 + 报告与下发前完全相同 → 证伪', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/.../Q2026101401683.zip',
      archiveMTime: 0,
      archiveExists: false,
      taskCreatedAt: TASK_CREATED,
      progressTotal: 1,
      expectedHosts: 1,
      baselineRaw: STALE_REPORT,
      currentRaw: STALE_REPORT,
    })
    expect(result.ok).toBe(false)
    expect(result.refuted).toBe(true)
    expect(result.reason).toContain('报告未刷新')
  })

  // P1 的包一定被删，所以"新报告"是这条链路唯一可用的正向证据
  it('包时间缺失但报告已刷新 → 可信（P1 直传场景）', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/.../Q2026101401683.zip',
      archiveMTime: 0,
      archiveExists: false,
      taskCreatedAt: TASK_CREATED,
      progressTotal: 1,
      expectedHosts: 1,
      baselineRaw: STALE_REPORT,
      currentRaw: '状态：收集任务已完成！\n总进度：100% - 1/1（已收集完成/收集总数）\n总耗时：00:06:12',
    })
    expect(result.ok).toBe(true)
  })

  it('没有基线时不得仅凭 total 与目标数相等放行', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/.../Q.zip',
      archiveMTime: 0,
      taskCreatedAt: TASK_CREATED,
      progressTotal: 1,
      expectedHosts: 1,
    })
    expect(result.ok).toBe(false)
    // 缺证据 ≠ 证伪：不能因此给出"以节点为准纠正"的按钮
    expect(result.refuted).toBe(false)
  })

  it('bridge 明确说包不存在时，即便带了 mtime 也不当作时间证据', () => {
    const result = isCompletionTrustworthy({
      nodeState: 'completed',
      archivePath: '/sf/.../Q.zip',
      archiveMTime: FRESH_MTIME,
      archiveExists: false,
      taskCreatedAt: TASK_CREATED,
    })
    expect(result.ok).toBe(false)
  })
})

describe('decideNodeSync', () => {
  it('平台已归档但节点仍在跑、且完成判定不可信 → 允许纠正', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'running',
      archivePath: '/sf/.../Q.zip',
      progressTotal: 3,
      expectedHosts: 1,
    })
    expect(action.action).toBe('warn_completed_conflict')
    expect(action.canCorrect).toBe(true)
    expect(buildSyncNotice(action)).toContain('以节点为准纠正')
  })

  it('平台已归档、节点有新的采集任务但判据全过 → 只提示不给纠正按钮', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'running',
      archivePath: '/sf/.../Q.zip',
      archiveMTime: FRESH_MTIME,
      taskCreatedAt: TASK_CREATED,
      progressTotal: 1,
      expectedHosts: 1,
    })
    expect(action.action).toBe('warn_completed_conflict')
    expect(action.canCorrect).toBe(false)
    expect(buildSyncNotice(action)).not.toContain('以节点为准纠正')
  })

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

  it('平台已归档 + 节点Running 但无证据 → 只提示，不给纠正按钮', () => {
    const action = decideNodeSync({ platformStatus: 'completed', nodeState: 'running' })
    expect(action.action).toBe('warn_completed_conflict')
    expect(action.canCorrect).toBe(false)
  })

  it('平台已归档 + 节点异常结束 → 提示核对产物，不提供纠正', () => {
    const action = decideNodeSync({ platformStatus: 'completed', nodeState: 'abnormal' })
    expect(action.action).toBe('warn_completed_conflict')
    expect(action.canCorrect).toBeFalsy()
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
