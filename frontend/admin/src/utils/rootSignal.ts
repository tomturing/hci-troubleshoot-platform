// ============ 根因信号（root_signal_json）前端纯逻辑 ============
// 与关键信号（signals_json）完全解耦的第二抽取轨：一篇 KBD 可有多条**结论侧**
// 定位信号（v2 顶层数组，不限条数），用于在命中根因时快速圈定这一篇。
// 本模块只放可脱离 Vue 渲染的单测逻辑（解析/判级/格式化/请求体构造），
// 视图层（KbdReviewView.vue）负责装配与交互（逐条渲染信号列表）。
// 后端契约事实源：backend/kb-service/app/services/root_signal_extractor.py（schema_version=2）

export interface RootSignalAcquire {
  tool?: string
  args?: Record<string, any>
  [key: string]: any
}

export interface RootSignalProvenance {
  source_section?: string
  trace_tier?: 'root_cause' | 'content' | string
  source_sha256?: string
  source_category_id?: string
  method?: string
  evidence?: string
  confidence?: number
  needs_review?: boolean
  [key: string]: any
}

export interface RootSignal {
  schema_version?: number
  role?: string
  match_type?: 'executable' | 'text' | string
  signal_text?: string
  acquire?: RootSignalAcquire | null
  match?: { type?: string; mode?: string; expected?: string[] | any } | null
  distinctiveness?: { category_conflicts?: number; global_conflicts?: number } | null
  provenance?: RootSignalProvenance
  prompt_revision?: string
  [key: string]: any
}

/**
 * 解析 root_signal_json 为信号数组（v2 兼容入口）。
 * v2 落库为顶层数组；旧单对象/标量亦兜底归一为数组，无内容则空数组。
 */
export function parseSignals(raw: unknown): RootSignal[] {
  if (!raw) return []
  if (Array.isArray(raw)) return raw.filter((item) => item && typeof item === 'object') as RootSignal[]
  if (typeof raw === 'object') return [raw as RootSignal]
  return []
}

/** 冲突度判级：根因信号的价值就是「唯一定位」，故 0/0 与有冲突必须一眼可辨（按单条信号）。 */
export type ConflictLevel = 'unextracted' | 'unique' | 'category_only' | 'ambiguous'

export function distinctivenessLevel(signal?: RootSignal | null): ConflictLevel {
  if (!signal || !signal.distinctiveness) return 'unextracted'
  const global = Number(signal.distinctiveness.global_conflicts ?? 0)
  const category = Number(signal.distinctiveness.category_conflicts ?? 0)
  if (global > 0) return 'ambiguous'
  if (category > 0) return 'category_only'
  return 'unique'
}

/** 判级 → el-tag 类型 + 中文释义（视图层只管渲染，不懂阈值）。 */
export function conflictDisplay(level: ConflictLevel): { type: 'success' | 'warning' | 'danger' | 'info'; label: string } {
  switch (level) {
    case 'unique':
      return { type: 'success', label: '全库唯一定位' }
    case 'category_only':
      return { type: 'warning', label: '同类内不唯一' }
    case 'ambiguous':
      return { type: 'danger', label: '跨类撞车，锚点过泛' }
    default:
      return { type: 'info', label: '尚未抽取' }
  }
}

/** executable 型的采集动作摘要：工具 + 命令 + 关键参数，供审核人一眼看出「现场真能跑」。 */
export function formatAcquire(signal?: RootSignal | null): string {
  const acquire = signal?.acquire
  if (!acquire || typeof acquire !== 'object') return ''
  const args = (acquire.args || {}) as Record<string, any>
  const command = String(args.command || args.file || args.path || args.instruction || '').trim()
  const extra = Array.isArray(args.command_args) ? args.command_args.map(String).join(' ') : ''
  const target = String(args.host || '').trim()
  return [acquire.tool, [command, extra].filter(Boolean).join(' '), target].filter(Boolean).join(' ｜ ')
}

/** text 型关键词 / executable 判定串的统一取法（后端已扁平为字符串数组）。 */
export function matchKeywords(signal?: RootSignal | null): string[] {
  const expected = signal?.match?.expected
  if (!Array.isArray(expected)) return []
  return expected.map((item) => String(item ?? '').trim()).filter(Boolean)
}

export interface RootSignalBackfillForm {
  statuses: string[]
  onlyMissing: boolean
  categoryPrefix: string
  limit: number
}

/**
 * 构造全库回刷请求体。
 * 后端只在 draft/published 范围内回刷（archived/rejected 不参与线上定位），
 * 故前端把非法组合在提交前就挡掉，避免打到 422 后用户不知所云。
 */
export function buildBackfillPayload(form: RootSignalBackfillForm): Record<string, any> | string {
  const statuses = [...new Set(form.statuses || [])]
  if (!statuses.length) return '请至少选择一个回刷状态'
  if (statuses.some((s) => s !== 'draft' && s !== 'published')) return '回刷范围只能是草稿 / 已发布'
  const payload: Record<string, any> = {
    statuses,
    only_missing: form.onlyMissing,
    limit: Math.min(Math.max(Number(form.limit) || 1, 1), 100),
  }
  const prefix = String(form.categoryPrefix || '').trim()
  if (prefix) payload.category_prefix = prefix
  return payload
}

/** 回刷响应 → 人话（remaining>0 时必须告知还需继续驱动，否则用户以为已刷完）。 */
export function describeBackfillResult(data: {
  submitted?: number
  remaining?: number
  batch_id?: string
}): string {
  const submitted = Number(data?.submitted ?? 0)
  const remaining = Number(data?.remaining ?? 0)
  if (!submitted) return '本批没有符合条件的 KBD 可回刷'
  return remaining > 0
    ? `已提交 ${submitted} 条，待刷余量 ${remaining} 条，可继续提交下一批直至归零`
    : `已提交 ${submitted} 条，待刷余量 0 条 —— 全库回刷完成`
}
