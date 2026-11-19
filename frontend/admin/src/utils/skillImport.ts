/**
 * SKILL.md 导入解析（Agent Skills Open Standard）
 *
 * 字段映射与后端 shared/models/skill_definition.py 保持一致：
 *   name → skill_name，description → description，正文 → instructions_md，
 *   compatibility / license / allowed-tools / metadata 同名映射，
 *   display_name 为平台扩展字段（中文展示名）。
 *
 * frontmatter 仅覆盖标准涉及的子集：扁平标量（含引号/块标量）、
 * 内联数组、一级嵌套映射（metadata）；不引入完整 YAML 依赖。
 */

export interface ParsedSkillImport {
  skill_name: string
  display_name: string
  description: string
  instructions_md: string
  compatibility: string
  license: string
  allowed_tools: string
  meta_author: string
  meta_category: string
  meta_tags: string
}

/** 去除行尾注释（仅处理引号外的 #，避免误伤值内容） */
function stripInlineComment(raw: string): string {
  let quote: string | null = null
  for (let i = 0; i < raw.length; i++) {
    const ch = raw[i]
    if (quote) {
      if (ch === quote) quote = null
    } else if (ch === '"' || ch === "'") {
      quote = ch
    } else if (ch === '#' && (i === 0 || /\s/.test(raw[i - 1]))) {
      return raw.slice(0, i)
    }
  }
  return raw
}

/** 解析标量：支持单/双引号；其余按原样返回（trim 后） */
function parseScalar(raw: string): string {
  const value = stripInlineComment(raw).trim()
  if (value.length >= 2) {
    const first = value[0]
    const last = value[value.length - 1]
    if ((first === '"' && last === '"') || (first === "'" && last === "'")) {
      const inner = value.slice(1, -1)
      return first === '"'
        ? inner.replace(/\\(["\\nrt])/g, (_, c: string) =>
            ({ '"': '"', '\\': '\\', n: '\n', r: '\r', t: '\t' })[c] ?? c)
        : inner.replace(/''/g, "'")
    }
  }
  return value
}

/** 解析内联数组 [a, b, "c"]；非数组形式返回 null */
function parseInlineList(raw: string): string[] | null {
  const value = stripInlineComment(raw).trim()
  if (!value.startsWith('[') || !value.endsWith(']')) return null
  const inner = value.slice(1, -1).trim()
  if (!inner) return []
  return inner.split(',').map(item => parseScalar(item)).filter(Boolean)
}

interface FrontmatterEntry {
  key: string
  value: string
  children: { key: string; value: string }[]
}

/** 逐行解析 frontmatter：扁平 key: value + 一级缩进嵌套映射 + 块标量（| / >） */
function parseFrontmatterLines(lines: string[]): FrontmatterEntry[] {
  const entries: FrontmatterEntry[] = []
  let current: FrontmatterEntry | null = null
  let block: { indent: number; style: '|' | '>'; lines: string[] } | null = null

  const flushBlock = () => {
    if (!block || !current) return
    const joined = block.style === '|'
      ? block.lines.join('\n')
      : block.lines.join(' ').replace(/\s+/g, ' ').trim()
    current.value = joined
    block = null
  }

  for (const line of lines) {
    if (!line.trim() || line.trim().startsWith('#')) {
      if (block) block.lines.push('')
      continue
    }
    const indent = line.length - line.trimStart().length

    // 块标量续行：缩进更深（或空行）的行都属于块内容
    if (block) {
      if (indent > block.indent) {
        block.lines.push(line.slice(block.indent + 2))
        continue
      }
      flushBlock()
    }

    const match = line.trimStart().match(/^([A-Za-z0-9_-]+):\s*(.*)$/)
    if (!match) continue
    const [, key, restRaw] = match
    const rest = restRaw.trim()

    if (indent === 0) {
      current = { key, value: '', children: [] }
      entries.push(current)
      if (rest === '|' || rest === '>' || rest === '|-' || rest === '>-') {
        block = { indent, style: rest[0] as '|' | '>', lines: [] }
      } else {
        current.value = parseScalar(rest)
      }
    } else if (current && rest) {
      // 一级嵌套（如 metadata 下的 author/category/tags）
      current.children.push({ key, value: parseScalar(rest) })
    }
  }
  flushBlock()
  return entries
}

/**
 * 解析 SKILL.md 全文（YAML frontmatter + Markdown 正文）。
 * 解析失败或缺少必填字段时抛出 Error，message 可直接展示给用户。
 */
export function parseSkillMarkdown(text: string): ParsedSkillImport {
  const normalized = text.replace(/^\uFEFF/, '').replace(/\r\n?/g, '\n')
  const match = normalized.match(/^---\n([\s\S]*?)\n---[ \t]*(?:\n|$)/)
  if (!match) {
    throw new Error('未识别到 YAML frontmatter：文件须以 --- 包裹的元数据块开头')
  }
  const body = normalized.slice(match[0].length).replace(/^\n+/, '')

  const entries = parseFrontmatterLines(match[1].split('\n'))
  const fields = new Map(entries.map(e => [e.key, e]))
  const scalar = (key: string) => fields.get(key)?.value ?? ''

  const skillName = scalar('name')
  if (!skillName) throw new Error('frontmatter 缺少必填字段 name（技能标识）')
  const description = scalar('description')
  if (!description) throw new Error('frontmatter 缺少必填字段 description（技能描述）')

  // allowed-tools 标准允许字符串或列表，统一归一为空格分隔字符串
  const allowedEntry = fields.get('allowed-tools')
  let allowedTools = allowedEntry?.value ?? ''
  if (allowedEntry) {
    const list = parseInlineList(allowedEntry.value)
    if (list) allowedTools = list.join(' ')
  }

  const metaChildren = new Map((fields.get('metadata')?.children ?? []).map(c => [c.key, c.value]))
  const tagsRaw = metaChildren.get('tags') ?? ''
  const tagsList = parseInlineList(tagsRaw)
  const metaTags = tagsList ? tagsList.join(', ') : tagsRaw

  return {
    skill_name: skillName,
    display_name: scalar('display_name') || scalar('display-name'),
    description,
    instructions_md: body,
    compatibility: scalar('compatibility'),
    license: scalar('license'),
    allowed_tools: allowedTools,
    meta_author: metaChildren.get('author') ?? '',
    meta_category: metaChildren.get('category') ?? '',
    meta_tags: metaTags,
  }
}