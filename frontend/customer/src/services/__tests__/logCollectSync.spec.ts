import { describe, expect, it } from 'vitest'
import { buildSyncNotice, decideNodeSync, hashReport, resolveOwnership } from '../logCollectSync'

const STALE_REPORT = '状态：收集任务已完成！\n总进度：100% - 2/2\n归档状态：发送成功'
const FRESH_REPORT = '状态：收集任务进行中！\n总进度：0% - 0/2'

describe('hashReport', () => {
  it('同一份报告指纹稳定', () => {
    expect(hashReport(STALE_REPORT)).toBe(hashReport(STALE_REPORT))
  })

  it('报告内容变化即指纹变化（归属判定依赖这一点）', () => {
    expect(hashReport(STALE_REPORT)).not.toBe(hashReport(FRESH_REPORT))
  })

  it('空报告返回空指纹，调用方据此退化为"无基线"', () => {
    expect(hashReport('')).toBe('')
  })
})

describe('resolveOwnership 报告归属', () => {
  it('报告已刷新 → 属于本次任务', () => {
    expect(resolveOwnership({ nodeState: 'completed', reportRefreshed: true }).ownership).toBe('current')
  })

  it('报告与下发前逐字相同 → 上一次任务的旧报告', () => {
    const result = resolveOwnership({ nodeState: 'completed', reportRefreshed: false })
    expect(result.ownership).toBe('stale')
    expect(result.reason).toContain('上一次任务')
  })

  it('无指纹时，采集总数与本次目标不符 → 旧报告', () => {
    expect(resolveOwnership({ nodeState: 'completed', progressTotal: 3, expectedHosts: 1 }).ownership).toBe('stale')
  })

  it('无指纹时，报告主机与目标无交集 → 旧报告', () => {
    expect(
      resolveOwnership({
        nodeState: 'completed',
        reportHosts: ['172.28.24.131', '172.28.24.132'],
        targetHosts: ['172.28.24.130'],
      }).ownership,
    ).toBe('stale')
  })

  it('无指纹且无矛盾证据 → unknown，由调用方按节点态为准', () => {
    expect(resolveOwnership({ nodeState: 'completed' }).ownership).toBe('unknown')
  })
})

describe('decideNodeSync 节点态直映射', () => {
  it('采集中 + 节点完成（报告已刷新）→ 修正为完成', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: true,
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q2026101401683.zip',
    })
    expect(action.action).toBe('patch_completed')
  })

  /**
   * 现场回归（工单 Q2026101401683 / 2026-10-26 21:57）：
   * 节点 100% 完成、P1 上传成功、压缩包已被插件自删（没有任何 mtime 证据），
   * 旧判据因此永久判"无法确认完成归属"，平台卡在 collecting。
   * 新模型：证明不了"报告是旧的"，就必须按节点态落完成。
   */
  it('采集中 + 节点完成（无基线可比、包已自删）→ 仍然落完成', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q2026101401683.zip',
      progressTotal: 2,
      expectedHosts: 2,
      reportHosts: ['172.28.24.131', '172.28.24.132'],
      targetHosts: ['172.28.24.131', '172.28.24.132'],
    })
    expect(action.action).toBe('patch_completed')
    expect(action.ownership).toBe('unknown')
  })

  it('采集中 + 节点仍是旧报告 → 只等待，不落任何终态', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: false,
    })
    expect(action.action).toBe('wait_refresh')
    expect(action.reason).toContain('等待本次任务的报告')
  })

  it('采集中 + 节点异常结束 → 失败', () => {
    expect(decideNodeSync({ platformStatus: 'collecting', nodeState: 'abnormal' }).action).toBe('patch_failed')
  })

  it('采集中 + 节点已无任务但包路径已生成 → 完成', () => {
    expect(
      decideNodeSync({ platformStatus: 'collecting', nodeState: 'none', archivePath: '/sf/.../Q.zip' }).action,
    ).toBe('patch_completed')
  })

  it('采集中 + 节点已无任务且无包 → 采集中断，判失败', () => {
    expect(decideNodeSync({ platformStatus: 'collecting', nodeState: 'none' }).action).toBe('patch_failed')
  })

  it('平台失败但节点仍在跑 → 接管', () => {
    expect(decideNodeSync({ platformStatus: 'failed', nodeState: 'running', reportRefreshed: true }).action).toBe(
      'adopt',
    )
  })

  // 回归护栏（工单 Q2026102701294，2026-10-27）：failed 是终态，
  // 直接 patch_completed 必然被后端状态机 409 拒绝，前端每 30 秒重试一次，
  // 节点上 289 MiB 的包永远交付不出去。必须走合法边 → collecting 再收尾。
  it('平台失败 + 节点已完成且包仍在 → 接管继续交付，绝不硬改终态', () => {
    const action = decideNodeSync({
      platformStatus: 'failed',
      nodeState: 'completed',
      reportRefreshed: true,
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q2026102701294.zip',
    })
    expect(action.action).toBe('adopt')
    expect(action.adoptMode).toBe('deliver')
    expect(action.action).not.toBe('patch_completed')
  })

  it('平台失败 + 节点已完成但包已移除 → 只提示重新采集，不写库', () => {
    const action = decideNodeSync({ platformStatus: 'failed', nodeState: 'completed', reportRefreshed: true })
    expect(action.action).toBe('none')
    expect(action.reason).toContain('重新采集')
  })

  it('平台已归档但节点在跑（报告已刷新）→ 提示冲突，纠正需用户点一次', () => {
    const action = decideNodeSync({ platformStatus: 'completed', nodeState: 'running', reportRefreshed: true })
    expect(action.action).toBe('warn_completed_conflict')
    expect(action.canCorrect).toBe(true)
  })

  it('平台已归档、节点在跑但报告没刷新 → 先按旧报告等待，不提供纠正', () => {
    const action = decideNodeSync({ platformStatus: 'completed', nodeState: 'running', reportRefreshed: false })
    expect(action.action).toBe('wait_refresh')
    expect(action.canCorrect).toBeFalsy()
  })

  it('节点态无法识别 → 不动平台状态', () => {
    expect(decideNodeSync({ platformStatus: 'collecting', nodeState: 'unknown' }).action).toBe('none')
  })

  it('平台上无任务记录、节点有游离任务 → 提示占用，并说明信息不可得', () => {
    const action = decideNodeSync({ platformStatus: null, nodeState: 'running' })
    expect(action.action).toBe('warn_node_busy')
    // 非平台下发的任务，acli 报告里没有模块与时间范围，必须提前说清楚
    expect(action.reason).toContain('非本平台下发')
  })
})

describe('buildSyncNotice', () => {
  it('等待旧报告被刷新时给出可见理由与"仍在自动刷新"的说明', () => {
    const notice = buildSyncNotice(
      decideNodeSync({ platformStatus: 'collecting', nodeState: 'completed', reportRefreshed: false }),
    )
    expect(notice).toContain('上一次任务')
    expect(notice).toContain('自动刷新会继续跟进')
  })
})
