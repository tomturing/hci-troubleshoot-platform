import { describe, expect, it } from 'vitest'
import {
  blocksCreate,
  buildSyncNotice,
  decideNodeSync,
  classifyUploadState,
  CREATE_ACK_GRACE_MS,
  DELIVERY_EVIDENCE_WAIT_MS,
  fallbackNodeState,
  hashReport,
  interpretArchiveProbe,
  isP1UploadDelivered,
  parseAcliTotalTime,
  resolveOwnership,
  SESSION_ADOPT_GRACE_MS,
} from '../logCollectSync'

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

describe('parseAcliTotalTime', () => {
  it('解析带毫秒的 HH:MM:SS.s（现场原始值）', () => {
    expect(parseAcliTotalTime('00:03:38.9')).toBeCloseTo(218.9, 5)
  })

  it('解析不足一小时时省略小时段的 MM:SS', () => {
    expect(parseAcliTotalTime('58:11')).toBe(3491)
  })

  it('解析跨小时的 HH:MM:SS', () => {
    expect(parseAcliTotalTime('1:02:03')).toBe(3723)
  })

  it('无法解析 → null，调用方据此跳过否决而不是放行', () => {
    expect(parseAcliTotalTime('')).toBeNull()
    expect(parseAcliTotalTime(undefined)).toBeNull()
    expect(parseAcliTotalTime('unknown')).toBeNull()
    expect(parseAcliTotalTime('3 分 38 秒')).toBeNull()
  })
})

describe('resolveOwnership 报告归属', () => {
  it('报告已刷新 → 只说明报告变了，不再直接认定属于本次任务', () => {
    // 2026-11-06 Q2026110666667：acli 在同一任务的上传阶段会持续改写完成报告
    // （Total time 03:09.0 → 03:38.9、raw 长度 2485 → 2776），"变了"既可能是新任务、
    // 也可能是同一任务的演进，无法区分。归属的正向证据改由 L1（采集起点）承担。
    const result = resolveOwnership({ nodeState: 'completed', reportRefreshed: true })
    expect(result.ownership).toBe('unknown')
    expect(result.reason).toContain('上传阶段')
  })

  it('报告总耗时超过任务年龄 → 必是更早任务的报告（L2 时间否决）', () => {
    // 现场数据：新任务创建 3 秒，报告自称跑了 03:38.9（218.9 秒）
    const result = resolveOwnership({
      nodeState: 'completed',
      reportRefreshed: true,
      reportTotalSeconds: 218.9,
      taskAgeSeconds: 3,
    })
    expect(result.ownership).toBe('stale')
    expect(result.reason).toContain('不可能是本次任务的报告')
  })

  it('报告总耗时在任务年龄之内（含松弛）→ 不做否决', () => {
    const result = resolveOwnership({
      nodeState: 'completed',
      reportRefreshed: true,
      reportTotalSeconds: 218.9,
      taskAgeSeconds: 600,
    })
    expect(result.ownership).toBe('unknown')
  })

  it('报告没带总耗时 → 跳过时间否决，绝不当成"时间对得上"', () => {
    const result = resolveOwnership({ nodeState: 'completed', reportRefreshed: true, taskAgeSeconds: 3 })
    expect(result.ownership).toBe('unknown')
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

  // 回归护栏（工单 Q2026110901673）：create 是同步命令，但插件注册任务有秒级延迟。
  // 旧实现在 create 后 6 秒就把「节点无任务」写成 failed，且文案误称「采集中断」。
  it('采集中 + 节点无任务 + 本次亲自下发且未超接单宽限 → 有界等待，不写终态', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'none',
      requireSessionAdopt: true,
      sessionAdopted: false,
      taskAgeMs: 5_000,
    })
    expect(action.action).toBe('wait_refresh')
    expect(action.reason).toContain('尚未接单')
  })

  it('采集中 + 节点无任务 + 超接单宽限 → 判失败并带规范码', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'none',
      requireSessionAdopt: true,
      sessionAdopted: false,
      taskAgeMs: CREATE_ACK_GRACE_MS + 1,
    })
    expect(action.action).toBe('patch_failed')
    expect(action.errorCode).toBe('LC-PLUGIN-008')
    expect(action.reason).toContain('未建立采集任务')
  })

  it('采集中 + 曾见过 running 后节点无任务 → 仍是采集中断且不等宽限', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'none',
      requireSessionAdopt: true,
      sessionAdopted: true,
      taskAgeMs: 5_000,
    })
    expect(action.action).toBe('patch_failed')
    expect(action.reason).toBe('节点上已无任务，采集中断')
  })

  it('采集中 + 节点无任务 + 接管历史任务 → 不等宽限', () => {
    expect(
      decideNodeSync({
        platformStatus: 'collecting',
        nodeState: 'none',
        requireSessionAdopt: false,
        taskAgeMs: 5_000,
      }).action,
    ).toBe('patch_failed')
  })

  it('采集中 + 节点已无任务且无包 → 判失败并补齐错误码', () => {
    const action = decideNodeSync({ platformStatus: 'collecting', nodeState: 'none' })
    expect(action.action).toBe('patch_failed')
    expect(action.errorCode).toBe('LC-PLUGIN-008')
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
      archiveExists: true,
    })
    expect(action.action).toBe('adopt')
    expect(action.adoptMode).toBe('deliver')
    expect(action.action).not.toBe('patch_completed')
  })

  it('平台失败 + 节点已完成但包已移除 → 只提示重新采集，不写库', () => {
    const action = decideNodeSync({
      platformStatus: 'failed',
      nodeState: 'completed',
      reportRefreshed: true,
      archiveExists: false,
    })
    expect(action.action).toBe('none')
    expect(action.reason).toContain('压缩包已不在')
    expect(action.reason).toContain('重新采集')
  })

  // 回归护栏（工单 Q2026102701403，2026-10-28）：旧版 bridge 不返回 archive_exists，
  // 前端收到 undefined 却断言"压缩包已不在"，现场 455 MiB 的包明明在盘上，
  // 用户因此只能重采一遍。未探测到 ≠ 已删除，两种说法必须能区分。
  it('平台失败 + 节点已完成但缺少 stat 结论 → 不得谎报"包已不在"', () => {
    const action = decideNodeSync({
      platformStatus: 'failed',
      nodeState: 'completed',
      reportRefreshed: true,
    })
    expect(action.action).toBe('none')
    expect(action.reason).toContain('未确认')
    expect(action.reason).not.toContain('压缩包已不在')
  })

  // 回归护栏（#1323，2026-10-27）：「采集完成」不等于「已交付」。
  // completed 只是 acli 的报告，平台可能一个字节都没收到；此时包还在节点上（stat 证实），
  // 唯一正确的出口是经平台中转补交付，而不是只给"重新收集"。
  it('平台已完成但未交付 + stat 证实包仍在 → 上传到平台，不重新采集', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      reportRefreshed: true,
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q2026102701294.zip',
      archiveExists: true,
      platformDelivered: false,
    })
    expect(action.action).toBe('adopt')
    expect(action.adoptMode).toBe('deliver')
    expect(action.reason).toContain('不会重新采集')
  })

  it('平台已完成且已交付 + 节点包仍在（P2 不删包）→ 不再提示上传', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      reportRefreshed: true,
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q2026102701294.zip',
      archiveExists: true,
      platformDelivered: true,
    })
    expect(action.action).toBe('none')
    expect(action.action).not.toBe('adopt')
  })

  it('平台已完成但未交付 + stat 证实包已不在 → 只提示，不造出空交付', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      reportRefreshed: true,
      archiveExists: false,
      platformDelivered: false,
    })
    // 证据驱动状态机：包已不在只是"这一刻没量到"，不能反推"插件直传成功"、
    // 更不能立刻判失败。只做有界等待，把交付结论留给落盘侧核验。
    expect(action.action).toBe('wait_delivery')
    expect(action.action).not.toBe('adopt')
    expect(action.reason).toContain('正在确认')
  })

  // 现场回归（工单 Q2026102701193，2026-10-27）：
  // 节点 acli 报「日志收集单发送状态：发送成功」，插件随即自删 zip；
  // 平台因漏记 delivery_status 而被判成"未交付 + 包仍在节点"，于是
  //   a) 结果页一直提示「请点上传到平台」，点下去 stat exit 1；
  //   b) 新建/重新收集被 preCreate 守卫拦回同一句提示 —— 完全死锁。
  // 契约：节点报告发送成功就是 P1 的交付结论，平台只做补登，不得要求补交付。
  it('已完成 + 节点报 P1 发送成功 + 平台无记录 → 补登已交付，绝不给上传入口', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      reportRefreshed: true,
      // 报告里仍带着路径，但文件已被插件删除
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q2026102701193.zip',
      archiveExists: false,
      nodeDelivered: true,
      platformDelivered: false,
    })
    expect(action.action).toBe('patch_delivered')
    expect(action.action).not.toBe('adopt')
    expect(action.reason).toContain('P1')
  })

  it('已完成 + 节点报 P1 发送失败 + 包仍在 → 补交付成立', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      reportRefreshed: true,
      archiveExists: true,
      nodeDelivered: false,
      platformDelivered: false,
    })
    expect(action.action).toBe('adopt')
    expect(action.adoptMode).toBe('deliver')
  })

  it('已完成 + 未探测包存在性（旧 bridge）→ 不提供上传入口，只说明待确认', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      reportRefreshed: true,
      archivePath: '/sf/data/local/acli/tmp/data/log_collect/Q2026102701193.zip',
      platformDelivered: false,
    })
    expect(action.action).toBe('wait_delivery')
    expect(action.action).not.toBe('adopt')
    expect(action.reason).toContain('正在确认')
  })

  it('平台已完成但未交付 + 节点无任务 → 没有可交付的物证，不提供上传入口', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'none',
      platformDelivered: false,
    })
    expect(action.action).toBe('none')
  })

  it('未接管时旧报告仍必须拦下（放行范围不能过宽）', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: false,
    })
    expect(action.action).toBe('wait_refresh')
  })

  it('接管时节点仍在跑 → 不得误判为完成', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'running',
      reportRefreshed: false,
    })
    expect(action.action).not.toBe('patch_completed')
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

/** 现场 2026-11-06 Q2026110666667 的时间线，用作用例基线 */
describe('L1 采集起点门', () => {
  it('新任务 3 秒就报完成、从未见过 running → 不采信，继续等', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: true,
      requireSessionAdopt: true,
      sessionAdopted: false,
      taskAgeMs: 3_000,
    })
    expect(action.action).toBe('wait_refresh')
    expect(action.reason).toContain('采集起点')
  })

  it('见到过 running（采集起点）后再报完成 → 正常落完成', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: true,
      requireSessionAdopt: true,
      sessionAdopted: true,
      taskAgeMs: 218_000,
    })
    expect(action.action).toBe('patch_completed')
  })

  it('超过冷启动窗口仍未见起点 → 兜底放行，绝不把真完成永久卡住', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: true,
      requireSessionAdopt: true,
      sessionAdopted: false,
      taskAgeMs: SESSION_ADOPT_GRACE_MS + 1,
    })
    expect(action.action).toBe('patch_completed')
  })

  it('接管历史任务（未要求起点）→ 不设门，Q2026101401683 的路径不受影响', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: true,
      requireSessionAdopt: false,
      sessionAdopted: false,
      taskAgeMs: 5_000,
    })
    expect(action.action).toBe('patch_completed')
  })

  it('拿不到任务年龄 → 无法证明"刚下发"，按节点态放行（不制造新的卡死）', () => {
    const action = decideNodeSync({
      platformStatus: 'collecting',
      nodeState: 'completed',
      reportRefreshed: true,
      requireSessionAdopt: true,
      sessionAdopted: false,
    })
    expect(action.action).toBe('patch_completed')
  })
})

describe('L2 假 Success 拦截', () => {
  it('本次下发、从未见上传、包已不在 → 不写 delivered', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      requireSessionAdopt: true,
      nodeDelivered: true,
      platformDelivered: false,
      archiveExists: false,
      uploadSeen: false,
      deliveryWaitElapsedMs: 0,
    })
    expect(action.action).toBe('wait_delivery')
    expect(action.reason).toContain('从未观测到上传过程')
  })

  it('见过上传阶段后再报成功 → 正常补登已交付', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      requireSessionAdopt: true,
      nodeDelivered: true,
      platformDelivered: false,
      archiveExists: false,
      uploadSeen: true,
    })
    expect(action.action).toBe('patch_delivered')
  })

  it('接管历史任务（未要求起点）→ 不拦，Q2026102701193 的补登路径不变', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      nodeDelivered: true,
      platformDelivered: false,
      archiveExists: false,
      uploadSeen: false,
    })
    expect(action.action).toBe('patch_delivered')
  })

  it('等待超时仍无归档证据 → 明确告知结果未知，而不是静默卡住', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'completed',
      requireSessionAdopt: true,
      nodeDelivered: true,
      platformDelivered: false,
      archiveExists: false,
      uploadSeen: false,
      deliveryWaitElapsedMs: DELIVERY_EVIDENCE_WAIT_MS + 1,
    })
    expect(action.action).toBe('delivery_unknown')
  })
})

describe('L3 状态机单调', () => {
  it('平台已 completed 时，任何路径都不会再返回 patch_completed', () => {
    const cases = [
      { nodeState: 'completed' as const },
      { nodeState: 'running' as const },
      { nodeState: 'abnormal' as const },
      { nodeState: 'unknown' as const },
    ]
    for (const item of cases) {
      const action = decideNodeSync({ platformStatus: 'completed', ...item })
      expect(action.action).not.toBe('patch_completed')
    }
  })

  it('平台已 completed 时，也不会被自动改写成 failed', () => {
    const action = decideNodeSync({ platformStatus: 'completed', nodeState: 'abnormal' })
    expect(action.action).not.toBe('patch_failed')
  })

  it('终态纠正是用户显式动作：提示冲突但 canCorrect 可为真', () => {
    const action = decideNodeSync({
      platformStatus: 'completed',
      nodeState: 'running',
      reportRefreshed: true,
    })
    expect(action.action).toBe('warn_completed_conflict')
    expect(action.canCorrect).toBe(true)
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

// 现场回归（Q2026102701193，2026-10-27）：补交付待办把 create 也拦住的死锁。
// 判定与"是否能新建任务"是两回事：补交付只说明节点上有个已完成的包，
// 它不与新的 create 互斥；拦下来就会既不能新建、也补交付不了。
describe('blocksCreate：新建任务守卫的边界', () => {
  it('补交付待办不拦新建', () => {
    expect(blocksCreate({ action: 'adopt', reason: '', adoptMode: 'deliver' })).toBe(false)
  })

  it('节点还在跑 / 游离任务必须拦（会撞插件互斥）', () => {
    expect(blocksCreate({ action: 'adopt', reason: '', adoptMode: 'resume' })).toBe(true)
    expect(blocksCreate({ action: 'cancel_retry', reason: '' })).toBe(true)
    expect(blocksCreate({ action: 'warn_node_busy', reason: '' })).toBe(true)
  })

  it('无需用户决策的结论不拦', () => {
    expect(blocksCreate(null)).toBe(false)
    expect(blocksCreate({ action: 'none', reason: '' })).toBe(false)
    expect(blocksCreate({ action: 'patch_delivered', reason: '' })).toBe(false)
  })
})

// 现场回归（Q2026102900165，2026-10-29）：aSV 受限 shell 没有 stat（exit 127），
// 29.9 MB 的压缩包就在盘上，面板却报「压缩包已不在节点上」。
// 交付链路只能把「探测失败」说成「无法确认」，绝不得译成「已删除」。
describe('interpretArchiveProbe：探测回包的三态判据', () => {
  it('stat → ls 兜底成功时给出可交付的大小', () => {
    const outcome = interpretArchiveProbe({
      status: 'ok',
      log_data: { exists: true, size: 29900423, probe_level: 'ls', probe_diag: 'stat:exit=127 stat: not found' },
    })
    expect(outcome.failure).toBeNull()
    expect(outcome.exists).toBe(true)
    expect(outcome.size).toBe(29900423)
  })

  it('旧版 bridge 只回 size 不回 exists：不得当成不存在', () => {
    const outcome = interpretArchiveProbe({ status: 'ok', log_data: { size: 1024 } })
    expect(outcome.failure).toBeNull()
    expect(outcome.exists).toBeNull()
    expect(outcome.size).toBe(1024)
  })

  it('探测失败（LC-TRANSFER-006）→ 说无法确认并带出节点诊断', () => {
    const outcome = interpretArchiveProbe({
      status: 'error',
      error_code: 'LC-TRANSFER-006',
      message: '无法确认压缩包状态（节点命令不可用或通道异常）',
      log_data: { exists: null, size: 0, probe_diag: 'stat:exit=127 stat: not found | ls:exit=127' },
    })
    expect(outcome.failure?.errorCode).toBe('LC-TRANSFER-006')
    expect(outcome.failure?.message).toContain('无法确认')
    expect(outcome.failure?.message).toContain('stat:exit=127')
    // 关键断言：绝不能出现那句把用户推向重采的话。
    expect(outcome.failure?.message).not.toContain('压缩包已不在节点上')
  })

  it('确认不存在（LC-TRANSFER-001）→ 才允许说已不在并指向重新采集', () => {
    const outcome = interpretArchiveProbe({
      status: 'ok',
      error_code: 'LC-TRANSFER-001',
      message: '节点上已确认没有该压缩包',
      log_data: { exists: false, size: 0, probe_level: 'missing' },
    })
    expect(outcome.failure?.errorCode).toBe('LC-TRANSFER-001')
    expect(outcome.failure?.message).toContain('压缩包已不在节点上')
    expect(outcome.failure?.message).toContain('重新采集')
  })

  it('error 且无错误码时原样回显桥的 message，不丢失真因', () => {
    const outcome = interpretArchiveProbe({ status: 'error', message: 'SSH 会话已断开' })
    expect(outcome.failure?.message).toBe('SSH 会话已断开')
    expect(outcome.exists).toBeNull()
  })

  it('空回包 / 缺 log_data 一律是未知，不得当 false', () => {
    for (const reply of [null, undefined, {}, { status: 'ok' }]) {
      const outcome = interpretArchiveProbe(reply)
      expect(outcome.exists).toBeNull()
      expect(outcome.failure).toBeNull()
    }
  })
})

/**
 * 2026-11-06 Q2026110486009：同一 acli 版本在部分机器上输出英文，
 * 只认中文的兜底会把"已完成 + 上传失败"读成 unknown。
 */
describe('fallbackNodeState 语言无关', () => {
  it('中文样本保持原判定', () => {
    expect(fallbackNodeState({ status: '收集任务进行中！' })).toBe('running')
    expect(fallbackNodeState({ status: '收集任务已完成！' })).toBe('completed')
    expect(fallbackNodeState({ status: '收集任务异常结束！' })).toBe('abnormal')
    expect(fallbackNodeState({ status: '收集任务不存在！' })).toBe('none')
  })

  it('英文样本按同一套词义判定', () => {
    expect(fallbackNodeState({ status: 'Collect task is in progress!' })).toBe('running')
    expect(fallbackNodeState({ status: 'Collect task has been completed!' })).toBe('completed')
    expect(fallbackNodeState({ status: 'Sending failed' })).toBe('abnormal')
  })

  it('英文无任务不得被 collect 抢先判成进行中', () => {
    expect(fallbackNodeState({ status: 'Collect task does not exist!' })).toBe('none')
  })

  it('识别不了仍然返回 unknown', () => {
    expect(fallbackNodeState({ status: '' })).toBe('unknown')
  })
})

/**
 * 证据驱动交付状态机（2026-11-06，Q2026101201081）。
 *
 * 现场教训：acli 16:17:10 报「收集任务已完成」，20 GiB 的包 16:18 才落盘。
 * 旧实现在报告完成那一刻同时做了两件错事：
 *   - `upload_state` 为空却写 `delivered`（假成功）；
 *   - 一次瞬时 `archive_missing` 就写 `failed + LC-TRANSFER-007`（假失败）。
 * 交付结论必须由证据决定，且证据不足时只能"等待/未知"，不能二选一猜一个。
 */
describe('交付证据有界等待', () => {
  const completed = {
    platformStatus: 'completed' as const,
    nodeState: 'completed' as const,
    reportRefreshed: true,
    platformDelivered: false,
  }

  it('窗口固定为 10 分钟，且只有一个常量来源', () => {
    expect(DELIVERY_EVIDENCE_WAIT_MS).toBe(600000)
  })

  it('落盘侧核验通过 → 补登已交付，与节点自述无关', () => {
    expect(decideNodeSync({ ...completed, archiveVerified: 'delivered' }).action).toBe('patch_delivered')
  })

  it('落盘侧明确否定 + 节点自述成功 → 不写库、不报失败，进入等待', () => {
    const action = decideNodeSync({
      ...completed,
      nodeDelivered: true,
      archiveVerified: 'failed',
    })
    expect(action.action).toBe('wait_delivery')
    expect(action.reason).toContain('等待')
  })

  it('无证据 + 包已不在 → 有界等待，不得断言"插件直传成功/无需再上传"', () => {
    const action = decideNodeSync({ ...completed, archiveExists: false })
    expect(action.action).toBe('wait_delivery')
    expect(action.reason).not.toContain('无需再上传')
  })

  it('窗口边界：未到点是等待，到点收敛为结果未知（不写 failed）', () => {
    expect(
      decideNodeSync({ ...completed, deliveryWaitElapsedMs: DELIVERY_EVIDENCE_WAIT_MS - 1 }).action,
    ).toBe('wait_delivery')
    const expired = decideNodeSync({ ...completed, deliveryWaitElapsedMs: DELIVERY_EVIDENCE_WAIT_MS })
    expect(expired.action).toBe('delivery_unknown')
    expect(expired.reason).toContain('结果未知')
  })

  it('包仍在盘 → 保留补交付入口，不被等待逻辑吞掉', () => {
    const action = decideNodeSync({ ...completed, archiveExists: true })
    expect(action.action).toBe('adopt')
    expect(action.adoptMode).toBe('deliver')
  })

  it('回归护栏：采集结论绝不被交付证据拦住（#1277 / Q2026101401683）', () => {
    // "要求完成态自证"曾把真实完成永久卡在 collecting；本次改动只约束交付结论。
    expect(
      decideNodeSync({
        platformStatus: 'collecting',
        nodeState: 'completed',
        archiveVerified: 'failed',
        deliveryWaitElapsedMs: 0,
      }).action,
    ).toBe('patch_completed')
  })

  it('两种新动作都给出可操作提示，且不谎称已归档', () => {
    const waiting = buildSyncNotice({ action: 'wait_delivery', reason: '正在确认归档件' })
    const unknown = buildSyncNotice({ action: 'delivery_unknown', reason: '结果未知' })
    expect(waiting).toContain('10 分钟')
    expect(waiting).not.toContain('已归档')
    expect(unknown).toContain('不会判成失败')
  })
})

/**
 * 2026-11-06 英文环境实测 `upload_state="Success"`，但 LogCollectPanel 用
 * `includes('成功')` 判定 P1 是否交付，导致成功被判失败并阻断 finishP1 收尾。
 * 所有状态词判定必须收敛到按词义归一，不能散落在组件里硬编码中文。
 */
describe('classifyUploadState 上传状态语言无关', () => {
  const cases: Array<[string, string | undefined, ReturnType<typeof classifyUploadState>]> = [
    ['中文成功', '发送成功', 'delivered'],
    ['中文发送中', '发送中', 'uploading'],
    ['中文上传中', '上传中', 'uploading'],
    ['中文失败', '发送失败', 'failed'],
    ['中文错误', '发送错误', 'failed'],
    ['英文成功', 'Success', 'delivered'],
    ['英文成功（实机 Succeed 写法）', 'Succeed', 'delivered'],
    ['英文完成', 'Completed', 'delivered'],
    ['英文发送中', 'Sending', 'uploading'],
    ['英文上传中', 'Uploading', 'uploading'],
    ['英文进行中', 'In progress', 'uploading'],
    ['英文失败', 'Failed', 'failed'],
    ['英文发送失败（含 sending，失败必须优先）', 'Sending failed', 'failed'],
    ['英文鉴权失败', '401 Unauthorized', 'failed'],
    ['英文超时', 'Timeout', 'failed'],
    ['空串', '', 'unknown'],
    ['未传', undefined, 'unknown'],
    ['无法识别', '???', 'unknown'],
  ]
  it.each(cases)('%s', (_name, raw, want) => {
    expect(classifyUploadState(raw)).toBe(want)
  })

  it('前后空白不影响判定', () => {
    expect(classifyUploadState('  Success  ')).toBe('delivered')
  })
})

describe('isP1UploadDelivered P1 收尾放行', () => {
  it('英文成功必须放行（回归：旧代码会抛"上传失败"）', () => {
    expect(isP1UploadDelivered('Success')).toBe(true)
    expect(isP1UploadDelivered('Succeed')).toBe(true)
  })

  it('中文成功保持放行', () => {
    expect(isP1UploadDelivered('发送成功')).toBe(true)
  })

  it('明确失败一律阻断，中英文一致', () => {
    expect(isP1UploadDelivered('发送失败')).toBe(false)
    expect(isP1UploadDelivered('Failed')).toBe(false)
    expect(isP1UploadDelivered('Sending failed')).toBe(false)
  })

  it('节点没给上传块时由证据状态机兜底，不再默认已交付', () => {
    // 函数本身为兼容 #1453 仍返回 true（English P1 成功不能被误杀），
    // 但交付结论不再直接采信它：finishP1 会走 verify-archive，
    // 空 upload_state 在证据不足时进入有界等待（见「交付证据有界等待」）。
    expect(isP1UploadDelivered('')).toBe(true)
    expect(isP1UploadDelivered(undefined)).toBe(true)
  })
})
