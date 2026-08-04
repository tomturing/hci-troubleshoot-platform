/** 将多行关键字输入解析为字面量数组；逗号属于关键字内容，不能猜测性拆分。 */
export function parseKeywordInput(value: string): string[] {
  return value
    .split(/\r?\n/)
    .map(item => item.trim())
    .filter(Boolean)
}

/** 将关键字数组统一显示为每行一个、可无损回写的字面量列表。 */
export function formatKeywordInput(value: unknown): string {
  return Array.isArray(value) ? value.map(item => String(item).trim()).filter(Boolean).join('\n') : ''
}
