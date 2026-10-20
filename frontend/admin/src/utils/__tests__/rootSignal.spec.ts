// 根因信号前端纯逻辑单测（对应 src/utils/rootSignal.ts）
//
// 注：本文件只测前端纯逻辑。v2 已取消 MIN_ROOT_CAUSE_LEN 跨语言钉值（抽取输入面放开为整篇
// KBD，根因短不再阻塞），故不再在此钉前端阈值。
import { describe, expect, it } from 'vitest'
import {
  buildBackfillPayload,
  buildEditPayload,
  closureDisplay,
  conflictDisplay,
  describeBackfillResult,
  distinctivenessLevel,
  formatAcquire,
  joinKeywords,
  matchKeywords,
  parseRejected,
  parseSignals,
  rejectedAnchor,
  rejectedKind,
  splitKeywords,
} from '../rootSignal'
import type { RootSignal } from '../rootSignal'

function signal(overrides: Record<string, any> = {}): RootSignal {
  return {
    schema_version: 2,
    role: 'root_cause',
    match_type: 'executable',
    signal_text: '/run/lock 分区被占满导致任务失败',
    acquire: { tool: 'qfk_system', args: { command: 'df', command_args: ['-h', '/run/lock'], host: '{{HOST}}' } },
    match: { type: 'keyword', mode: 'all', expected: ['100%'] },
    distinctiveness: { category_conflicts: 0, global_conflicts: 0 },
    ...overrides,
  }
}

describe('distinctivenessLevel 冲突判级', () => {
  it('0/0 判为全库唯一（根因信号的核心价值）', () => {
    expect(distinctivenessLevel(signal())).toBe('unique')
  })

  it('仅同类冲突判为 category_only，跨类冲突优先判 ambiguous', () => {
    expect(distinctivenessLevel(signal({ distinctiveness: { category_conflicts: 2, global_conflicts: 0 } })))
      .toBe('category_only')
    expect(distinctivenessLevel(signal({ distinctiveness: { category_conflicts: 2, global_conflicts: 1 } })))
      .toBe('ambiguous')
  })

  it('未抽取（无信号或无 distinctiveness）判为 unextracted', () => {
    expect(distinctivenessLevel(null)).toBe('unextracted')
    expect(distinctivenessLevel({ match_type: 'text' })).toBe('unextracted')
  })

  it('判级颜色与释义随级别切换，ambiguous 必须最醒目', () => {
    expect(conflictDisplay('unique').type).toBe('success')
    expect(conflictDisplay('category_only').type).toBe('warning')
    expect(conflictDisplay('ambiguous').type).toBe('danger')
    expect(conflictDisplay('unextracted').label).toBe('尚未抽取')
  })
})

describe('锚点渲染辅助', () => {
  it('executable 摘要包含工具、命令与参数', () => {
    const text = formatAcquire(signal())
    expect(text).toContain('qfk_system')
    expect(text).toContain('df -h /run/lock')
    expect(text).toContain('{{HOST}}')
  })

  it('text 型无 acquire 时摘要为空串（不得渲染成 undefined）', () => {
    expect(formatAcquire(signal({ match_type: 'text', acquire: null }))).toBe('')
  })

  it('关键词只保留非空字符串项', () => {
    expect(matchKeywords(signal({ match: { expected: ['/etc/shadow', '', ' 旧密码哈希 '] } })))
      .toEqual(['/etc/shadow', '旧密码哈希'])
    expect(matchKeywords(signal({ match: { expected: 'oops-not-array' } }))).toEqual([])
  })
})

describe('parseSignals v2 数组解析', () => {
  it('v2 顶层数组直接逐条返回', () => {
    const list = parseSignals([signal(), signal({ match_type: 'text', acquire: null })])
    expect(list).toHaveLength(2)
    expect(list[0].match_type).toBe('executable')
    expect(list[1].match_type).toBe('text')
  })

  it('旧 v1 单对象兜底归一为单元素数组', () => {
    expect(parseSignals(signal())).toHaveLength(1)
  })

  it('空/非法输入归一为空数组（无内容则无信号）', () => {
    expect(parseSignals(null)).toEqual([])
    expect(parseSignals(undefined)).toEqual([])
    expect(parseSignals([])).toEqual([])
    expect(parseSignals('oops')).toEqual([])
    expect(parseSignals([null, 1, signal()])).toHaveLength(1)
  })
})

describe('closureDisplay 闭包态四态渲染', () => {
  it('closed 绿标（现场可执行），dangling_requires/capability_missing 红标，hardcoded_literals 橙标', () => {
    expect(closureDisplay('closed')).toEqual({ type: 'success', label: '已闭环（现场可执行）' })
    expect(closureDisplay('dangling_requires')?.type).toBe('danger')
    expect(closureDisplay('capability_missing')?.type).toBe('danger')
    expect(closureDisplay('hardcoded_literals')?.type).toBe('warning')
  })

  it('text 形态/旧数据无闭包义务（缺省或空）→ 返回 null，视图不渲染该标签', () => {
    expect(closureDisplay(undefined)).toBeNull()
    expect(closureDisplay(null)).toBeNull()
    expect(closureDisplay('')).toBeNull()
    // 未知态也不臆造语义，兑底不渲染
    expect(closureDisplay('unknown_future_state')).toBeNull()
  })

  it('executable 闭包字段经 parseSignals 原样透传，视图可据以渲染 closureDisplay', () => {
    const [sig] = parseSignals([
      signal({ closure_status: 'closed', requires: ['disk_dev'], resolved_by: [{ var: 'disk_dev', by: 'upstream', ref: 'asan_disks' }] }),
    ])
    expect(sig.closure_status).toBe('closed')
    expect(sig.requires).toEqual(['disk_dev'])
    expect(sig.resolved_by?.[0]?.ref).toBe('asan_disks')
    expect(closureDisplay(sig.closure_status)?.label).toBe('已闭环（现场可执行）')
  })
})

describe('parseRejected 拒绝候选解析', () => {
  it('v2 顶层数组逐条返回（与 accepted 分列存储）', () => {
    const list = parseRejected([
      { reason_code: 'ROOT_SIGNAL_UNCLOSED', closure_status: 'dangling_requires', unresolved: ['node_ip'] },
      { reason_code: 'ROOT_SIGNAL_HARDCODED', closure_status: 'hardcoded_literals' },
    ])
    expect(list).toHaveLength(2)
    expect(list[0].unresolved).toEqual(['node_ip'])
    expect(list[1].closure_status).toBe('hardcoded_literals')
  })

  it('null/非数组/数组内非对象项一律兑底空数组（后端 CHECK 限 jsonb_typeof=array）', () => {
    expect(parseRejected(null)).toEqual([])
    expect(parseRejected(undefined)).toEqual([])
    expect(parseRejected('oops')).toEqual([])
    expect(parseRejected({ not: 'array' })).toEqual([])
    expect(parseRejected([null, 1, 'x', { reason_code: 'OK' }])).toHaveLength(1)
  })
})

describe('全库回刷请求体', () => {
  it('合法表单产出 statuses/only_missing/limit，前缀为空不下发', () => {
    expect(buildBackfillPayload({ statuses: ['draft', 'published'], onlyMissing: true, categoryPrefix: '  ', limit: 100 }))
      .toEqual({ statuses: ['draft', 'published'], only_missing: true, limit: 100 })
  })

  it('分类前缀非空时下发 category_prefix，limit 夹紧到 1..100', () => {
    const payload = buildBackfillPayload({ statuses: ['draft'], onlyMissing: false, categoryPrefix: '虚拟机', limit: 9999 })
    expect(payload).toEqual({ statuses: ['draft'], only_missing: false, limit: 100, category_prefix: '虚拟机' })
    expect(buildBackfillPayload({ statuses: ['draft'], onlyMissing: false, categoryPrefix: '', limit: 0 }))
      .toMatchObject({ limit: 1 })
  })

  it('空状态或越界状态返回中文错误串而非请求体（挡在提交前）', () => {
    expect(buildBackfillPayload({ statuses: [], onlyMissing: true, categoryPrefix: '', limit: 100 })).toContain('至少选择一个')
    expect(buildBackfillPayload({ statuses: ['archived'], onlyMissing: true, categoryPrefix: '', limit: 100 })).toContain('草稿')
  })

  it('回刷结果如实告知余量：remaining>0 不得表述为已完成', () => {
    expect(describeBackfillResult({ submitted: 100, remaining: 37 })).toContain('待刷余量 37')
    expect(describeBackfillResult({ submitted: 100, remaining: 0 })).toContain('全库回刷完成')
    expect(describeBackfillResult({ submitted: 0, remaining: 0 })).toContain('没有符合条件')
  })
})

describe('关键词 textarea ⇄ expected 数组', () => {
  it('splitKeywords 逐行、去空、去重保序', () => {
    expect(splitKeywords('/etc/shadow\n\n旧密码哈希\n /etc/shadow ')).toEqual(['/etc/shadow', '旧密码哈希'])
    expect(splitKeywords(null)).toEqual([])
  })

  it('joinKeywords 与 splitKeywords 对合（数组→逐行文本）', () => {
    expect(joinKeywords(['/etc/shadow', '旧密码哈希'])).toBe('/etc/shadow\n旧密码哈希')
    expect(splitKeywords(joinKeywords(['/a', '/b']))).toEqual(['/a', '/b'])
    expect(joinKeywords('not-array')).toBe('')
  })
})

describe('buildEditPayload 逐条编辑态 → PATCH 全量替换体', () => {
  it('把 _expectedText/_argsJsonText 还原为后端契约并补 schema_version/role', () => {
    const draft = [
      {
        match_type: 'executable',
        signal_text: '/run/lock 占满',
        acquire: { tool: 'qfk_system' },
        match: { type: 'keyword', mode: 'all' },
        _expectedText: '100%\nOOM',
        _argsJsonText: '{"command":"df","command_args":["-h","/run/lock"]}',
      },
    ]
    const payload = buildEditPayload(draft) as Record<string, any>
    expect(typeof payload).toBe('object')
    const sig = payload.signals[0]
    expect(sig.match.expected).toEqual(['100%', 'OOM'])
    expect(sig.acquire.args).toEqual({ command: 'df', command_args: ['-h', '/run/lock'] })
    expect(sig.schema_version).toBe(2)
    expect(sig.role).toBe('root_cause')
    // UI 辅助键绝不泄漏到请求体
    expect(sig._expectedText).toBeUndefined()
    expect(sig._argsJsonText).toBeUndefined()
  })

  it('text 型强制剥离 acquire（后端硬门禁会拒非 executable 的采集）', () => {
    const payload = buildEditPayload([
      { match_type: 'text', signal_text: 'shadow 残留', acquire: { tool: 'x' }, _expectedText: '/etc/shadow' },
    ]) as Record<string, any>
    expect(payload.signals[0].acquire).toBeNull()
  })

  it('executable 采集参数非法 JSON 时返回中文错误串而非请求体（挡在前端就地提示）', () => {
    const bad = buildEditPayload([
      { match_type: 'executable', signal_text: 's', acquire: { tool: 'qfk_system' }, _argsJsonText: '{oops' },
    ])
    expect(typeof bad).toBe('string')
    expect(bad).toContain('不是合法 JSON')
    const arr = buildEditPayload([
      { match_type: 'executable', signal_text: 's', acquire: { tool: 'qfk_system' }, _argsJsonText: '[1,2]' },
    ])
    expect(arr).toContain('必须是 JSON 对象')
  })

  it('executable 采集参数留空归一为空对象 args（而非报错）', () => {
    const payload = buildEditPayload([
      { match_type: 'executable', signal_text: 's', acquire: { tool: 'qfk_system' }, _argsJsonText: '  ' },
    ]) as Record<string, any>
    expect(payload.signals[0].acquire.args).toEqual({})
  })
})

describe('审计条目的展示归一分（硬拒 vs L3 剪枝）', () => {
  it('闭环拒绝候选取 candidate.signal_text 作为锚点', () => {
    expect(rejectedAnchor({ candidate: signal(), reason_code: 'ROOT_CLOSURE_DANGLING' })).toBe(
      '/run/lock 分区被占满导致任务失败',
    )
  })

  it('L3 剪枝项无 candidate 包体时回退到自身 signal_text（否则 UI 全是「无锚点描述」）', () => {
    expect(
      rejectedAnchor({ signal_text: '弱判据信号', reason_code: 'redundant_with_stronger_signal', redundant_with: '强判据' }),
    ).toBe('弱判据信号')
  })

  it('两者都缺失时返回空串，交由视图层统一回退文案', () => {
    expect(rejectedAnchor({})).toBe('')
    expect(rejectedAnchor({ candidate: {} as any })).toBe('')
  })

  it('redundant_with_stronger_signal 判为 pruned，其余一律 rejected', () => {
    expect(rejectedKind({ reason_code: 'redundant_with_stronger_signal' })).toBe('pruned')
    expect(rejectedKind({ reason_code: 'ROOT_CLOSURE_DANGLING' })).toBe('rejected')
    expect(rejectedKind({})).toBe('rejected')
  })
})
