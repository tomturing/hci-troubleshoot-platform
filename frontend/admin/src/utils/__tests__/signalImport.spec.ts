// 关键信号 JSON 导入/导出纯逻辑单测（对应 src/utils/signalImport.ts）
import { describe, expect, it, vi } from 'vitest'
import {
  assignImportedSignalIds,
  buildImportAnnotations,
  countImportIdRegenerations,
  MAX_IMPORT_SIGNALS,
  parseImportedSignalsJson,
  serializeSignalForExport,
} from '../signalImport'
import type { SignalV2 } from '../kbdSignalTypes'

// 构造一条最小合法信号（qfk_system 只读命令形态）
function makeSignal(overrides: Record<string, any> = {}): Record<string, any> {
  return {
    id: 'sig_demo',
    role: 'must',
    acquire: { tool: 'qfk_system', args: { command: 'ps', host: '{{HOST}}' } },
    match: null,
    orchestrate: { phase: 'diagnostic', requires: [], produces: [] },
    ...overrides,
  }
}

describe('parseImportedSignalsJson - 形态识别', () => {
  it('单信号对象（含 acquire）解析成功', () => {
    const result = parseImportedSignalsJson(JSON.stringify(makeSignal()))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.shape).toBe('single')
    expect(result.signals).toHaveLength(1)
    expect(result.signals[0].acquire.tool).toBe('qfk_system')
  })

  it('信号数组解析成功', () => {
    const result = parseImportedSignalsJson(JSON.stringify([makeSignal(), makeSignal({ id: 'sig_b' })]))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.shape).toBe('array')
    expect(result.signals).toHaveLength(2)
  })

  it('完整文档形态只取 signals，丢弃 contract 等顶层字段', () => {
    const doc = {
      schema_version: 2,
      signals: [makeSignal()],
      verification_contract: { evidence_policy: { must: ['sig_demo'] } },
      generation_metadata: { model: 'x' },
      publish_validation: { status: 'passed' },
    }
    const result = parseImportedSignalsJson(JSON.stringify(doc))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.shape).toBe('doc')
    expect(result.signals).toHaveLength(1)
    expect(result.droppedDocFields).toEqual(
      expect.arrayContaining(['schema_version', 'verification_contract', 'generation_metadata', 'publish_validation']),
    )
    // contract 不允许渗入任何导入信号
    for (const signal of result.signals) {
      expect((signal as Record<string, any>).verification_contract).toBeUndefined()
    }
  })

  it('空文本拒绝', () => {
    const result = parseImportedSignalsJson('   ')
    expect(result.ok).toBe(false)
  })

  it('非法 JSON 拒绝并给出语法错误', () => {
    const result = parseImportedSignalsJson('{oops')
    expect(result.ok).toBe(false)
    if (result.ok === true) return
    expect(result.error).toContain('JSON 语法错误')
  })

  it('顶层为数字/字符串拒绝', () => {
    expect(parseImportedSignalsJson('42').ok).toBe(false)
    expect(parseImportedSignalsJson('"hello"').ok).toBe(false)
  })

  it('signals 非数组拒绝', () => {
    const result = parseImportedSignalsJson(JSON.stringify({ signals: {} }))
    expect(result.ok).toBe(false)
    if (result.ok === true) return
    expect(result.error).toContain('signals 字段必须是数组')
  })

  it('对象既无 signals 又无 acquire 时拒绝', () => {
    const result = parseImportedSignalsJson(JSON.stringify({ foo: 1 }))
    expect(result.ok).toBe(false)
  })

  it('空 signals 数组拒绝', () => {
    expect(parseImportedSignalsJson(JSON.stringify({ schema_version: 2, signals: [] })).ok).toBe(false)
  })

  it('超过条数上限拒绝', () => {
    const many = Array.from({ length: MAX_IMPORT_SIGNALS + 1 }, (_, index) => makeSignal({ id: `sig_${index}` }))
    const result = parseImportedSignalsJson(JSON.stringify(many))
    expect(result.ok).toBe(false)
    if (result.ok === true) return
    expect(result.error).toContain(String(MAX_IMPORT_SIGNALS))
  })
})

describe('parseImportedSignalsJson - 元素结构校验（指明下标）', () => {
  it('数组含非对象元素时报错并指明第几条', () => {
    const result = parseImportedSignalsJson(JSON.stringify([makeSignal(), 42]))
    expect(result.ok).toBe(false)
    if (result.ok === true) return
    expect(result.error).toContain('第 2 条')
  })

  it('缺少 acquire 时报错并指明下标', () => {
    const result = parseImportedSignalsJson(JSON.stringify([{ id: 'x', match: null }]))
    expect(result.ok).toBe(false)
    if (result.ok === true) return
    expect(result.error).toContain('第 1 条')
    expect(result.error).toContain('acquire')
  })

  it('acquire.tool 为空时报错', () => {
    const result = parseImportedSignalsJson(JSON.stringify([makeSignal({ acquire: { tool: '', args: {} } })]))
    expect(result.ok).toBe(false)
    if (result.ok === true) return
    expect(result.error).toContain('acquire.tool')
  })
})

describe('parseImportedSignalsJson - 归一化', () => {
  it('role 缺失归一为 must 并计数', () => {
    const signal = makeSignal()
    delete signal.role
    const result = parseImportedSignalsJson(JSON.stringify(signal))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.signals[0].role).toBe('must')
    expect(result.roleFixedCount).toBe(1)
  })

  it('非法 role 归一为 must，合法 role 保留', () => {
    const result = parseImportedSignalsJson(
      JSON.stringify([makeSignal({ id: 'a', role: 'whatever' }), makeSignal({ id: 'b', role: 'should' })]),
    )
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.signals[0].role).toBe('must')
    expect(result.signals[1].role).toBe('should')
    expect(result.roleFixedCount).toBe(1)
  })

  it('provenance 剥离未知键、保留来源事实键、needs_review 恒为 true', () => {
    const result = parseImportedSignalsJson(
      JSON.stringify(
        makeSignal({
          provenance: {
            expert_created: true, // schema 不允许，必须剥离
            evidence: '截图 1',
            source_section: '排查步骤 2',
            source_refs: ['27123'],
          },
        }),
      ),
    )
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    const provenance = result.signals[0].provenance || {}
    expect(provenance.expert_created).toBeUndefined()
    expect(provenance.evidence).toBe('截图 1')
    expect(provenance.source_section).toBe('排查步骤 2')
    expect(provenance.source_refs).toEqual(['27123'])
    expect(provenance.needs_review).toBe(true)
    expect(result.strippedFields).toEqual([{ index: 0, keys: ['provenance.expert_created'] }])
  })

  it('无 provenance 时补 needs_review', () => {
    const result = parseImportedSignalsJson(JSON.stringify(makeSignal()))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.signals[0].provenance).toEqual({ needs_review: true })
  })

  it('review 字段一律删除', () => {
    const result = parseImportedSignalsJson(JSON.stringify(makeSignal({ review: { require_human_confirm: false } })))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.signals[0].review).toBeUndefined()
  })

  it('未知顶层键剥离并记录（Schema additionalProperties:false）', () => {
    const result = parseImportedSignalsJson(JSON.stringify(makeSignal({ ghost_field: 1 })))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect((result.signals[0] as Record<string, any>).ghost_field).toBeUndefined()
    expect(result.strippedFields).toEqual([{ index: 0, keys: ['ghost_field'] }])
  })

  it('match 缺失归一为 null，orchestrate/args 缺失归一为空对象', () => {
    const result = parseImportedSignalsJson(
      JSON.stringify({ id: 'min', acquire: { tool: 'qkv_task' } }),
    )
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.signals[0].match).toBeNull()
    expect(result.signals[0].orchestrate).toEqual({})
    expect(result.signals[0].acquire.args).toEqual({})
  })

  it('数字 id 字符串化', () => {
    const result = parseImportedSignalsJson(JSON.stringify(makeSignal({ id: 42 })))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.signals[0].id).toBe('42')
  })
})

describe('assignImportedSignalIds / countImportIdRegenerations', () => {
  it('无冲突保留原 id', () => {
    const parsed = parseImportedSignalsJson(JSON.stringify(makeSignal({ id: 'fresh_id' })))
    if (parsed.ok === false) throw new Error('解析失败')
    const { signals, regeneratedCount } = assignImportedSignalIds(
      parsed.signals,
      new Set(['other']),
      () => 'generated_unused',
    )
    expect(signals[0].id).toBe('fresh_id')
    expect(regeneratedCount).toBe(0)
  })

  it('与现有 id 冲突时重新生成', () => {
    const parsed = parseImportedSignalsJson(JSON.stringify(makeSignal({ id: 'dup' })))
    if (parsed.ok === false) throw new Error('解析失败')
    const createId = vi.fn(() => 'generated_1')
    const { signals, regeneratedCount } = assignImportedSignalIds(parsed.signals, new Set(['dup']), createId)
    expect(signals[0].id).toBe('generated_1')
    expect(regeneratedCount).toBe(1)
    expect(createId).toHaveBeenCalledTimes(1)
  })

  it('批次内重复 id：第一条保留，第二条重生', () => {
    const parsed = parseImportedSignalsJson(
      JSON.stringify([makeSignal({ id: 'same' }), makeSignal({ id: 'same' })]),
    )
    if (parsed.ok === false) throw new Error('解析失败')
    const createId = vi.fn(() => 'generated_2')
    const { signals, regeneratedCount } = assignImportedSignalIds(parsed.signals, new Set(), createId)
    expect(signals[0].id).toBe('same')
    expect(signals[1].id).toBe('generated_2')
    expect(regeneratedCount).toBe(1)
  })

  it('缺失 id 时生成新 id', () => {
    const parsed = parseImportedSignalsJson(JSON.stringify(makeSignal({ id: undefined })))
    if (parsed.ok === false) throw new Error('解析失败')
    const createId = vi.fn(() => 'generated_3')
    const { signals, regeneratedCount } = assignImportedSignalIds(parsed.signals, new Set(), createId)
    expect(signals[0].id).toBe('generated_3')
    expect(regeneratedCount).toBe(1)
  })

  it('干跑计数与实际分配一致', () => {
    const parsed = parseImportedSignalsJson(
      JSON.stringify([
        makeSignal({ id: 'dup' }),
        makeSignal({ id: 'dup' }),
        makeSignal({ id: 'fresh' }),
        makeSignal({ id: undefined }),
      ]),
    )
    if (parsed.ok === false) throw new Error('解析失败')
    const existing = new Set(['dup'])
    const dryCount = countImportIdRegenerations(parsed.signals, existing)
    const createId = vi.fn(() => `gen_${createId.mock.calls.length}`)
    const { regeneratedCount } = assignImportedSignalIds(parsed.signals, existing, createId)
    expect(dryCount).toBe(regeneratedCount)
    expect(dryCount).toBe(3)
  })

  it('不修改入参信号对象', () => {
    const parsed = parseImportedSignalsJson(JSON.stringify(makeSignal({ id: 'dup' })))
    if (parsed.ok === false) throw new Error('解析失败')
    const before = parsed.signals[0].id
    assignImportedSignalIds(parsed.signals, new Set(['dup']), () => 'generated_4')
    expect(parsed.signals[0].id).toBe(before)
  })
})

describe('buildImportAnnotations', () => {
  it('每条信号生成 signal_id + reason_code，note 为空不带', () => {
    const signals: SignalV2[] = [
      { id: 'a', acquire: { tool: 'qfk_system', args: {} }, match: null, orchestrate: {} },
      { id: 'b', acquire: { tool: 'qkv_task', args: {} }, match: null, orchestrate: {} },
    ]
    const annotations = buildImportAnnotations(signals, 'missing_signal')
    expect(annotations).toEqual([
      { signal_id: 'a', reason_code: 'missing_signal' },
      { signal_id: 'b', reason_code: 'missing_signal' },
    ])
  })

  it('note 非空时附带并截断到 500 字符', () => {
    const signals: SignalV2[] = [{ id: 'a', acquire: { tool: 'qfk_system', args: {} }, match: null, orchestrate: {} }]
    const longNote = 'x'.repeat(600)
    const [annotation] = buildImportAnnotations(signals, 'missing_signal', longNote)
    expect(annotation.note).toHaveLength(500)
  })
})

describe('serializeSignalForExport', () => {
  it('剥离 provenance/review，输出可解析 JSON', () => {
    const signal: SignalV2 = {
      id: 'sig_export',
      role: 'should',
      acquire: { tool: 'qfk_system', args: { command: 'ps' } },
      match: null,
      orchestrate: {},
      provenance: { needs_review: true },
      review: { require_human_confirm: false },
    }
    const text = serializeSignalForExport(signal)
    const parsed = JSON.parse(text)
    expect(parsed.provenance).toBeUndefined()
    expect(parsed.review).toBeUndefined()
    expect(parsed.id).toBe('sig_export')
  })

  it('导出产物可重新导入且 id 保留（roundtrip）', () => {
    const signal: SignalV2 = {
      id: 'sig_roundtrip',
      role: 'must',
      acquire: { tool: 'qfk_log', args: { file: 'sfvt_vtpdaemon.log' } },
      match: { type: 'keyword', pattern: 'error', expected: true, extract: { type: 'text', rows: { mode: 'all' } } },
      orchestrate: { phase: 'diagnostic', requires: [], produces: [] },
    }
    const result = parseImportedSignalsJson(serializeSignalForExport(signal))
    expect(result.ok).toBe(true)
    if (result.ok === false) return
    expect(result.shape).toBe('single')
    expect(result.signals[0].id).toBe('sig_roundtrip')
    expect(countImportIdRegenerations(result.signals, new Set())).toBe(0)
  })
})
