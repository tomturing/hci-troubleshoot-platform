/**
 * QKV 变量处理（原输出后处理）预览与格式化工具。
 */

export const FEATURE_LABEL_MAP: Record<string, string> = {
  vm_name: '虚拟机名称',
  host: '主机名称',
  disk_name: '磁盘名称',
  interface_name: '网口名称',
  error_code: '错误码',
  source_host: '源主机',
  destination_host: '目标主机',
  'percent.current': '百分比',
  number: '数字',
  change_pair: '源主机 → 目标主机',
}

export const AGGREGATION_LABEL_MAP: Record<string, string> = {
  first_number: '首次出现的数值',
  last_number: '最后出现的数值',
  max: '最大值',
  min: '最小值',
  sum: '求和',
  avg: '平均值',
}

export function formatAggregationLabel(agg?: string): string {
  if (!agg) return ''
  return AGGREGATION_LABEL_MAP[agg] || agg
}

export function getMatcherPatterns(match?: Record<string, any>): string[] {
  if (!match) return []
  const pattern = match.pattern
  if (Array.isArray(pattern)) return pattern.filter(Boolean).map(String)
  if (typeof pattern === 'string' && pattern.trim()) return [pattern]
  return []
}

/**
 * 格式化派生变量提取方式摘要
 */
export function formatDeriveExtractSummary(processing?: Record<string, any>): string {
  if (!processing || typeof processing !== 'object') return '—'
  const extract = processing.extract
  if (!extract || typeof extract !== 'object') return '直接取值'

  if (extract.ai_processing?.instruction) {
    const mode = extract.ai_processing.mode === 'derive' ? '智能推导' : '原文取值'
    return `${mode}「${extract.ai_processing.instruction}」`
  }
  if (extract.type === 'split') {
    const sep = extract.separator !== undefined ? extract.separator : ','
    return `分隔符「${sep}」`
  }
  if (extract.type === 'feature') {
    const label = FEATURE_LABEL_MAP[extract.feature] || extract.feature || '特征'
    return `特征「${label}」`
  }
  return extract.type ? `提取(${extract.type})` : '特征提取'
}

/**
 * 格式化断言判断条件摘要
 */
export function formatAssertSummary(processing?: Record<string, any>): string {
  if (!processing || typeof processing !== 'object') return '—'
  const match = processing.match
  if (!match || typeof match !== 'object') return '—'
  const type = match.type || 'keyword'

  if (type === 'threshold') {
    const op = match.operator || '>'
    const val = match.value !== undefined ? match.value : ''
    const agg = match.aggregation && match.aggregation !== 'first_number'
      ? ` (${formatAggregationLabel(match.aggregation)})`
      : ''
    return `${op} ${val}${agg}`.trim() || '—'
  }
  if (type === 'keyword') {
    const patterns = getMatcherPatterns(match)
    const mode = match.mode === 'and' ? '且' : '或'
    const patternStr = patterns.length ? `[${patterns.join(', ')}]` : '—'
    return `包含 ${patternStr}${patterns.length > 1 ? ` (${mode})` : ''}`
  }
  if (type === 'regex') {
    return `正则 /${match.pattern || ''}/`
  }
  if (type === 'state') {
    return `状态 == "${match.pattern || ''}"`
  }
  if (type === 'delta') {
    const op = match.operator || '>'
    const val = match.value !== undefined ? match.value : ''
    const samples = match.minimum_samples || 2
    return `差值 ${op} ${val} (样本≥${samples})`
  }
  if (type === 'trend') {
    const dir = match.direction === 'decreasing' ? '下降' : '上升'
    const val = match.value !== undefined ? match.value : ''
    const samples = match.minimum_samples || 3
    return `趋势 ${dir} > ${val} (样本≥${samples})`
  }
  if (type === 'exists') {
    return match.expected === false ? '应不存在' : '应存在'
  }
  const op = match.operator || match.direction || ''
  const val = match.value !== undefined ? match.value : ''
  return `${type} ${op} ${val}`.trim() || type
}

/**
 * 生成完整的单行中文描述（用于 title 提示或文本预览）
 */
export function formatOutputProcessingFullText(processing?: Record<string, any>): string {
  if (!processing || typeof processing !== 'object') return '—'
  const input = processing.input || '—'
  if (processing.mode === 'assert') {
    const cond = formatAssertSummary(processing)
    const notExpected = processing.match?.expected === false ? ' [期望为假]' : ''
    return `判断：${input} ${cond}${notExpected}`
  }
  const extract = formatDeriveExtractSummary(processing)
  const target = processing.name || processing.target_variable || '未命名'
  const type = processing.type ? ` (${processing.type})` : ''
  return `提取：${input} → ${extract} → ${target}${type}`
}
