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
  // 闭环字段（PR-3/PR-4）：仅 executable 携带；text 无闭包义务可为空
  closure_status?: 'closed' | 'dangling_requires' | 'hardcoded_literals' | 'capability_missing' | string
  requires?: string[]
  resolved_by?: { var?: string; by?: string; ref?: string }[]
  [key: string]: any
}

/** 拒绝候选（root_signal_rejected_json 元素，与后端 signals_json.rejected_candidates 同构）。 */
export interface RootSignalRejected {
  candidate?: RootSignal
  /** L3 剪枝项直接带 signal_text（无 candidate 包体） */
  signal_text?: string
  reason?: string
  reason_code?: string
  /** 被哪条更强信号覆盖（仅 reason_code=redundant_with_stronger_signal） */
  redundant_with?: string
  closure_status?: string
  unresolved?: string[]
  literals?: { field?: string; value?: string; entity?: string; suggested_var?: string }[]
  [key: string]: any
}

/**
 * 审计条目的展示锚点：闭环拒绝候选在 candidate.signal_text，L3 剪枝项直接带 signal_text。
 * 两者同列共存，故取值必须兼容两种形态，否则剪枝项在 UI 全是「无锚点描述」。
 */
export function rejectedAnchor(item: RootSignalRejected): string {
  return String(item?.candidate?.signal_text || item?.signal_text || '')
}

/**
 * 审计条目类型：剪枝（合法但同篇冗余）与硬拒（不可闭环）语义不同，
 * 混在一起展示会让审核人误判「这条信号不可用」。
 */
export function rejectedKind(item: RootSignalRejected): 'pruned' | 'rejected' {
  return item?.reason_code === 'redundant_with_stronger_signal' ? 'pruned' : 'rejected'
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

/**
 * 解析 root_signal_rejected_json 为拒绝候选数组（v2 顶层数组，与 accepted 分列存储）。
 * 后端 CHECK 限 jsonb_typeof='array'，旧 null/非数组一律兑底空数组。
 */
export function parseRejected(raw: unknown): RootSignalRejected[] {
  if (!raw) return []
  if (Array.isArray(raw)) return raw.filter((item) => item && typeof item === 'object') as RootSignalRejected[]
  return []
}

/**
 * 闭包态 → el-tag 类型 + 中文释义（与后端 root_signal_closure 四态同源）。
 * text 形态无闭包义务（closure_status 缺省）→ 不渲染该标签（返 null）。
 */
export function closureDisplay(
  status?: string | null,
): { type: 'success' | 'warning' | 'danger' | 'info'; label: string } | null {
  switch (status) {
    case 'closed':
      return { type: 'success', label: '已闭环（现场可执行）' }
    case 'dangling_requires':
      return { type: 'danger', label: '变量悬空无来源' }
    case 'hardcoded_literals':
      return { type: 'warning', label: '含写死实体未参数化' }
    case 'capability_missing':
      return { type: 'danger', label: '采集能力缺失' }
    default:
      // text/未闭环旧数据：无闭包态，不渲染
      return null
  }
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
/** 关键词 textarea 文本 ⇄ match.expected 数组（逐行、去空、去重保序）。 */
export function splitKeywords(text: unknown): string[] {
  const seen = new Set<string>()
  const out: string[] = []
  for (const line of String(text ?? '').split('\n')) {
    const kw = line.trim()
    if (kw && !seen.has(kw)) {
      seen.add(kw)
      out.push(kw)
    }
  }
  return out
}

/** match.expected → textarea 文本（逐行）。 */
export function joinKeywords(expected: unknown): string {
  if (!Array.isArray(expected)) return ''
  return expected.map((x) => String(x ?? '')).join('\n')
}

/**
 * 逐条编辑态 → PATCH 请求体（全量替换）。
 * - 把 UI 辅助键 _expectedText/_argsJsonText 还原为后端契约（match.expected / acquire.args）；
 * - text 型强制剥离 acquire（后端硬门禁会拒，前端提前挡，避免用户打完才知错）；
 * - 采集参数 JSON 非法时返回错误串，前端据此就地提示，不打到后端 422。
 */
export function buildEditPayload(draft: any[]): Record<string, any> | string {
  const signals: any[] = []
  for (const raw of draft || []) {
    const sig: any = JSON.parse(JSON.stringify(raw ?? {}))
    const label = String(sig.signal_text || '').trim()
    if (typeof sig._expectedText === 'string') {
      sig.match = { ...(sig.match || {}), expected: splitKeywords(sig._expectedText) }
    }
    if (sig.match_type === 'executable' && sig.acquire && typeof sig._argsJsonText === 'string') {
      const trimmed = sig._argsJsonText.trim()
      if (!trimmed) {
        sig.acquire = { ...sig.acquire, args: {} }
      } else {
        let parsed: unknown
        try {
          parsed = JSON.parse(trimmed)
        } catch {
          return `信号「${label}」的采集参数不是合法 JSON`
        }
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          return `信号「${label}」的采集参数必须是 JSON 对象`
        }
        sig.acquire = { ...sig.acquire, args: parsed }
      }
    }
    // text 型绝不携带 acquire
    if (sig.match_type !== 'executable') sig.acquire = null
    delete sig._expectedText
    delete sig._argsJsonText
    sig.schema_version = 2
    sig.role = 'root_cause'
    signals.push(sig)
  }
  return { signals }
}

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
