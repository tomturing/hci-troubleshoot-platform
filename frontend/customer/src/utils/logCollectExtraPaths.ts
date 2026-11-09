/**
 * 日志收集「额外收集路径」（acli `log_collect create -p`）的输入校验。
 *
 * 第一性原理：这条链路的最终形态是一条 shell 命令 ——
 *   `acli log_collect create ... -p '/a,/b'`
 * 而插件侧拼的是 `cp -r {path} {dest}`，**没有给路径加引号**
 * （single_collect.py:388）。因此一个含空格、分号或 `$()` 的路径，
 * 不只是"收不到"，而是能在客户节点上执行任意命令。
 *
 * 所以这里的规则与 bridge 的白名单**必须同源**（terminal_bridge/log_collect.go
 * 的 logCollectPathRe：`^/[A-Za-z0-9_./-]+$`），前端只负责让用户在提交前就看到
 * 拒绝原因，真正的防线仍在 bridge。前端放宽一分，用户就会在下发时才收到 422。
 *
 * 对抗性审查（为什么这些规则是硬性的，不是"体验优化"）：
 *  - 逗号：bridge 把多条路径拼成 `-p "a,b"`，含逗号的单条路径会被拆成两条；
 *  - 空格：插件未加引号，`cp -r /a b` 会把 `b` 当成目标目录；
 *  - `..`：越过 /sf 读取任意文件，且属于典型的路径穿越；
 *  - 中文/括号/引号：白名单外字符，bridge 会直接拒绝；提前拦下可避免"填完才失败"。
 */

/** 与后端 `extra_paths` 的 max_length=20 保持一致。 */
export const EXTRA_PATH_MAX_COUNT = 20
/** 单条路径长度上限：acli 侧无限制，但过长路径必然不是真实目录，且会撑爆命令行。 */
export const EXTRA_PATH_MAX_LENGTH = 512

/** 与 bridge 的 logCollectPathRe 同源：绝对路径 + 受限字符集。 */
const EXTRA_PATH_PATTERN = /^\/[A-Za-z0-9_./-]+$/

export interface ExtraPathValidation {
  ok: boolean
  /** 不合法时的可读原因（直接展示给用户，不暴露正则）。 */
  reason?: string
}

/**
 * 校验单条路径。
 *
 * 顺序不可随意调整：先判空再判前缀，最后才判字符集 ——
 * 否则空串会命中"必须是绝对路径"，用户看到的提示会与真实问题错位。
 */
export function validateExtraPath(raw: string): ExtraPathValidation {
  const value = raw.trim()
  if (!value) {
    return { ok: false, reason: '路径不能为空' }
  }
  if (value.length > EXTRA_PATH_MAX_LENGTH) {
    return { ok: false, reason: `路径过长（最多 ${EXTRA_PATH_MAX_LENGTH} 字符）` }
  }
  if (!value.startsWith('/')) {
    return { ok: false, reason: '必须是绝对路径（以 / 开头）' }
  }
  if (value.includes('..')) {
    return { ok: false, reason: '不允许包含 ..（禁止路径穿越）' }
  }
  if (value.includes(',')) {
    return { ok: false, reason: '单条路径不能包含逗号（会被拆成多条）' }
  }
  if (!EXTRA_PATH_PATTERN.test(value)) {
    return { ok: false, reason: '只允许字母、数字、下划线、点、斜杠与短横线，且不能含空格' }
  }
  return { ok: true }
}

export interface ParsedExtraPaths {
  /** 去重、去空、保持输入顺序的合法路径。 */
  accepted: string[]
  /** 被拒绝的项与原因，用于一次性提示用户，而不是逐条弹窗。 */
  rejected: Array<{ value: string; reason: string }>
  /** 因数量上限被截断的条数（0 表示未触发）。 */
  overflow: number
}

/**
 * 解析一次粘贴/输入的内容。
 *
 * 支持英文逗号与换行两种分隔：用户从文档里复制路径清单时，两种格式都常见；
 * 只支持逗号会让"每行一条"的粘贴变成一条超长非法路径。
 */
export function parseExtraPathInput(raw: string, existing: string[] = []): ParsedExtraPaths {
  const seen = new Set(existing.map((item) => item.trim()).filter(Boolean))
  const accepted: string[] = []
  const rejected: ParsedExtraPaths['rejected'] = []
  let overflow = 0

  const candidates = raw
    .split(/[\n,]/)
    .map((item) => item.trim())
    .filter(Boolean)

  for (const candidate of candidates) {
    const result = validateExtraPath(candidate)
    if (!result.ok) {
      rejected.push({ value: candidate, reason: result.reason ?? '格式非法' })
      continue
    }
    if (seen.has(candidate)) {
      continue
    }
    if (seen.size >= EXTRA_PATH_MAX_COUNT) {
      overflow += 1
      continue
    }
    seen.add(candidate)
    accepted.push(candidate)
  }

  return { accepted, rejected, overflow }
}

/**
 * 下发前收敛：去空、去重、保持顺序。
 *
 * 平台任务与 bridge 必须拿到**同一份**清单：前端去重后仍可能因历史回填
 * 出现重复项，重复不会报错但会让 `-p` 字符串变长，也可能让用户误以为
 * 同一条路径被收了两次。
 */
export function normalizeExtraPaths(list: string[] | null | undefined): string[] {
  if (!list?.length) return []
  const seen = new Set<string>()
  const result: string[] = []
  for (const item of list) {
    const value = String(item ?? '').trim()
    if (!value || seen.has(value)) continue
    seen.add(value)
    result.push(value)
  }
  return result
}

/** 数量上限的可读提示：达到上限后输入框需要禁用并说明原因。 */
export function extraPathsLimitHint(count: number): string {
  if (count >= EXTRA_PATH_MAX_COUNT) {
    return `已达上限 ${EXTRA_PATH_MAX_COUNT} 条，如需调整请先删除已有路径`
  }
  return `最多 ${EXTRA_PATH_MAX_COUNT} 条，当前 ${count} 条`
}
