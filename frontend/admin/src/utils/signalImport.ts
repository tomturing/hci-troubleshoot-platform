// 关键信号 JSON 导入/导出纯逻辑（零 Vue/Element/fetch 依赖，可独立单测）。
//
// 设计边界（2026-08-17）：
// - 本地只做"结构与形态"预校验和归一化（Schema additionalProperties:false 要求的
//   未知字段剥离、role 归一、provenance needs_review 重置等）；
// - 逐工具 acquire.args 校验、match 内部结构、match/produces 互斥、变量依赖图
//   等领域契约不在此复制，统一交给后端 PATCH 的 validate_draft_signals_json（422）。
import type { ChangeAnnotation, SignalV2 } from './kbdSignalTypes'

export type ImportedDocShape = 'single' | 'array' | 'doc'

export interface ImportParseSuccess {
  ok: true
  shape: ImportedDocShape
  /** 已归一化的信号列表（尚未分配/去重 ID） */
  signals: SignalV2[]
  /** doc 形态下被丢弃的顶层字段（verification_contract 等由前端 reconcile 重建） */
  droppedDocFields: string[]
  /** 归一化过程中剥离的未知字段（signal 顶层与 provenance 内），用于摘要展示 */
  strippedFields: Array<{ index: number; keys: string[] }>
  /** role 缺失/非法被归一为 must 的信号数量 */
  roleFixedCount: number
}

export interface ImportParseFailure {
  ok: false
  error: string
}

export type ImportParseResult = ImportParseSuccess | ImportParseFailure

/** 导入体积上限：保护主线程 JSON.parse 与 PATCH 请求体 */
export const MAX_IMPORT_BYTES = 2 * 1024 * 1024
/** 单次导入信号条数上限 */
export const MAX_IMPORT_SIGNALS = 200

const VALID_ROLES: ReadonlyArray<SignalV2['role']> = ['must', 'should', 'exclude', 'context']

/** signal.v2.schema.json 中 signal 允许的顶层键（additionalProperties:false） */
const KNOWN_SIGNAL_KEYS = new Set(['id', 'role', 'acquire', 'match', 'orchestrate', 'provenance', 'review'])

/** signal.v2.schema.json 中 provenance 允许的键集 */
const KNOWN_PROVENANCE_KEYS = new Set([
  'category',
  'method',
  'source_section',
  'confidence',
  'risk',
  'needs_review',
  'evidence',
  'source_refs',
])

function isPlainObject(value: unknown): value is Record<string, any> {
  return value !== null && typeof value === 'object' && !Array.isArray(value)
}

/**
 * 解析导入文本。接受三种形态：
 * 1. 单信号对象（含对象型 acquire）；
 * 2. 信号数组；
 * 3. 完整 SignalsDoc（含 signals 数组，其余顶层字段丢弃并记入摘要）。
 */
export function parseImportedSignalsJson(text: string): ImportParseResult {
  const trimmed = (text || '').trim()
  if (!trimmed) {
    return { ok: false, error: '导入内容为空；请粘贴信号 JSON 或选择 .json 文件。' }
  }
  if (new TextEncoder().encode(trimmed).length > MAX_IMPORT_BYTES) {
    return { ok: false, error: `导入内容超过 ${Math.round(MAX_IMPORT_BYTES / 1024 / 1024)}MB 上限，请拆分后导入。` }
  }

  let parsed: unknown
  try {
    parsed = JSON.parse(trimmed)
  } catch (error) {
    return { ok: false, error: `JSON 语法错误：${error instanceof Error ? error.message : String(error)}` }
  }

  let shape: ImportedDocShape
  let rawSignals: unknown[]
  let droppedDocFields: string[] = []
  if (Array.isArray(parsed)) {
    shape = 'array'
    rawSignals = parsed
  } else if (isPlainObject(parsed)) {
    if (Array.isArray(parsed.signals)) {
      shape = 'doc'
      rawSignals = parsed.signals
      droppedDocFields = Object.keys(parsed).filter((key) => key !== 'signals')
    } else if (parsed.signals !== undefined) {
      return { ok: false, error: 'signals 字段必须是数组。' }
    } else if (isPlainObject(parsed.acquire)) {
      shape = 'single'
      rawSignals = [parsed]
    } else {
      return {
        ok: false,
        error: '无法识别的导入形态：需要单条信号对象（含 acquire）、信号数组或含 signals 数组的完整文档。',
      }
    }
  } else {
    return { ok: false, error: '导入内容顶层必须是对象或数组。' }
  }

  if (rawSignals.length === 0) {
    return { ok: false, error: '导入内容不包含任何信号。' }
  }
  if (rawSignals.length > MAX_IMPORT_SIGNALS) {
    return { ok: false, error: `一次最多导入 ${MAX_IMPORT_SIGNALS} 条信号（当前 ${rawSignals.length} 条），请拆分后导入。` }
  }

  const signals: SignalV2[] = []
  const strippedFields: ImportParseSuccess['strippedFields'] = []
  let roleFixedCount = 0

  for (let index = 0; index < rawSignals.length; index += 1) {
    const item = rawSignals[index]
    if (!isPlainObject(item)) {
      return { ok: false, error: `第 ${index + 1} 条信号不是有效对象。` }
    }
    const acquire = item.acquire
    if (!isPlainObject(acquire)) {
      return { ok: false, error: `第 ${index + 1} 条信号缺少采集配置 acquire。` }
    }
    const tool = acquire.tool
    if (typeof tool !== 'string' || !tool.trim()) {
      return { ok: false, error: `第 ${index + 1} 条信号的采集类型 acquire.tool 为空。` }
    }

    const strippedKeys: string[] = []
    const signal: SignalV2 = {
      acquire: { tool: tool.trim(), args: isPlainObject(acquire.args) ? acquire.args : {} },
      match: isPlainObject(item.match) ? item.match : null,
      orchestrate: isPlainObject(item.orchestrate) ? item.orchestrate : {},
    }

    // id：Schema 要求 string，数字 id 字符串化；缺失的 id 在 assignImportedSignalIds 阶段补齐
    if (typeof item.id === 'string' && item.id.trim()) {
      signal.id = item.id.trim()
    } else if (typeof item.id === 'number' && Number.isFinite(item.id)) {
      signal.id = String(item.id)
    }

    // role：缺失/非法归一为 must，供 reconcileSignalContract 重建 evidence_policy
    if (VALID_ROLES.includes(item.role as SignalV2['role'])) {
      signal.role = item.role as SignalV2['role']
    } else {
      signal.role = 'must'
      roleFixedCount += 1
    }

    // provenance：仅保留 Schema 允许键（evidence/source_section/source_refs 是卡片
    // 「来源证据」区直接消费的来源事实，全量重置会导致溯源空白），并强制待复核
    if (isPlainObject(item.provenance)) {
      const provenance: Record<string, any> = {}
      for (const [key, value] of Object.entries(item.provenance)) {
        if (KNOWN_PROVENANCE_KEYS.has(key)) provenance[key] = value
        else strippedKeys.push(`provenance.${key}`)
      }
      provenance.needs_review = true
      signal.provenance = provenance
    } else {
      signal.provenance = { needs_review: true }
    }

    // review 是审核状态而非内容，导入一律不带；未知顶层键剥离（Schema additionalProperties:false）
    for (const key of Object.keys(item)) {
      if (key === 'review') continue
      if (!KNOWN_SIGNAL_KEYS.has(key)) strippedKeys.push(key)
    }

    if (strippedKeys.length) strippedFields.push({ index, keys: strippedKeys })
    signals.push(signal)
  }

  return { ok: true, shape, signals, droppedDocFields, strippedFields, roleFixedCount }
}

/**
 * 为导入信号分配 ID：与现有信号或批次内其他信号冲突时重新生成，缺失时生成。
 * 不修改入参，返回新对象数组。
 */
export function assignImportedSignalIds(
  signals: SignalV2[],
  existingIds: ReadonlySet<string>,
  createId: () => string,
): { signals: SignalV2[]; regeneratedCount: number } {
  const used = new Set(existingIds)
  let regeneratedCount = 0
  const assigned = signals.map((signal) => {
    const original = typeof signal.id === 'string' ? signal.id.trim() : ''
    let id = original
    if (!id || used.has(id)) {
      id = createId()
      regeneratedCount += 1
    }
    used.add(id)
    return { ...signal, id }
  })
  return { signals: assigned, regeneratedCount }
}

/**
 * ID 分配的干跑计数（预览用，不生成随机 ID）。
 * 与 assignImportedSignalIds 行为一致：冲突/缺失计一次，批次内重复 id 第一条保留、后续重生。
 */
export function countImportIdRegenerations(signals: SignalV2[], existingIds: ReadonlySet<string>): number {
  const used = new Set(existingIds)
  let count = 0
  signals.forEach((signal, index) => {
    const original = typeof signal.id === 'string' ? signal.id.trim() : ''
    if (!original || used.has(original)) {
      count += 1
      used.add(`__import_preview_${index}__`)
    } else {
      used.add(original)
    }
  })
  return count
}

/** 为每条导入信号构造 change_annotation（后端要求 reason_code ∈ REASON_CODES 且必含 signal_id/path）。 */
export function buildImportAnnotations(
  signals: SignalV2[],
  reasonCode: string,
  note?: string,
): ChangeAnnotation[] {
  const trimmedNote = (note || '').trim().slice(0, 500)
  return signals.map((signal) => {
    const annotation: ChangeAnnotation = {
      signal_id: String(signal.id ?? ''),
      reason_code: reasonCode,
    }
    if (trimmedNote) annotation.note = trimmedNote
    return annotation
  })
}

/** 导出（复制）单条信号：剥离 provenance/review 审核态字段，只保留可复用内容。 */
export function serializeSignalForExport(signal: SignalV2): string {
  const { provenance: _provenance, review: _review, ...content } = signal
  return JSON.stringify(content, null, 2)
}
