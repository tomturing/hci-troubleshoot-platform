/**
 * 产出变量目录解析工具。
 *
 * 工具定义的 parameters_schema.properties.produces.default 是产出变量的
 * 唯一事实来源；这里集中处理旧数据兼容、空值和重复名称，避免不同页面
 * 各自实现一套稍有差异的解析规则。
 */
export interface ProduceVariableOption {
  name: string
  path: string
}

export interface ProduceVariableToolLike {
  tool_name?: unknown
  category?: unknown
  is_active?: unknown
  parameters_schema?: unknown
}

export function extractProduceVariablesFromSchema(schema: unknown): ProduceVariableOption[] {
  if (!schema || typeof schema !== 'object') return []
  const record = schema as Record<string, any>
  const properties = record.properties && typeof record.properties === 'object' ? record.properties : null
  const produces = properties?.produces?.default ?? record.produces
  if (!Array.isArray(produces)) return []

  const seen = new Set<string>()
  const options: ProduceVariableOption[] = []
  for (const item of produces) {
    if (!item || typeof item !== 'object') continue
    const name = String(item.name ?? '').trim()
    if (!name || seen.has(name)) continue
    seen.add(name)
    options.push({ name, path: String(item.path ?? '').trim() })
  }
  return options
}

export function buildProduceVariableCatalog(tools: ProduceVariableToolLike[]): Record<string, ProduceVariableOption[]> {
  const catalog: Record<string, ProduceVariableOption[]> = {}
  for (const tool of tools) {
    const toolName = String(tool.tool_name ?? '').trim()
    if (!toolName || tool.category !== 'qkv' || tool.is_active === false) continue
    catalog[toolName] = extractProduceVariablesFromSchema(tool.parameters_schema)
  }
  return catalog
}

/** 从指定 QKV 工具目录中精确取得变量与 JSON 路径的绑定。 */
export function findProduceVariable(
  catalog: Record<string, ProduceVariableOption[]>,
  toolName: string,
  name: string,
): ProduceVariableOption | undefined {
  return catalog[toolName]?.find((option) => option.name === name)
}
