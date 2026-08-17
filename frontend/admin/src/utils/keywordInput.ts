/** 将多行关键字输入解析为字面量数组；逗号属于关键字内容，不能猜测性拆分。 */
export function parseKeywordInput(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean)
}

/** 将模型中的关键字统一为用于比较的语义列表。 */
export function normalizeKeywordList(value: unknown): string[] {
  if (Array.isArray(value)) {
    return value.map(item => String(item).trim()).filter(Boolean)
  }
  return typeof value === 'string' ? parseKeywordInput(value) : []
}

/** 将关键字数组统一显示为每行一个、可无损回写的字面量列表。 */
export function formatKeywordInput(value: unknown): string {
  return Array.isArray(value) ? value.map(item => String(item).trim()).filter(Boolean).join('\n') : ''
}

/** 外部模型同步时保留字符串原文，避免编辑中的末尾换行被格式化掉。 */
export function formatKeywordDraft(value: unknown): string {
  return typeof value === 'string' ? value : formatKeywordInput(value)
}

/** 比较关键字语义而非输入格式，允许编辑态保留末尾换行和空格。 */
export function keywordListsEqualInput(input: string, value: unknown): boolean {
  const left = parseKeywordInput(input)
  const right = normalizeKeywordList(value)
  return left.length === right.length && left.every((item, index) => item === right[index])
}
