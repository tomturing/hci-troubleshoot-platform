/**
 * 产出变量目录解析工具。
 *
 * 工具定义的 parameters_schema.properties.produces.default 是产出变量的
 * 唯一事实来源；这里集中处理旧数据兼容、空值和重复名称，避免不同页面
 * 各自实现一套稍有差异的解析规则。
 *
 * alias 字段说明（PR#790 后引入）：
 * - name：工具目录标准变量名，全局唯一，由工具管理维护。
 * - alias：KBD 级局部变量名，可选，由 KBD 审核者填写。
 * - effectiveKey = alias.trim() || name，是运行时变量池的实际写入 key。
 * - 同一信号的多个 produces 条目中，effectiveKey 必须互不相同。
 */
export interface ProduceVariableOption {
  name: string
  path: string
  /** KBD 级局部变量名别名，留空时运行时沿用 name。 */
  alias?: string
}

export interface ProduceVariableToolLike {
  tool_name?: unknown
  category?: unknown
  is_active?: unknown
  parameters_schema?: unknown
}

/**
 * 计算产出变量的运行时有效 key（单一真相源）。
 *
 * 优先使用 alias（非空时），否则回退到 name。
 * 调用方不应自行实现此逻辑，统一调用此函数。
 */
export function effectiveProduceKey(produce: Pick<ProduceVariableOption, 'name' | 'alias'>): string {
  const a = (produce.alias ?? '').trim()
  return a || produce.name
}

/**
 * 为可视化编辑器读取产出变量草稿，保留尚未填写名称或路径的行。
 *
 * 编辑态的空行是用户正在输入的有效状态，不能复用目录解析的过滤规则。
 */
export function parseProduceVariableDraftsFromSchema(schema: unknown): ProduceVariableOption[] {
  if (!schema || typeof schema !== 'object') return []
  const record = schema as Record<string, any>
  const properties = record.properties && typeof record.properties === 'object' ? record.properties : null
  const produces = properties?.produces?.default ?? record.produces
  if (!Array.isArray(produces)) return []

  return produces
    .filter((item): item is Record<string, unknown> => Boolean(item && typeof item === 'object'))
    .map((item) => ({
      name: String(item.name ?? ''),
      path: String(item.path ?? ''),
      // alias 可选字段，缺失时为 undefined，保持向下兼容
      alias: item.alias !== undefined ? String(item.alias) : undefined,
    }))
}

/** 读取已保存变量目录，仅保留可供下游引用的具名变量。 */
export function extractProduceVariablesFromSchema(schema: unknown): ProduceVariableOption[] {
  const drafts = parseProduceVariableDraftsFromSchema(schema)

  const seen = new Set<string>()
  const options: ProduceVariableOption[] = []
  for (const item of drafts) {
    const name = item.name.trim()
    if (!name || seen.has(name)) continue
    seen.add(name)
    options.push({ name, path: item.path.trim(), alias: item.alias })
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
