<script setup lang="ts">
import { ref, computed, nextTick, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FullScreen, Refresh } from '@element-plus/icons-vue'
import { useCategories } from '../composables/useCategories'
import { marked } from 'marked'
import DOMPurify from 'dompurify'
import { MatcherEditor, ValueExtractEditor } from '@/components/editors'

// ──────────────────────────────────────────────────────────────────────────────
// 类型定义
// ──────────────────────────────────────────────────────────────────────────────
interface KbdMetadata {
  sangfor_main_module?: string | null
  sangfor_sub_module?: string | null
  suite_version?: string | null
  sangfor_updated_at?: string | null
  sangfor_created_at?: string | null
  create_admin_id?: string | null
  update_admin_id?: string | null
}

interface KbdEntry {
  id: number
  support_id: string
  title: string
  content_md: string
  images_json: ImageJsonItem[]  // 图片描述列表（权威数据源）
  metadata: KbdMetadata
  category_id: string | null
  ai_category_id: string | null
  ai_category_conf: number | null
  ai_category_reason: string | null
  status: string
  reviewer_id: number | null
  review_note: string | null
  created_at: string
  updated_at: string
  ai_category_label?: string | null
  // 关键信号集合：后端 GET 直出标准化 v2 文档 {schema_version, signals}（前端原生读 v2，RFC §7）
  signals_json?: SignalsDoc | null
  latest_proposal_revision_id?: number | null
  working_revision_id?: number | null
  lock_version?: number
  maintenance_working?: boolean
  review_view?: 'entry' | 'maintenance_working'
}

interface RevisionMetadata {
  id: number
  revision_no: number
  revision_type: 'proposal' | 'expert'
  checksum: string
  created_at?: string | null
  diff_from_parent?: Array<{ operation: string; path: string }>
}

interface KbdRevisionState {
  latest_proposal_revision_id: number | null
  working_revision_id: number | null
  lock_version: number
  history: RevisionMetadata[]
  active_resource: { revision: number; checksum: string; version: string } | null
}

interface CapabilityDescriptor {
  capability_id: string
  kind: 'producer' | 'consumer'
  contract_status: string
  runtime_status: string
  verification_status: string
  args_schema: Record<string, any>
}

interface CandidateValidation {
  status: 'ok' | 'warning' | 'error'
  publishable: boolean
  runtime_verified: boolean
  error_count: number
  warning_count: number
  issues: Array<{
    level: string
    code: string
    location: string
    message: string
    action?: { type: string; signal_id?: string; suggested_tool?: string }
  }>
  platform_status?: Array<Record<string, unknown>>
}

interface CommandPreview {
  tool: string
  command: string
  host?: string | null
  variables?: string[]
  notice?: string
}

// ============ 关键信号 v2 数据模型（RFC §7 前端原生读 v2 对象化，2026-07-22） ============
// GET 边界直接返回 v2 文档，前端不再归一/适配，直接基于该结构渲染与编辑；
// 回写时仍发回完整 v2 文档（{schema_version, signals}），后端 update_kbd_entry 幂等归约。
interface SignalV2 {
  id?: number | string
  role?: 'must' | 'should' | 'exclude' | 'context'
  acquire: { tool: string; args: Record<string, any> }
  match: { type?: string; pattern?: string | string[]; mode?: string; expected?: boolean; value?: number; [key: string]: any } | null
  orchestrate: Record<string, any>
  provenance?: Record<string, any>
  review?: { require_human_confirm?: boolean }
}
interface SignalsDoc {
  schema_version: number
  signals: SignalV2[]
  rejected_candidates?: Array<{ candidate: unknown; reason: string }>
  verification_contract?: Record<string, any>
  generation_metadata?: Record<string, any>
  publish_validation?: Record<string, any>
}

interface ChangeAnnotation {
  path?: string
  signal_id?: string
  reason_code: string
  note?: string
}

// 图片描述项（images_json 数组元素）
interface ImageJsonItem {
  seq: number           // 图片序号
  section: string       // 所属章节字段
  desc: string          // desc.txt v3 内容
  context_before?: string // 图片前方原文上下文（Evidence provenance）
  context_after?: string  // 图片后方原文上下文（Evidence provenance）
  evidence?: Record<string, unknown> // Vision Evidence IR
}

// 生成深信服案例原始页面 URL
const SANGFOR_BASE_URL = 'https://support.sangfor.com.cn'
function makeSupportUrl(supportId: string): string {
  return `${SANGFOR_BASE_URL}/cases/list?product_id=33&type=1&category_id=${supportId}&isOpen=true`
}

interface PendingKbdResponse {
  entries: KbdEntry[]
  total: number
  page: number
  page_size: number
}



// 截图说明解析类型
interface ScreenshotTypeInfo {
  label: string   // "告警截图"
  color: string   // 前景色
  bgColor: string // 背景色
  icon: string    // emoji 图标
}

interface ScreenshotFields {
  // ── v2 字段（PaddleOCR + LLM 双引擎新格式）─────────────────────────────────
  /** 背景颜色（黑色/白色/其他），后端 Pillow 采样决定 */
  background: string
  /** 截图类型（终端/日志/告警/任务/弹框/配置/其他截图），后端识别结果 */
  screenshotType: string
  /** PaddleOCR 全量文字行 */
  fullText: string[]
  /**
   * 截断后的可见内容（根据截图类型决定方向）：
   *   终端/日志截图 → FULL_TEXT 后 N 行（最新输出在末尾）
   *   告警/任务/弹框/配置截图 → FULL_TEXT 前 N 行（最新内容在最前）
   */
  visibleContent: string[]
  /** 历史 v2 类型相关关键内容（KEY 字段）；Evidence v3 不再生成 */
  key: string[]
  /** 历史 v2 排障建议（TIPS 字段）；Evidence v3 不再生成 */
  tips: string[]
  /** 语义描述（DESCRIPTION 字段）：是否可信由 images_json Evidence quality 决定 */
  description: string
  // ── v1 兼容字段（旧格式 0-4 字段，新条目不再写入）────────────────────────
  intro: string
  bgColorText: string
  typeName: string
  errorContent: string[]
  techTips: string[]
}

interface NormalSegment {
  type: 'normal'
  html: string
}

interface ScreenshotSegment {
  type: 'screenshot'
  typeInfo: ScreenshotTypeInfo
  errorLabel: string
  fields: ScreenshotFields
  expanded: boolean
  seq?: number
  /** 与 images_json 权威 Evidence 的关联；同一图片允许在正文中被多次引用 */
  evidence?: ParsedImageJson
}

type ContentSegment = NormalSegment | ScreenshotSegment

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态
// ──────────────────────────────────────────────────────────────────────────────
const loading = ref(false)
const entries = ref<KbdEntry[]>([])
const total = ref(0)
const categoryFilter = ref('')
const statusFilter = ref('')

const STATUS_MAP: Record<string, string> = {
  draft: '待审核', published: '已发布', rejected: '已拒绝', archived: '已归档',
}
function statusLabel(s: string) { return STATUS_MAP[s] || s }
const supportIdFilter = ref('')
const titleKeywordFilter = ref('')
const confidenceFilter = ref('')
const sortBy = ref('updated_at')
const sortOrder = ref('desc')

function handleSortChange({ prop, order }: { prop: string; order: string | null }) {
  sortBy.value = prop || 'updated_at'
  sortOrder.value = order === 'ascending' ? 'asc' : 'desc'
  catPage.value = 1
  fetchPending()
}

// ── 按 AI 分类 Tab 分组 ──
interface CategoryStat {
  category_id: string
  category_label: string
  count: number
}
const categoryStats = ref<CategoryStat[]>([])
const activeCategory = ref('__all__')      // '__all__' = 全部
const statsLoading = ref(false)

// 当前 Tab 的分页
const catPage = ref(1)
const catPageSize = ref(20)

async function fetchCategoryStats() {
  statsLoading.value = true
  try {
    const params = new URLSearchParams({ status: statusFilter.value })
    const resp = await fetch(`/api/v1/kbd/pending/stats?${params}`, { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    categoryStats.value = data.stats || []
  } catch {
    categoryStats.value = []
  } finally {
    statsLoading.value = false
  }
}

function onTabChange(tab: string) {
  activeCategory.value = tab
  catPage.value = 1
  fetchPending()
}

function handleCatPageChange(p: number) {
  catPage.value = p
  fetchPending()
}

const { categoryOptions, categoriesLoading, fetchCategories } = useCategories()

// 详情弹窗
const detailDialogVisible = ref(false)
const detailFullscreen = ref(false)
const detailEntry = ref<KbdEntry | null>(null)
const canEditCurrent = computed(() =>
  detailEntry.value?.status !== 'published' || Boolean(detailEntry.value?.maintenance_working),
)
const reviewNote = ref('')
const editableCategoryId = ref('')

// 详情弹窗 — 内容内联编辑
const editingContent = ref(false)
const inlineContent = ref('')
const inlineEditLoading = ref(false)
const parsedSegments = ref<ContentSegment[]>([])

// 解析后的 images_json 图片列表
interface ParsedImageJson {
  seq: number
  section: string
  background: string
  typeInfo: ScreenshotTypeInfo
  fullText: string[]
  visibleContent: string[]
  description: string
  contextBefore: string
  contextAfter: string
  observedFacts: string[]
  inferences: string[]
  qualityStatus: string
  needsReview: boolean
  inferenceStatus: string
  inferenceNeedsReview: boolean
  inferenceIssues: string[]
  provenance: Record<string, any>
  expanded: boolean
}
const parsedImagesJson = ref<ParsedImageJson[]>([])
interface ImageEditDraft {
  seq: number
  section: string
  background: string
  screenshotType: string
  fullText: string
  description: string
  observedFacts: string
  inferences: string
}
const editingImageSeq = ref<number | null>(null)
const imageEditDraft = ref<ImageEditDraft | null>(null)
const imageSaveLoading = ref(false)
const revisionState = ref<KbdRevisionState | null>(null)
const revisionLoading = ref(false)
const capabilityMap = ref<Record<string, CapabilityDescriptor>>({})
const candidateValidation = ref<CandidateValidation | null>(null)
const candidateValidationLoading = ref(false)
const focusedSignalId = ref<string | null>(null)
const commandPreviews = ref<Record<string, CommandPreview | undefined>>({})
const commandPreviewErrors = ref<Record<string, string | undefined>>({})
const commandPreviewLoading = ref<Record<string, boolean | undefined>>({})
const expandedCommandPreviews = ref<Record<string, boolean | undefined>>({})

// 拒绝弹窗
const rejectDialogVisible = ref(false)
const rejectingEntry = ref<KbdEntry | null>(null)
const rejectNote = ref('')
const rejectLoading = ref(false)

// 当前部署尚未接入 Admin SSO。不得再用固定 currentUser=1 冒充真实专家；在发布/拒绝
// 等形成审核事实前，显式要求操作者填写其审核 ID，并由后端标记为 unverified。
const savedReviewerId = Number(window.localStorage.getItem('hci-admin-reviewer-id') || 0)
const currentUser = ref(Number.isInteger(savedReviewerId) && savedReviewerId > 0 ? savedReviewerId : 0)

async function ensureReviewerIdentity(): Promise<number | null> {
  if (currentUser.value > 0) return currentUser.value
  try {
    const result = await ElMessageBox.prompt(
      '当前环境尚未接入 Admin SSO。请输入你的真实审核人 ID；系统会如实记录为“未认证身份”，不会把它冒充为已认证 Expert Gold。',
      '填写审核身份',
      {
        confirmButtonText: '确认',
        cancelButtonText: '取消',
        inputPattern: /^[1-9]\d*$/,
        inputErrorMessage: '审核人 ID 必须是正整数',
      },
    )
    currentUser.value = Number(result.value)
    window.localStorage.setItem('hci-admin-reviewer-id', String(currentUser.value))
    return currentUser.value
  } catch {
    return null
  }
}

async function changeReviewerIdentity() {
  currentUser.value = 0
  await ensureReviewerIdentity()
}

// 编辑弹窗
const editDialogVisible = ref(false)
const editFullscreen = ref(false)
const editingEntry = ref<KbdEntry | null>(null)
const editTitle = ref('')
const editContent = ref('')
const editCategoryId = ref('')
const editLoading = ref(false)

// ──────────────────────────────────────────────────────────────────────────────
// API
// ──────────────────────────────────────────────────────────────────────────────
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

function kbdEditEndpoint(entry: KbdEntry): string {
  return entry.maintenance_working
    ? `/api/v1/kbd/${entry.id}/maintenance`
    : `/api/v1/kbd/${entry.id}`
}

function applyMaintenanceResponse(entry: KbdEntry, responseBody: any): void {
  if (!responseBody?.payload) return
  Object.assign(entry, responseBody.payload)
  entry.maintenance_working = true
  entry.review_view = 'maintenance_working'
  entry.working_revision_id = responseBody.working_revision_id
  entry.lock_version = responseBody.lock_version
}

async function fetchPending() {
  loading.value = true
  try {
    const params = new URLSearchParams({
      page: String(catPage.value),
      page_size: String(catPageSize.value),
    })
    if (statusFilter.value) {
      params.append('status', statusFilter.value)
    }
    // Tab 分类过滤（'__all__' = 不限制）
    if (activeCategory.value && activeCategory.value !== '__all__') {
      params.append('category_id', activeCategory.value)
    }
    // 额外的手动分类筛选（叠加到 Tab 之上）
    if (categoryFilter.value) {
      params.append('category_id', categoryFilter.value)
    }
    if (supportIdFilter.value) {
      params.append('support_id', supportIdFilter.value)
    }
    if (titleKeywordFilter.value) {
      params.append('title_keyword', titleKeywordFilter.value)
    }
    if (confidenceFilter.value) {
      const [minStr, maxStr] = confidenceFilter.value.split(',')
      if (minStr) params.append('min_confidence', minStr)
      if (maxStr) params.append('max_confidence', maxStr)
    }
    params.append('sort_by', sortBy.value)
    params.append('sort_order', sortOrder.value)
    const resp = await fetch(`/api/v1/kbd/pending?${params}`, {
      headers: authHeader,
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data: PendingKbdResponse = await resp.json()
    entries.value = data.entries
    total.value = data.total
    // 刷新分类统计（切换 Tab 或过滤条件变化时）
    fetchCategoryStats()
  } catch {
    ElMessage.error('加载 KBD 条目失败，请刷新重试')
  } finally {
    loading.value = false
  }
}



async function handleApprove(entry: KbdEntry) {
  if (!(await ensureReviewerIdentity())) return
  try {
    await ElMessageBox.confirm(
      `确认通过此 KBD 条目？\n\n「${entry.title}」`,
      '审核通过',
      { confirmButtonText: '确认发布', cancelButtonText: '取消', type: 'success' },
    )
    const resp = await fetch(`/api/v1/kbd/${entry.id}/approve`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        reviewer_id: currentUser.value,
        review_note: reviewNote.value || '',
        category_id: editableCategoryId.value || entry.ai_category_id || null,
        lock_version: entry.lock_version,
      }),
    })
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try {
        const errBody = await resp.json()
        if (errBody.detail) detail = typeof errBody.detail === 'string' ? errBody.detail : JSON.stringify(errBody.detail)
      } catch { /* ignore parse error */ }
      throw new Error(detail)
    }
    ElMessage.success('审核通过，KBD 条目已发布')
    detailDialogVisible.value = false
    await fetchPending()
  } catch (e: unknown) {
    const msg = (e as { message?: string })?.message || ''
    if (msg === 'cancel') return
    ElMessage.error(msg || '操作失败，请重试')
  }
}

async function fetchCapabilities() {
  try {
    const resp = await fetch('/api/v1/kbd/capabilities', { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const document = await resp.json()
    capabilityMap.value = Object.fromEntries(
      (document.capabilities || []).map((item: CapabilityDescriptor) => [item.capability_id, item]),
    )
  } catch {
    capabilityMap.value = {}
  }
}

async function fetchRevisionState(kbdId: number) {
  revisionLoading.value = true
  try {
    const resp = await fetch(`/api/v1/kbd/${kbdId}/revisions`, { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    revisionState.value = await resp.json()
  } catch {
    revisionState.value = null
  } finally {
    revisionLoading.value = false
  }
}

async function validateCurrentCandidate(options: { silent?: boolean } = {}) {
  if (!detailEntry.value) return
  candidateValidationLoading.value = true
  try {
    const resp = await fetch(`/api/v1/kbd/${detailEntry.value.id}/validate`, {
      method: 'POST',
      headers: authHeader,
    })
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${resp.status}`)
    candidateValidation.value = body
    if (!options.silent) {
      if (body.status === 'ok') ElMessage.success('当前专家稿校验通过')
      else if (body.status === 'warning') ElMessage.warning(`有 ${body.warning_count} 项内容需要专家确认`)
      else ElMessage.error(`发现 ${body.error_count} 个阻断问题，请按提示修复`)
    }
  } catch (error) {
    candidateValidation.value = null
    if (!options.silent) ElMessage.error(error instanceof Error ? error.message : '校验失败')
  } finally {
    candidateValidationLoading.value = false
  }
}

function signalDomId(signalId: string): string {
  return `kbd-signal-${signalId.replace(/[^a-zA-Z0-9_-]/g, '_')}`
}

function signalLabel(signal: SignalV2, index: number): string {
  const instruction = String(sigArgs(signal).instruction || '').trim()
  return `${sigTool(signal) || '未选择采集类型'} · ${instruction || `信号 ${index + 1}`}`
}

function validationIssueSignal(issue: CandidateValidation['issues'][number]): { signal: SignalV2; index: number } | null {
  const signalId = issue.action?.signal_id
  if (!signalId) return null
  const index = signalList.value.findIndex((signal) => String(signal.id) === String(signalId))
  return index >= 0 ? { signal: signalList.value[index], index } : null
}

async function focusSignal(signalId: string): Promise<void> {
  focusedSignalId.value = signalId
  await nextTick()
  document.getElementById(signalDomId(signalId))?.scrollIntoView({ behavior: 'smooth', block: 'center' })
}

async function handleValidationAction(issue: CandidateValidation['issues'][number]) {
  const signalId = issue.action?.signal_id
  if (!signalId) {
    if (issue.action?.type === 'edit_signal_role' && signalList.value.length) {
      startEditSignal(0)
      await focusSignal(signalStableId(signalList.value[0], 0))
      ElMessage.info('请在“证据作用”中将至少一条可执行信号设为“必要证据”后保存')
    }
    return
  }
  const index = signalList.value.findIndex((signal) => String(signal.id) === String(signalId))
  if (index < 0) return
  startEditSignal(index)
  if (issue.action?.suggested_tool) onSignalToolChange(issue.action.suggested_tool)
  await focusSignal(String(signalId))
  ElMessage.info('已定位并按建议切换采集类型，请复核参数后保存')
}

function capabilityStatus(tool: string): 'declared' | 'missing' {
  return capabilityMap.value[tool] ? 'declared' : 'missing'
}

function openRejectDialog(entry: KbdEntry) {
  rejectingEntry.value = entry
  rejectNote.value = ''
  rejectDialogVisible.value = true
}

async function submitReject() {
  if (!rejectingEntry.value) return
  if (!(await ensureReviewerIdentity())) return
  if (!rejectNote.value.trim()) {
    ElMessage.warning('请填写拒绝原因')
    return
  }
  rejectLoading.value = true
  try {
    const resp = await fetch(`/api/v1/kbd/${rejectingEntry.value.id}/reject`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        reviewer_id: currentUser.value,
        review_note: rejectNote.value,
      }),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('已拒绝，状态更新为 rejected')
    entries.value = entries.value.filter((e) => e.id !== rejectingEntry.value!.id)
    total.value -= 1
    rejectDialogVisible.value = false
    detailDialogVisible.value = false
  } catch {
    ElMessage.error('操作失败，请重试')
  } finally {
    rejectLoading.value = false
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 重新分类 & 重新识图（Prompt 修改后立即验证效果）
// ──────────────────────────────────────────────────────────────────────────────

const reclassifyLoading = ref<number | null>(null)  // 正在重分类的 entry.id
const reanalyzeLoading = ref<number | null>(null)   // 正在重识图的 entry.id
const reanalyzeSingleLoading = ref<{ kbdId: number; seq: number } | null>(null)  // 正在重识图的单张图片

async function handleReclassify(entry: KbdEntry) {
  try {
    await ElMessageBox.confirm(
      `确认用最新 Prompt 重新分类「${entry.title}」？`,
      '重新分类',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  reclassifyLoading.value = entry.id
  try {
    const resp = await fetch(`/api/v1/kbd/${entry.id}/reclassify`, {
      method: 'POST',
      headers: authHeader,
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data = await resp.json()
    ElMessage.success(`分类完成：${data.category_id}（置信度 ${data.confidence?.toFixed(2) || 'N/A'}）`)
    // 刷新列表中的该条目
    const idx = entries.value.findIndex(e => e.id === entry.id)
    if (idx !== -1) {
      entries.value[idx].ai_category_id = data.category_id
      entries.value[idx].ai_category_conf = data.confidence
      entries.value[idx].ai_category_reason = data.reason
    }
    // 刷新详情中的该条目
    if (detailEntry.value && detailEntry.value.id === entry.id) {
      detailEntry.value.ai_category_id = data.category_id
      detailEntry.value.ai_category_conf = data.confidence
      detailEntry.value.ai_category_reason = data.reason
    }
  } catch (err: any) {
    ElMessage.error(`重新分类失败：${err.message || '未知错误'}`)
  } finally {
    reclassifyLoading.value = null
  }
}

async function handleReanalyzeImages(entry: KbdEntry) {
  try {
    await ElMessageBox.confirm(
      `确认用最新 Prompt 重新识图「${entry.title}」？\n\n注意：重新识图耗时较长，请耐心等待。`,
      '重新识图',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  reanalyzeLoading.value = entry.id
  try {
    // 临时修复：使用同步模式避免异步轮询未实现导致的 undefined 问题
    // TODO: 后续实现异步轮询机制以避免长时间HTTP连接超时
    const resp = await fetch(`/api/v1/kbd/${entry.id}/reanalyze-images?sync=true`, {
      method: 'POST',
      headers: authHeader,
    })
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}))
      throw new Error(errData.detail || `HTTP ${resp.status}`)
    }
    const data = await resp.json()
    if (data.total === 0) {
      ElMessage.warning(data.message || '该 KBD 无原始图片，无法重算识图')
    } else {
      ElMessage.success({
        message: `识图完成：成功 ${data.done} 张，失败 ${data.failed} 张`,
        duration: 0,
        showClose: true,
      })
    }
    // 刷新详情（如果打开）
    if (detailEntry.value?.id === entry.id) {
      // 重新获取该条目详情
      const detailResp = await fetch(`/api/v1/kbd/${entry.id}`, { headers: authHeader })
      if (detailResp.ok) {
        const fresh = await detailResp.json()
        detailEntry.value = fresh
        parsedImagesJson.value = parseImagesJson(fresh.images_json || [])
        parsedSegments.value = parseContentMd(fresh.content_md || '')
        associateSegmentsWithSeq(parsedSegments.value, parsedImagesJson.value)
        // 同时更新列表里的该条目
        const idx = entries.value.findIndex(e => e.id === entry.id)
        if (idx !== -1) {
          entries.value[idx].content_md = fresh.content_md
          entries.value[idx].images_json = fresh.images_json
        }
      }
    }
  } catch (err: any) {
    ElMessage.error({
      message: `重新识图失败：${err.message || '未知错误'}`,
      duration: 0,
      showClose: true,
    })
  } finally {
    reanalyzeLoading.value = null
  }
}

// 单张图片重新识图
async function handleReanalyzeSingleImage(entry: KbdEntry, seq: number) {
  try {
    await ElMessageBox.confirm(
      `确认重新识图第 ${seq + 1} 张图片（img_${seq}）？`,
      '单张重新识图',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  reanalyzeSingleLoading.value = { kbdId: entry.id, seq }
  try {
    // 临时修复：使用同步模式避免异步轮询未实现导致的 undefined 问题
    // TODO: 后续实现异步轮询机制以避免长时间HTTP连接超时
    const resp = await fetch(`/api/v1/kbd/${entry.id}/reanalyze-image/${seq}?sync=true`, {
      method: 'POST',
      headers: authHeader,
    })
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}))
      throw new Error(errData.detail || `HTTP ${resp.status}`)
    }
    const data = await resp.json()
    ElMessage.success({
      message: `识图完成：${data.screenshot_type}`,
      duration: 0,  // 不自动关闭
      showClose: true,
    })
    // 刷新详情（如果打开）
    if (detailEntry.value?.id === entry.id) {
      // 重新获取该条目详情
      const detailResp = await fetch(`/api/v1/kbd/${entry.id}`, { headers: authHeader })
      if (detailResp.ok) {
        const fresh = await detailResp.json()
        detailEntry.value = fresh
        parsedImagesJson.value = parseImagesJson(fresh.images_json || [])
        parsedSegments.value = parseContentMd(fresh.content_md || '')
        associateSegmentsWithSeq(parsedSegments.value, parsedImagesJson.value)
        // 同时更新列表里的该条目
        const idx = entries.value.findIndex(e => e.id === entry.id)
        if (idx !== -1) {
          entries.value[idx].content_md = fresh.content_md
          entries.value[idx].images_json = fresh.images_json
        }
      }
    }
  } catch (err: any) {
    ElMessage.error({
      message: `重新识图失败：${err.message || '未知错误'}`,
      duration: 0,  // 不自动关闭
      showClose: true,
    })
  } finally {
    reanalyzeSingleLoading.value = null
  }
}

// 关键信号重新抽取（与"重新识图"保持一致：同步模式立等结果 + 刷新详情）
const reextractSignalsLoading = ref<number | null>(null)  // 正在重新抽取信号的 entry.id

async function handleReextractSignals(entry: KbdEntry) {
  try {
    await ElMessageBox.confirm(
      `确认用最新 Prompt 重新抽取「${entry.title}」的关键信号抽取？`,
      '重新抽取关键信号',
      { confirmButtonText: '确认', cancelButtonText: '取消', type: 'warning' }
    )
  } catch {
    return
  }
  reextractSignalsLoading.value = entry.id
  try {
    const resp = await fetch(`/api/v1/kbd/${entry.id}/extract-signals?sync=true`, {
      method: 'POST',
      headers: authHeader,
    })
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}))
      throw new Error(errData.detail || `HTTP ${resp.status}`)
    }
    const data = await resp.json()
    ElMessage.success({
      message: `关键信号抽取完成：共 ${data.signals_count ?? 0} 条（拒绝 ${data.rejected_count ?? 0} 条）`,
      duration: 0,
      showClose: true,
    })
    // 刷新详情（如果打开）
    if (detailEntry.value?.id === entry.id) {
      const detailResp = await fetch(`/api/v1/kbd/${entry.id}`, { headers: authHeader })
      if (detailResp.ok) {
        const fresh = await detailResp.json()
        detailEntry.value = fresh
      }
    }
  } catch (err: any) {
    ElMessage.error({
      message: `重新抽取关键信号失败：${err.message || '未知错误'}`,
      duration: 0,
      showClose: true,
    })
  } finally {
    reextractSignalsLoading.value = null
  }
}

async function openDetailDialog(entry: KbdEntry) {
  detailFullscreen.value = false
  detailDialogVisible.value = true
  editingContent.value = false
  clearStagedSignalEdits()
  cancelEditSignal()
  revisionState.value = null
  candidateValidation.value = null
  focusedSignalId.value = null
  commandPreviews.value = {}
  commandPreviewErrors.value = {}
  commandPreviewLoading.value = {}
  expandedCommandPreviews.value = {}
  void fetchRevisionState(entry.id)
  // 拉取完整详情（确保含 signals_json）
  try {
    const resp = await fetch(`/api/v1/kbd/${entry.id}`, { headers: authHeader })
    if (resp.ok) {
      const fresh = await resp.json()
      detailEntry.value = fresh
      reviewNote.value = fresh.review_note || ''
      editableCategoryId.value = fresh.category_id || fresh.ai_category_id || ''
      inlineContent.value = fresh.content_md || ''
      parsedSegments.value = parseContentMd(fresh.content_md || '')
      parsedImagesJson.value = parseImagesJson(fresh.images_json || [])
      associateSegmentsWithSeq(parsedSegments.value, parsedImagesJson.value)
      void validateCurrentCandidate({ silent: true })
      return
    }
  } catch {
    // 回退到列表项
  }
  detailEntry.value = entry
  reviewNote.value = entry.review_note || ''
  editableCategoryId.value = entry.category_id || entry.ai_category_id || ''
  inlineContent.value = entry.content_md || ''
  parsedSegments.value = parseContentMd(entry.content_md || '')
  parsedImagesJson.value = parseImagesJson(entry.images_json || [])
  associateSegmentsWithSeq(parsedSegments.value, parsedImagesJson.value)
  void validateCurrentCandidate({ silent: true })
}

async function refreshOpenedDetail(): Promise<void> {
  if (!detailEntry.value) return
  const response = await fetch(`/api/v1/kbd/${detailEntry.value.id}`, { headers: authHeader })
  if (!response.ok) throw new Error(`HTTP ${response.status}`)
  const fresh = await response.json()
  detailEntry.value = fresh
  editableCategoryId.value = fresh.category_id || fresh.ai_category_id || ''
  inlineContent.value = fresh.content_md || ''
  parsedSegments.value = parseContentMd(fresh.content_md || '')
  parsedImagesJson.value = parseImagesJson(fresh.images_json || [])
  associateSegmentsWithSeq(parsedSegments.value, parsedImagesJson.value)
  await fetchRevisionState(fresh.id)
  await validateCurrentCandidate({ silent: true })
}

async function createMaintenanceWorking() {
  if (!detailEntry.value) return
  try {
    const response = await fetch(`/api/v1/kbd/${detailEntry.value.id}/maintenance`, {
      method: 'POST',
      headers: authHeader,
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail?.message || body.detail || `HTTP ${response.status}`)
    applyMaintenanceResponse(detailEntry.value, body)
    await refreshOpenedDetail()
    ElMessage.success('维护工作稿已创建；Agent 继续使用当前生效版')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '创建维护工作稿失败')
  }
}

async function discardMaintenanceWorking() {
  if (!detailEntry.value) return
  try {
    await ElMessageBox.confirm(
      '确认放弃当前维护工作稿？历史 Revision 会保留，Agent 当前生效版不受影响。',
      '放弃维护工作稿',
      { type: 'warning', confirmButtonText: '确认放弃', cancelButtonText: '取消' },
    )
    const response = await fetch(`/api/v1/kbd/${detailEntry.value.id}/maintenance`, {
      method: 'DELETE',
      headers: authHeader,
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`)
    await refreshOpenedDetail()
    ElMessage.success('维护工作稿已放弃，Agent 当前生效版未改变')
  } catch (error) {
    if ((error as { message?: string })?.message === 'cancel') return
    ElMessage.error(error instanceof Error ? error.message : '放弃维护工作稿失败')
  }
}

async function publishMaintenanceWorking() {
  if (!detailEntry.value) return
  if (!(await ensureReviewerIdentity())) return
  try {
    await validateCurrentCandidate()
    if (!candidateValidation.value?.publishable) return
    await ElMessageBox.confirm(
      '确认发布当前维护工作稿？发布成功后 Agent 将原子切换到新版本；失败时继续使用旧版本。',
      '发布维护版',
      { type: 'success', confirmButtonText: '确认发布', cancelButtonText: '取消' },
    )
    const response = await fetch(`/api/v1/kbd/${detailEntry.value.id}/maintenance/publish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        reviewer_id: currentUser.value,
        review_note: reviewNote.value || '',
        category_id: editableCategoryId.value || null,
        lock_version: detailEntry.value.lock_version,
      }),
    })
    const body = await response.json().catch(() => ({}))
    if (!response.ok) throw new Error(body.detail?.message || body.detail || `HTTP ${response.status}`)
    ElMessage.success('维护版已发布并生效')
    await refreshOpenedDetail()
    await fetchPending()
  } catch (error) {
    if ((error as { message?: string })?.message === 'cancel') return
    ElMessage.error(error instanceof Error ? error.message : '发布维护版失败')
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 关键信号面板（signals_json）：基于 v2 文档直接渲染/编辑（RFC §7 前端原生读 v2 对象化）
// ──────────────────────────────────────────────────────────────────────────────
// v2 原生读取辅助：直接从 v2 结构各段取值，不拍平/不更名。
function sigTool(sig: SignalV2): string { return sig.acquire?.tool || '' }
function sigArgs(sig: SignalV2): Record<string, any> { return sig.acquire?.args || {} }
function sigMatch(sig: SignalV2): Record<string, any> { return sig.match || {} }
function sigOrch(sig: SignalV2): Record<string, any> { return sig.orchestrate || {} }
function sigProvenance(sig: SignalV2): Record<string, any> { return sig.provenance || {} }
function sigSourceRefs(sig: SignalV2): string[] {
  const refs = sigProvenance(sig).source_refs
  return Array.isArray(refs) ? refs.map((item) => String(item)) : []
}
function sigRoleLabel(sig: SignalV2): string {
  return ({ must: '必要证据（必须满足）', should: '增强证据（按门槛满足）', exclude: '排除证据（出现即排除）', context: '上下文证据（执行但不参与结论）' } as Record<string, string>)[sig.role || ''] || '未分配'
}
function qualityTagType(status: string): 'success' | 'warning' | 'danger' | 'info' {
  if (['success', 'manual_reviewed'].includes(status)) return 'success'
  if (status === 'failed') return 'danger'
  if (['partial', 'low_quality', 'needs_review'].includes(status)) return 'warning'
  return 'info'
}
function isBackendSig(sig: SignalV2): boolean {
  return sigTool(sig).startsWith('qfk') || sig.provenance?.category === 'backend'
}

// 一个 KBD 的多条旧信号可能同时带有管道。暂存区必须用稳定 Signal ID，而不是数组
// 下标：专家排序或删除信号后，下标会变化，继续用下标会把草稿错误套到另一条信号。
const stagedSignalEdits = ref<Record<string, SignalV2>>({})
const newSignalId = ref<string | null>(null)

function cloneSignal(signal: SignalV2): SignalV2 {
  return JSON.parse(JSON.stringify(signal)) as SignalV2
}

const evidenceRoles = ['must', 'should', 'exclude', 'context'] as const
const changeReasonOptions = [
  ['source_missed', '原始案例遗漏'],
  ['screenshot_misread', '截图识别错误'],
  ['fact_inference_confused', '事实与推断混淆'],
  ['wrong_category', '分类错误'],
  ['wrong_capability', '工具能力错误'],
  ['invalid_argument', '工具参数错误'],
  ['missing_signal', '缺少关键信号'],
  ['redundant_signal', '冗余或示例信号'],
  ['unsafe_command', '命令不安全'],
  ['unsupported_semantics', '平台暂不支持的语义'],
  ['threshold_or_match_error', '判定或阈值错误'],
  ['wording_only', '仅表述优化'],
  ['other_expert_correction', '其他专家修正'],
] as const

/**
 * 专家只编辑每条 Signal 的“证据作用”。执行 Contract 是这份角色清单的派生
 * 投影，不能让删除/排序后的旧 signal_id 残留在 payload。后端会再次执行同一
 * 归一化，前端这一步仅用于即时预览和降低无意义的保存失败。
 */
function reconcileSignalContract(currentDoc: SignalsDoc, list: SignalV2[]): SignalsDoc {
  const payload = JSON.parse(JSON.stringify(currentDoc)) as SignalsDoc
  const previousPolicy = payload.verification_contract?.evidence_policy || {}
  const legacyRoles = Object.fromEntries(
    evidenceRoles.flatMap((role) => (previousPolicy[role] || []).map((id: unknown) => [String(id), role])),
  ) as Record<string, SignalV2['role']>
  const policy: Record<string, string[]> = Object.fromEntries(evidenceRoles.map((role) => [role, []]))
  payload.signals = list.map((raw) => {
    const signal = cloneSignal(raw)
    const id = String(signal.id || '')
    const role = evidenceRoles.includes(signal.role as typeof evidenceRoles[number])
      ? signal.role!
      : legacyRoles[id] || 'must'
    signal.role = role
    if (id) policy[role].push(id)
    return signal
  })
  const previousMinimum = Number(previousPolicy.minimum_should || 0)
  payload.verification_contract = {
    ...(payload.verification_contract || {}),
    schema_version: payload.verification_contract?.schema_version || 1,
    evidence_policy: {
      ...policy,
      minimum_should: Math.min(Math.max(0, Number.isFinite(previousMinimum) ? previousMinimum : 0), policy.should.length),
      on_missing_must: 'inconclusive',
    },
  }
  return payload
}

function signalStableId(signal: SignalV2, index: number): string {
  if (!signal.id) {
    signal.id = createSignalId()
    signal.provenance = {
      ...(signal.provenance || {}),
      needs_review: true,
      legacy_id_assigned: true,
      legacy_display_position: index + 1,
    }
  }
  return String(signal.id)
}

function createSignalId(): string {
  const random = typeof crypto !== 'undefined' && 'randomUUID' in crypto
    ? crypto.randomUUID().replace(/-/g, '').slice(0, 12)
    : Math.random().toString(36).slice(2, 14)
  return `expert_${Date.now()}_${random}`
}

function schemaDefaultArgs(tool: string): Record<string, any> {
  const schema = capabilityMap.value[tool]?.args_schema || {}
  const args: Record<string, any> = {}
  for (const [name, property] of Object.entries(schema.properties || {}) as Array<[string, Record<string, any>]>) {
    if (property.default !== undefined) args[name] = cloneSignal(property.default as any)
  }
  return args
}

function defaultProduces(tool: string): Array<Record<string, any>> {
  if (tool === 'qkv_dialog') {
    return [
      { name: 'END', path: 'end' },
      { name: 'REQUEST_ID', path: 'request_id' },
      { name: 'HOST', path: 'host' },
    ]
  }
  if (tool === 'qkv_task') {
    return [
      { name: 'VM', path: 'vm' },
      { name: 'HOST', path: 'host' },
      { name: 'END', path: 'end' },
    ]
  }
  if (tool === 'qkv_alert') {
    return [
      { name: 'HOST', path: 'host' },
      { name: 'END', path: 'end' },
    ]
  }
  return []
}

function buildSignalForTool(tool: string, previous?: SignalV2): SignalV2 {
  const producer = tool.startsWith('qkv')
  const oldArgs = previous?.acquire?.args || {}
  const args = schemaDefaultArgs(tool)
  if (typeof oldArgs.instruction === 'string') args.instruction = oldArgs.instruction
  if (producer && typeof oldArgs.keyword === 'string') args.keyword = oldArgs.keyword
  if (tool === 'qkv_task') args.is_failed = true
  if (tool === 'qkv_dialog') {
    args.paths = ['/sf/log/today', '/sf/log/today/vt']
    args.context_lines ??= 2
  }
  // qfk_system 在宿主机执行时，持久化契约通过省略 container 表达；编辑器使用
  // host 作为显式的默认选项，便于专家把已选择的 aCLI 容器恢复为宿主机。
  if (tool === 'qfk_system') args.container = 'host'
  return {
    id: previous?.id || createSignalId(),
    role: previous?.role || 'should',
    acquire: { tool, args },
    match: producer ? null : { type: 'keyword', pattern: '', mode: 'or', expected: true },
    orchestrate: {
      phase: previous?.orchestrate?.phase || 'diagnostic',
      produces: producer ? defaultProduces(tool) : [],
      requires: [],
    },
    provenance: {
      ...(previous?.provenance || {}),
      category: producer ? 'frontend' : 'backend',
      needs_review: true,
      expert_created: previous ? previous.provenance?.expert_created : true,
    },
    review: { require_human_confirm: true },
  }
}

function onSignalToolChange(tool: string) {
  signalEditDraft.value = buildSignalForTool(tool, signalEditDraft.value)
  if (tool.startsWith('qfk')) syncDraftRequires()
}

function qfkSystemCommandText(args: Record<string, any>): string {
  return [args.command, ...(Array.isArray(args.command_args) ? args.command_args : [])]
    .filter((item) => typeof item === 'string' && item.trim())
    .join(' ')
}

function setQfkSystemCommandText(args: Record<string, any>, value: string) {
  // 后端在保存时用 shlex 做权威分词；这里保留完整编辑文本，避免前端猜测引号语义。
  args.command = value
  args.command_args = []
  syncDraftRequires()
}

function qfkSystemCommandPreview(args: Record<string, any>): string {
  const timeoutRaw = Number(args.timeout || 120)
  const timeout = Number.isFinite(timeoutRaw) && timeoutRaw > 0 ? timeoutRaw : 120
  const cluster = args.cluster ? ' --cluster' : ''
  const formatterValue = typeof args.formatter === 'string' ? args.formatter.trim() : ''
  const formatter = formatterValue ? ` --formatter ${formatterValue}` : ''
  const containerValue = typeof args.container === 'string' ? args.container.trim() : ''
  const container = containerValue && containerValue !== 'host'
    ? ` --container ${containerValue}`
    : ''
  return `acli${cluster} --timeout ${timeout}${formatter}${container} system ${qfkSystemCommandText(args) || '<命令>'}`
}

function commandPreviewKey(signal: SignalV2, index: number): string {
  return signalStableId(signal, index)
}

function hasCommandPreview(signal: SignalV2, index: number): boolean {
  return Boolean(expandedCommandPreviews.value[commandPreviewKey(signal, index)])
}

async function loadCommandPreview(signal: SignalV2, index: number, force = false): Promise<void> {
  const key = commandPreviewKey(signal, index)
  expandedCommandPreviews.value = { ...expandedCommandPreviews.value, [key]: true }
  if (!force && (commandPreviews.value[key] || commandPreviewErrors.value[key])) return

  commandPreviewLoading.value = { ...commandPreviewLoading.value, [key]: true }
  commandPreviewErrors.value = { ...commandPreviewErrors.value, [key]: undefined }
  try {
    const resp = await fetch('/api/v1/kbd/command-preview', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ signal }),
    })
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) {
      throw new Error(typeof body?.detail === 'string' ? body.detail : `HTTP ${resp.status}`)
    }
    commandPreviews.value = { ...commandPreviews.value, [key]: body as CommandPreview }
  } catch (error) {
    commandPreviewErrors.value = {
      ...commandPreviewErrors.value,
      [key]: error instanceof Error ? error.message : '命令编译预览失败',
    }
  } finally {
    commandPreviewLoading.value = { ...commandPreviewLoading.value, [key]: false }
  }
}

function toggleCommandPreview(signal: SignalV2, index: number): void {
  const key = commandPreviewKey(signal, index)
  if (expandedCommandPreviews.value[key]) {
    expandedCommandPreviews.value = { ...expandedCommandPreviews.value, [key]: false }
    return
  }
  void loadCommandPreview(signal, index)
}

async function copyCommandPreview(signal: SignalV2, index: number): Promise<void> {
  const preview = commandPreviews.value[commandPreviewKey(signal, index)]
  if (!preview?.command) return
  try {
    await navigator.clipboard.writeText(preview.command)
    ElMessage.success('完整命令已复制；请在已授权的 HCI 终端中人工核查')
  } catch {
    ElMessage.error('复制失败，请手动选择命令文本')
  }
}

function clearStagedSignalEdits() {
  stagedSignalEdits.value = {}
}

function discardStagedSignalEdit(signalId: string) {
  if (!Object.prototype.hasOwnProperty.call(stagedSignalEdits.value, signalId)) return
  const next = { ...stagedSignalEdits.value }
  delete next[signalId]
  stagedSignalEdits.value = next
}

const stagedSignalEditCount = computed(() => Object.keys(stagedSignalEdits.value).length)

function hasStagedSignalEdit(signal: SignalV2, index: number): boolean {
  return Object.prototype.hasOwnProperty.call(stagedSignalEdits.value, signalStableId(signal, index))
}

const signalList = computed<SignalV2[]>(() => {
  const source = (detailEntry.value?.signals_json as SignalsDoc | undefined)?.signals || []
  return source.map((signal, index) => stagedSignalEdits.value[signalStableId(signal, index)] || signal)
})
const signalGenerationMetadata = computed<Record<string, any>>(
  () => (detailEntry.value?.signals_json as SignalsDoc | undefined)?.generation_metadata || {},
)
const rejectedSignalCandidates = computed(
  () => (detailEntry.value?.signals_json as SignalsDoc | undefined)?.rejected_candidates || [],
)
const producerSignals = computed(() =>
  signalList.value.map((s, i) => ({ sig: s, origIdx: i })).filter((x) => !isBackendSig(x.sig)),
)
const consumerSignals = computed(() =>
  signalList.value.map((s, i) => ({ sig: s, origIdx: i })).filter((x) => isBackendSig(x.sig)),
)
const shouldSignalCount = computed(() => signalList.value.filter((signal) => signal.role === 'should').length)
const minimumShouldCount = computed<number>({
  get: () => Number((detailEntry.value?.signals_json as SignalsDoc | undefined)?.verification_contract?.evidence_policy?.minimum_should || 0),
  set: (value: number) => {
    if (!detailEntry.value?.signals_json) return
    const doc = detailEntry.value.signals_json as SignalsDoc
    doc.verification_contract ||= { schema_version: 1, evidence_policy: {} }
    doc.verification_contract.evidence_policy ||= {}
    doc.verification_contract.evidence_policy.minimum_should = Math.min(
      Math.max(0, Number(value) || 0),
      shouldSignalCount.value,
    )
  },
})

async function saveMinimumShouldRule() {
  signalSaveLoading.value = true
  try {
    await persistSignalList(signalList.value.map(cloneSignal), '增强证据规则已保存')
  } catch (error) {
    ElMessage.error(error instanceof Error ? `保存失败：${error.message}` : '保存失败，请重试')
  } finally {
    signalSaveLoading.value = false
  }
}

// ── QKV 生产者关键字 × 分类基线 辅助（实例/注释/软校验）───────────────────────
// 分类基线（category_baseline.yaml, 198 类）按标签语义分两性：
//   · 任务失败型故障：有任务记录走 qkv_task；纯弹框走 qkv_dialog（当前主控日志定位 END/REQUEST_ID）
//   · 告警型故障    ：标签以「告警」结尾               → acli alert get -k（qkv_alert）
// keyword 是 acli <type> get -k 的检索词，须与本案例所属分类基线标签语义一致，否则查不到记录、信号恒为假。
function qkvNatureLabel(tool: string): string {
  if (tool === 'qkv_alert') return '（告警型故障 · 分类基线）'
  if (tool === 'qkv_task' || tool === 'qkv_dialog') return '（任务失败型故障 · 分类基线）'
  return ''
}
function qkvKeywordPlaceholder(tool: string): string {
  if (tool === 'qkv_alert') return '告警型关键字，如 虚拟机CPU或内存占用过高告警、序列号过期告警'
  if (tool === 'qkv_task') return '任务失败型关键字，如 虚拟机开机失败、虚拟机快照失败'
  if (tool === 'qkv_dialog') return '任务失败型弹框关键字，如 虚拟机创建失败、磁盘替换失败'
  return '关键字，取自分类基线，如 虚拟机开机失败 / …告警'
}
// 软校验：生产者类型 ↔ 关键字性质 是否疑似不匹配（仅报强信号，避免误报）
function qkvKeywordMismatch(sig: SignalV2): boolean {
  const tool = sigTool(sig)
  const kw = String(sigArgs(sig).keyword || '')
  if (!kw) return false
  const hasAlert = kw.includes('告警')
  const hasFailVerb = /(失败|卡住|不达预期)/.test(kw)
  // 任务型工具却用了告警标签 → 应改 qkv_alert
  if ((tool === 'qkv_task' || tool === 'qkv_dialog') && hasAlert && !hasFailVerb) return true
  // 告警型工具却用了任务失败标签 → 应改 qkv_task/qkv_dialog
  if (tool === 'qkv_alert' && hasFailVerb && !hasAlert) return true
  return false
}

const editingSignalIndex = ref<number | null>(null)
const signalEditDraft = ref<SignalV2>({
  acquire: { tool: '', args: {} },
  match: { type: 'keyword', pattern: '', mode: 'or', expected: true },
  orchestrate: {},
})
const signalSaveLoading = ref(false)
const pipelineConvertLoading = ref(false)

function stageCurrentSignalEdit() {
  if (editingSignalIndex.value === null) return
  const signalId = signalStableId(signalEditDraft.value, editingSignalIndex.value)
  stagedSignalEdits.value = {
    ...stagedSignalEdits.value,
    [signalId]: cloneSignal(signalEditDraft.value),
  }
}

function deriveSignalRequires(sig: SignalV2): string[] {
  const found = new Set<string>()
  const collect = (value: any) => {
    if (typeof value === 'string') {
      for (const match of value.matchAll(/\{\{([A-Z][A-Z0-9_]*)(?:\.[A-Z0-9_]+)*\}\}/g)) found.add(match[1])
    } else if (Array.isArray(value)) {
      value.forEach(collect)
    } else if (value && typeof value === 'object') {
      Object.values(value).forEach(collect)
    }
  }
  collect(sig.acquire?.args || {})
  collect(sig.match?.extract || {})
  for (const produce of sig.orchestrate?.produces || []) collect(produce?.extract || {})
  return [...found].sort()
}

function syncDraftRequires() {
  signalEditDraft.value.orchestrate = signalEditDraft.value.orchestrate || {}
  signalEditDraft.value.orchestrate.requires = deriveSignalRequires(signalEditDraft.value)
}

async function convertDraftPipeline() {
  const draft = signalEditDraft.value
  const command = String(draft.acquire?.args?.command || '')
  if (!command.includes('|')) return
  const outputMode = qfkOutputMode(draft)
  pipelineConvertLoading.value = true
  try {
    const resp = await fetch('/api/v1/kbd/tools/convert-safe-pipeline', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ command }),
    })
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(body?.detail || `HTTP ${resp.status}`)
    const produces = (draft.orchestrate?.produces || []).filter((item: any) => item?.name)
    if (outputMode === 'produces' && produces.length !== 1) {
      throw new Error('安全转换到“产出变量”时，请先只保留一个目标变量；平台不会猜测应写入哪个变量')
    }
    const convertedExtract: Record<string, any> = JSON.parse(JSON.stringify(body.extract || {}))
    const columnKeys = Array.isArray(convertedExtract.columns) ? convertedExtract.columns.map((column: any) => String(column.key || '')).filter(Boolean) : []
    const scalarProduce = outputMode === 'produces' && !['object', 'array<object>'].includes(String(produces[0]?.type || 'string'))
    if (columnKeys.length > 1 && (outputMode === 'keyword' || scalarProduce)) {
      const { value } = await ElMessageBox.prompt(
        `安全转换识别出多列：${columnKeys.join('、')}。请选择唯一主值列，匹配模式或标量变量只会消费该列。`,
        '选择主值列',
        {
          inputValue: columnKeys[0],
          inputPlaceholder: columnKeys.join(' / '),
          inputValidator: (value) => columnKeys.includes(String(value || '').trim()) || `请输入以下列名之一：${columnKeys.join('、')}`,
          confirmButtonText: '继续预览', cancelButtonText: '取消',
        },
      )
      convertedExtract.value_key = value.trim()
    }
    const converted = cloneSignal(draft)
    if (outputMode === 'produces') {
      converted.orchestrate.produces.find((item: any) => item?.name)!.extract = convertedExtract
    } else {
      converted.match.extract = convertedExtract
    }
    converted.acquire.args.command = body.command
    const target = outputMode === 'produces' ? `变量 ${produces[0].name}` : '匹配模式'
    const preview = JSON.stringify({ command: body.command, extract: convertedExtract, conversion_id: body.conversion_id }, null, 2)
    await ElMessageBox.confirm(`将应用安全转换到${target}。\n\n${preview}`, '预览安全转换', {
      confirmButtonText: '确认应用', cancelButtonText: '取消', type: 'warning', distinguishCancelAndClose: true,
    })
    signalEditDraft.value = converted
    syncDraftRequires()
    const removed = Array.isArray(body.removed_segments) && body.removed_segments.length
      ? `；已移除：${body.removed_segments.join('、')}` : ''
    ElMessage.success(`已安全转换到“${target}”的声明式取值${removed}；转换编号：${body.conversion_id || '—'}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '管道转换失败，请人工复核')
  } finally {
    pipelineConvertLoading.value = false
  }
}

function startEditSignal(origIdx: number) {
  if (editingSignalIndex.value !== null && editingSignalIndex.value !== origIdx) {
    stageCurrentSignalEdit()
    ElMessage.info('已暂存当前信号的未保存修改，请继续修复其他信号后统一保存')
  }
  const sig = signalList.value[origIdx]
  if (!sig) return
  editingSignalIndex.value = origIdx
  const draft = cloneSignal(sig)
  // 确保嵌套对象存在，便于 v-model 直接绑定 v2 字段路径
  draft.acquire = draft.acquire || { tool: '', args: {} }
  draft.acquire.args = draft.acquire.args || {}
  // 后端会把历史 container=host 归一为省略字段；进入编辑态时恢复为可见的
  // host 选项，避免空白占位与“无法取消已选容器”的歧义。
  if (sigTool(draft) === 'qfk_system') draft.acquire.args.container ??= 'host'
  // 所有 QKV 都是变量生产者，必须保持 match=null。旧逻辑在进入编辑态时补 keyword
  // matcher，会导致专家只改关键字也无法通过保存契约。
  if (!isBackendSig(draft)) {
    draft.match = null
    if (sigTool(draft) === 'qkv_dialog') {
      draft.acquire.args.paths ??= ['/sf/log/today', '/sf/log/today/vt']
      draft.acquire.args.context_lines ??= 2
      draft.orchestrate = draft.orchestrate || {}
      if (!Array.isArray(draft.orchestrate.produces) || draft.orchestrate.produces.length === 0) {
        draft.orchestrate.produces = [
          { name: 'END', path: 'end' },
          { name: 'REQUEST_ID', path: 'request_id' },
          { name: 'HOST', path: 'host' },
        ]
      }
    }
  }
  draft.orchestrate = draft.orchestrate || {}
  signalEditDraft.value = draft
  if (isBackendSig(draft)) syncDraftRequires()
}

function qfkOutputMode(sig: SignalV2): 'keyword' | 'produces' {
  const produces = sigOrch(sig).produces || []
  // 编辑态刚切到“产出变量”时会先创建一条 name 为空的草稿并把 match 置空。
  // 模式必须由结构状态判断，不能由尚未填写的变量名判断，否则会误切回 keyword
  // 并访问 null match，触发 Vue 运行时异常和全屏遮罩残留。
  return sig.match === null || (Array.isArray(produces) && produces.length > 0) ? 'produces' : 'keyword'
}

function setQfkOutputMode(mode: 'keyword' | 'produces') {
  const draft = signalEditDraft.value
  draft.orchestrate = draft.orchestrate || {}
  if (mode === 'produces') {
    // 输出采集不做匹配判定，命令成功后把 stdout/JSON 路径结果写入变量池。
    draft.match = null
    if (!Array.isArray(draft.orchestrate.produces) || draft.orchestrate.produces.length === 0) {
      draft.orchestrate.produces = [{ name: '', type: 'string', path: '' }]
    }
    return
  }
  // 匹配模式不应残留输出变量，否则服务端会拒绝二义信号。
  draft.orchestrate.produces = []
  draft.match = { type: 'keyword', pattern: '', mode: 'or', expected: true }
}

function cancelEditSignal() {
  if (newSignalId.value && detailEntry.value?.signals_json) {
    detailEntry.value.signals_json.signals = detailEntry.value.signals_json.signals.filter(
      (signal) => String(signal.id) !== newSignalId.value,
    )
    newSignalId.value = null
  }
  if (editingSignalIndex.value !== null && signalEditDraft.value.id) {
    discardStagedSignalEdit(String(signalEditDraft.value.id))
  }
  editingSignalIndex.value = null
  signalEditDraft.value = {
    acquire: { tool: '', args: {} },
    match: { type: 'keyword', pattern: '', mode: 'or', expected: true },
    orchestrate: {},
  }
}

async function persistSignalList(
  list: SignalV2[],
  successMessage: string,
  changeAnnotations: ChangeAnnotation[] = [],
): Promise<boolean> {
  if (!detailEntry.value) return false
  const currentDoc = (detailEntry.value.signals_json || {}) as SignalsDoc
  const payload = reconcileSignalContract({ ...currentDoc, schema_version: 2 }, list)
  const resp = await fetch(kbdEditEndpoint(detailEntry.value), {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', ...authHeader },
    body: JSON.stringify({
      signals_json: payload,
      change_annotations: changeAnnotations,
      lock_version: detailEntry.value.lock_version,
    }),
  })
  const responseBody = await resp.json().catch(() => ({}))
  if (!resp.ok) {
    const detail = typeof responseBody?.detail === 'string'
      ? responseBody.detail
      : responseBody?.detail?.message || `HTTP ${resp.status}`
    throw new Error(detail)
  }
  applyMaintenanceResponse(detailEntry.value, responseBody)
  const newDoc: SignalsDoc = responseBody?.payload?.signals_json || responseBody?.signals_json || payload
  detailEntry.value.signals_json = newDoc
  detailEntry.value.lock_version = responseBody?.lock_version ?? detailEntry.value.lock_version
  const entryIndex = entries.value.findIndex((entry) => entry.id === detailEntry.value!.id)
  if (entryIndex !== -1) entries.value[entryIndex].signals_json = newDoc
  clearStagedSignalEdits()
  newSignalId.value = null
  void fetchRevisionState(detailEntry.value.id)
  await validateCurrentCandidate({ silent: true })
  ElMessage.success(successMessage)
  return true
}

function addSignal(tool: string) {
  if (!detailEntry.value) return
  const doc = (detailEntry.value.signals_json || { schema_version: 2, signals: [] }) as SignalsDoc
  const signal = buildSignalForTool(tool)
  doc.signals = [...(doc.signals || []), signal]
  detailEntry.value.signals_json = doc
  newSignalId.value = String(signal.id)
  startEditSignal(doc.signals.length - 1)
  ElMessage.info('请补全新信号的必填项后保存')
}

const deletingSignalIndex = ref<number | null>(null)
const deleteSignalReason = ref<string>('redundant_signal')
const deleteSignalNote = ref<string>('')
const deleteSignalDialogVisible = ref(false)

function deleteSignal(index: number) {
  if (!detailEntry.value) return
  const signal = signalList.value[index]
  const role = sigRoleLabel(signal)
  const policy = ((detailEntry.value.signals_json as SignalsDoc | undefined)?.verification_contract?.evidence_policy || {})
  const isLastMust = signal.role === 'must' && (policy.must || []).length <= 1
  const thresholdWillChange = signal.role === 'should'
    && Number(policy.minimum_should || 0) > Math.max(0, (policy.should || []).length - 1)
  const impact = [
    `“${role}”将从 Agent 的验证规则中同步移除。`,
    isLastMust ? '删除后可以先保存工作稿，但发布前必须把至少一条可执行信号设为“必要证据”。' : '',
    thresholdWillChange ? '增强证据门槛会自动收敛，避免要求已删除的信号。' : '',
    '原始 KBD 正文和截图证据不会删除。',
  ].filter(Boolean).join('\n\n')
  deletingSignalIndex.value = index
  deleteSignalReason.value = 'redundant_signal'
  deleteSignalNote.value = ''
  deleteSignalDialogVisible.value = true
  ElMessage.info(impact)
}

async function submitDeleteSignal() {
  if (!detailEntry.value || deletingSignalIndex.value === null) return
  const index = deletingSignalIndex.value
  const signal = signalList.value[index]
  if (!signal || !deleteSignalReason.value) {
    ElMessage.warning('请选择删除原因')
    return
  }
  signalSaveLoading.value = true
  try {
    await persistSignalList(
      signalList.value.filter((_, itemIndex) => itemIndex !== index),
      '关键信号已删除',
      [{
        signal_id: signalStableId(signal, index),
        reason_code: deleteSignalReason.value,
        ...(deleteSignalNote.value.trim() ? { note: deleteSignalNote.value.trim() } : {}),
      }],
    )
    editingSignalIndex.value = null
    deleteSignalDialogVisible.value = false
    deletingSignalIndex.value = null
  } catch (error) {
    ElMessage.error(error instanceof Error ? `删除失败：${error.message}` : '删除失败，请重试')
  } finally {
    signalSaveLoading.value = false
  }
}

async function duplicateSignal(index: number) {
  const source = signalList.value[index]
  if (!source) return
  const duplicate = cloneSignal(source)
  duplicate.id = createSignalId()
  duplicate.provenance = { ...(duplicate.provenance || {}), expert_created: true, needs_review: true }
  const list = signalList.value.map(cloneSignal)
  list.splice(index + 1, 0, duplicate)
  signalSaveLoading.value = true
  try {
    await persistSignalList(list, '已复制关键信号')
  } catch (error) {
    ElMessage.error(error instanceof Error ? `复制失败：${error.message}` : '复制失败，请重试')
  } finally {
    signalSaveLoading.value = false
  }
}

async function moveSignal(index: number, direction: -1 | 1) {
  const target = index + direction
  if (target < 0 || target >= signalList.value.length) return
  const list = signalList.value.map(cloneSignal)
  ;[list[index], list[target]] = [list[target], list[index]]
  signalSaveLoading.value = true
  try {
    await persistSignalList(list, '关键信号顺序已更新')
  } catch (error) {
    ElMessage.error(error instanceof Error ? `排序失败：${error.message}` : '排序失败，请重试')
  } finally {
    signalSaveLoading.value = false
  }
}

function restoreRejectedCandidate(candidate: unknown) {
  if (!candidate || typeof candidate !== 'object' || !('acquire' in candidate)) {
    ElMessage.error('该候选不是可编辑的 Signal 结构，无法直接恢复')
    return
  }
  if (!detailEntry.value) return
  const restored = cloneSignal(candidate as SignalV2)
  restored.id = createSignalId()
  restored.provenance = { ...(restored.provenance || {}), needs_review: true, expert_restored: true }
  const doc = detailEntry.value.signals_json as SignalsDoc
  doc.signals = [...(doc.signals || []), restored]
  newSignalId.value = String(restored.id)
  startEditSignal(doc.signals.length - 1)
  ElMessage.info('候选已恢复为编辑草稿，请复核并保存')
}

async function saveSignalEdit() {
  if (editingSignalIndex.value === null || !detailEntry.value) return
  if (isBackendSig(signalEditDraft.value)) {
    const produces = signalEditDraft.value.orchestrate?.produces || []
    const hasProduces = produces.some((item: any) => String(item?.name || '').trim())
    const hasMatch = Boolean(signalEditDraft.value.match)
    if (hasProduces === hasMatch) {
      ElMessage.error('后端信号必须且只能选择“匹配模式”或“产出变量”之一')
      return
    }
    const matcherType = String(signalEditDraft.value.match?.type || '')
    if (hasMatch && ['keyword', 'regex', 'state'].includes(matcherType)
      && !String(signalEditDraft.value.match?.pattern || '').trim()) {
      ElMessage.error('请填写用于判定命令结果的匹配内容')
      return
    }
    if (hasMatch && ['threshold', 'delta', 'trend'].includes(matcherType)
      && signalEditDraft.value.match?.value === undefined) {
      ElMessage.error('请填写数值判定的阈值')
      return
    }
    if (hasMatch && !signalEditDraft.value.match?.extract) {
      ElMessage.error('匹配模式必须先配置声明式取值')
      return
    }
    if (String(signalEditDraft.value.acquire?.args?.command || '').includes('|')) {
      ElMessage.error('执行命令不能保存 Shell 管道，请先点击“安全转换管道”')
      return
    }
    if (hasProduces) {
      for (const item of produces) {
        if (!/^[A-Z][A-Z0-9_]*$/.test(String(item?.name || ''))) {
          ElMessage.error('产出变量名必须为全大写字母、数字或下划线，且不能以数字开头')
          return
        }
        if (!item?.extract || !['text', 'json'].includes(String(item.extract.type))) {
          ElMessage.error(`产出变量 ${item.name} 必须配置新版声明式取值`)
          return
        }
      }
    }
    syncDraftRequires()
  }
  signalSaveLoading.value = true
  try {
    // 暂存当前卡片，以便在同一 KBD 中逐条修复多个旧管道，不丢失已经完成的安全转换。
    stageCurrentSignalEdit()
    const list = signalList.value.map(cloneSignal)
    const pipeSignals = list
      .map((signal, index) => ({ signal, index }))
      .filter(({ signal }) => isBackendSig(signal) && String(signal.acquire?.args?.command || '').includes('|'))
    if (pipeSignals.length) {
      const targets = pipeSignals.map(({ signal, index }) => {
        const id = String(signal.id || `sig_${index + 1}`)
        const instruction = String(signal.acquire?.args?.instruction || sigTool(signal) || '未命名信号')
        return `第 ${index + 1} 条 ${id}（${instruction}）`
      })
      ElMessage.error(`已暂存本条修改；仍有含 Shell 管道的信号：${targets.join('、')}。请点击该卡片的“编辑”逐条修复，再保存。`)
      return
    }
    await persistSignalList(list, '关键信号已保存')
    cancelEditSignal()
  } catch (error) {
    const detail = error instanceof Error ? error.message : ''
    ElMessage.error(detail ? `保存失败：${detail}` : '保存失败，请重试')
  } finally {
    signalSaveLoading.value = false
  }
}

// 跳转到工具管理页（自定义 QKV/QFK 采集器），按采集器名预填搜索
const router = useRouter()
function goToToolManage(acquirer?: string) {
  const q = (acquirer || '').replace(/\./g, '_')
  router.push({ path: '/tools', query: q ? { q } : {} })
}

function resetFilters() {
  categoryFilter.value = ''
  statusFilter.value = ''
  supportIdFilter.value = ''
  titleKeywordFilter.value = ''
  confidenceFilter.value = ''
  activeCategory.value = '__all__'
  catPage.value = 1
  fetchPending()
}

function openEditDialog(entry: KbdEntry) {
  editingEntry.value = entry
  editTitle.value = entry.title
  editContent.value = entry.content_md || ''
  editCategoryId.value = entry.category_id || entry.ai_category_id || ''
  editFullscreen.value = false
  editDialogVisible.value = true
}

async function submitEdit() {
  if (!editingEntry.value) return
  editLoading.value = true
  try {
    const payload: Record<string, unknown> = {}
    if (editTitle.value.trim() && editTitle.value !== editingEntry.value.title) {
      payload.title = editTitle.value.trim()
    }
    if (editContent.value !== editingEntry.value.content_md) {
      payload.content_md = editContent.value
    }
    if (editCategoryId.value !== (editingEntry.value.category_id || editingEntry.value.ai_category_id || '')) {
      payload.category_id = editCategoryId.value
    }
    if (Object.keys(payload).length === 0) {
      ElMessage.info('内容未变更')
      editDialogVisible.value = false
      return
    }
    if (typeof editingEntry.value.lock_version === 'number') {
      payload.lock_version = editingEntry.value.lock_version
    }
    const resp = await fetch(`/api/v1/kbd/${editingEntry.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('保存成功')
    editDialogVisible.value = false
    // 刷新列表：分类变更时需要重新分组
    await fetchPending()
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    editLoading.value = false
  }
}

async function handleRevertToDraft(entry: KbdEntry) {
  try {
    await ElMessageBox.confirm(
      `确认将「${entry.title}」退回待审核状态？`,
      '退回待审核',
      { confirmButtonText: '确认退回', cancelButtonText: '取消', type: 'warning' },
    )
    const resp = await fetch(`/api/v1/kbd/${entry.id}/revert-to-draft`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
    })
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try { const errBody = await resp.json(); if (errBody.detail) detail = typeof errBody.detail === 'string' ? errBody.detail : JSON.stringify(errBody.detail) } catch { /* */ }
      throw new Error(detail)
    }
    ElMessage.success('已退回待审核')
    detailDialogVisible.value = false
    await fetchPending()
  } catch (e: unknown) {
    const msg = (e as { message?: string })?.message || ''
    if (msg === 'cancel') return
    ElMessage.error(msg || '操作失败')
  }
}

async function handleRepublish(entry: KbdEntry) {
  if (!(await ensureReviewerIdentity())) return
  try {
    await ElMessageBox.confirm(
      `确认重新发布此 KBD 条目？\n\n「${entry.title}」\n\n将重新生成 embedding 并发布。`,
      '重新发布',
      { confirmButtonText: '确认发布', cancelButtonText: '取消', type: 'warning' },
    )
    const resp = await fetch(`/api/v1/kbd/${entry.id}/republish`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ reviewer_id: currentUser.value }),
    })
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try {
        const errBody = await resp.json()
        if (errBody.detail) detail = typeof errBody.detail === 'string' ? errBody.detail : JSON.stringify(errBody.detail)
      } catch { /* ignore parse error */ }
      throw new Error(detail)
    }
    ElMessage.success('重新发布成功')
    detailDialogVisible.value = false
    await fetchPending()
  } catch (e: unknown) {
    const msg = (e as { message?: string })?.message || ''
    if (msg === 'cancel') return
    ElMessage.error(msg || '操作失败，请重试')
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Markdown 渲染（基于标准 AST 词法状态机解析与防注入过滤，彻底告别手写正则妥协）
// ──────────────────────────────────────────────────────────────────────────────
function renderMarkdown(md: string): string {
  if (!md) return ''
  // 使用完备的 AST 语法分析器 marked 解析 Markdown 为标准 HTML
  // 并使用 DOMPurify 彻底进行过滤防御 XSS 攻击
  return DOMPurify.sanitize(marked.parse(md) as string)
}

// ──────────────────────────────────────────────────────────────────────────────
// 截图说明解析（accordion 卡片）
// ──────────────────────────────────────────────────────────────────────────────

/** 将截图类型字符串映射为 ScreenshotTypeInfo（v2 格式，后端已判断） */
function typeNameToInfo(typeName: string): ScreenshotTypeInfo {
  if (/告警截图/.test(typeName)) return { label: '告警截图', color: '#E6A23C', bgColor: '#FEF7EC', icon: '🔔' }
  if (/任务截图/.test(typeName)) return { label: '任务截图', color: '#409EFF', bgColor: '#EEF6FF', icon: '📋' }
  if (/弹框截图/.test(typeName)) return { label: '弹框截图', color: '#F56C6C', bgColor: '#FEF0F0', icon: '💬' }
  if (/配置截图/.test(typeName)) return { label: '配置截图', color: '#00A6A6', bgColor: '#E8F8F8', icon: '⚙️' }
  if (/终端截图/.test(typeName)) return { label: '终端截图', color: '#722ED1', bgColor: '#F5EEFF', icon: '💻' }
  if (/日志截图/.test(typeName)) return { label: '日志截图', color: '#67C23A', bgColor: '#F0F9EB', icon: '📄' }
  return { label: '其他截图', color: '#909399', bgColor: '#F5F7FA', icon: '🖼️' }
}

/**
 * v1 兼容：基于内容关键词推断截图类型（旧格式 0-4 字段，TYPE 字段不存在时使用）
 */
function detectScreenshotType(fields: ScreenshotFields): ScreenshotTypeInfo {
  const isBlack = /黑/.test(fields.bgColorText)
  const isWhite = /白/.test(fields.bgColorText)
  const fullText = [...fields.visibleContent, ...fields.errorContent].join(' ')

  if (isWhite && /紧急|普通|历史告警数|未处理/.test(fullText))
    return { label: '告警截图', color: '#E6A23C', bgColor: '#FEF7EC', icon: '🔔' }
  if (isWhite && (/操作人|对象类型|行为|开始时间|结束时间/.test(fullText) || /完成|失败|进行中/.test(fullText)))
    return { label: '任务截图', color: '#409EFF', bgColor: '#EEF6FF', icon: '📋' }
  if (isBlack && /\$\s|#\s|sudo|chmod|\/var\/|\/etc\//.test(fullText))
    return { label: '终端截图', color: '#722ED1', bgColor: '#F5EEFF', icon: '💻' }
  if (isBlack && (/日志/.test(fields.typeName) || /\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/.test(fullText)))
    return { label: '日志截图', color: '#67C23A', bgColor: '#F0F9EB', icon: '📄' }
  return { label: '其他截图', color: '#909399', bgColor: '#F5F7FA', icon: '🖼️' }
}

/** 根据截图类型决定字段2的标签名 */
function getErrorLabelByType(screenshotType: string): string {
  if (/终端截图/.test(screenshotType)) return '命令返回'
  if (/日志截图/.test(screenshotType)) return '错误日志'
  if (/告警截图/.test(screenshotType)) return '重要告警'
  if (/任务截图/.test(screenshotType)) return '失败任务'
  if (/弹框截图/.test(screenshotType)) return '弹框信息'
  if (/配置截图/.test(screenshotType)) return '配置状态'
  return '相关信息'
}

/** v1 兼容：根据内容关键词推断字段2标签名 */
function detectErrorLabel(items: string[]): string {
  const joined = items.join(' ')
  if (/告警/.test(joined)) return '告警信息'
  if (/红框|标注/.test(joined)) return '红框标注'
  if (/失败|任务/.test(joined)) return '失败任务'
  return '报错日志'
}

/** 最大展示行数（可见内容截断） */
const MAX_VISIBLE_LINES = 10

/**
 * v2 格式截图块解析器（PaddleOCR + LLM 双引擎新格式）。
 * 识别 BACKGROUND/TYPE/FULL_TEXT/KEY/TIPS 关键词 section。
 * 根据截图类型决定 FULL_TEXT 截断方向：
 *   终端/日志截图 → 取后 N 行（最新输出在末尾）
 *   告警/任务/其他截图 → 取前 N 行
 */
function parseScreenshotBlockV2(lines: string[]): ScreenshotSegment {
  let section = ''
  let background = '其他'
  let screenshotType = '其他截图'
  const fullText: string[] = []
  const key: string[] = []
  const tips: string[] = []
  const descriptionLines: string[] = []

  for (const line of lines) {
    // 剥离 "> " 前缀（v2 格式每行都以 "> " 开头，支持前面有缩进空格）
    const stripped = line.replace(/^\s*>\s*/, '').trim()
    if (!stripped) continue

    if (/^BACKGROUND:\s*/.test(stripped)) {
      background = stripped.replace(/^BACKGROUND:\s*/, '').trim()
    } else if (/^TYPE:\s*/.test(stripped)) {
      screenshotType = stripped.replace(/^TYPE:\s*/, '').trim()
    } else if (/^FULL_TEXT:$/.test(stripped)) {
      section = 'full'
    } else if (/^KEY:$/.test(stripped)) {
      section = 'key'
    } else if (/^TIPS:$/.test(stripped)) {
      section = 'tips'
    } else if (/^DESCRIPTION:$/.test(stripped)) {
      section = 'description'
    } else if (/^-\s/.test(stripped)) {
      const item = stripped.slice(2).trim()
      // 跳过占位符
      if (item === '无' || item === '（无文字）') continue
      if (section === 'full') fullText.push(item)
      else if (section === 'key') key.push(item)
      else if (section === 'tips') tips.push(item)
    } else if (section === 'description') {
      // DESCRIPTION 是纯文字段落（非 - 格式），跳过占位符
      if (stripped !== '（无描述）' && stripped !== '(无描述)') {
        descriptionLines.push(stripped)
      }
    }
  }

  // 根据 TYPE 决定 FULL_TEXT 截断方向
  const isEndFirst = /终端截图|日志截图/.test(screenshotType)
  const visibleContent = isEndFirst
    ? fullText.slice(-MAX_VISIBLE_LINES)   // 后 N 行（最新内容）
    : fullText.slice(0, MAX_VISIBLE_LINES)  // 前 N 行（最新内容在最前）

  const typeInfo = typeNameToInfo(screenshotType)
  const errorLabel = getErrorLabelByType(screenshotType)

  const fields: ScreenshotFields = {
    background,
    screenshotType,
    fullText,
    visibleContent,
    key,
    tips,
    description: descriptionLines.join('\n'),
    // v1 兼容字段（v2 条目不使用，填充 v2 值兼容旧模板引用）
    intro: '',
    bgColorText: background,
    typeName: screenshotType,
    errorContent: key,
    techTips: tips,
  }
  return { type: 'screenshot', typeInfo, errorLabel, fields, expanded: false }
}

/** 将截图说明行组解析为 ScreenshotSegment（自动检测 v1/v2 格式） */
function parseScreenshotBlock(lines: string[]): ScreenshotSegment {
  // 检测格式版本：v2 格式的行以 "> BACKGROUND:" 或 "> TYPE:" 等开头，支持前面有缩进空格
  const isV2 = lines.some(l => /^\s*>\s*(BACKGROUND|TYPE|FULL_TEXT|KEY|TIPS|DESCRIPTION):/.test(l))
  if (isV2) return parseScreenshotBlockV2(lines)

  // ── v1 兼容解析（旧格式 0-4 字段）──────────────────────────────────────────
  // 第一行: > **【截图说明】**：[可能直接是字段0内容]，支持前面有缩进空格
  const introLine = lines[0] || ''
  const introRaw = introLine.replace(/^\s*>\s*\*\*【截图说明】\*\*[：:]\s*/, '').trim()

  // 字段0 可能直接嵌在 intro 行（converter 将 desc.txt 首行拼在 "【截图说明】：" 后面）
  let bgColorText = ''
  let intro = introRaw
  const field0Match = introRaw.match(/^0[.、]\s*\*?\*?截图背景颜色\*?\*?[：:]\s*(.+)/)
  if (field0Match) {
    bgColorText = field0Match[1].replace(/\*\*/g, '').trim()
    intro = '' // 字段0已解析，不作为 intro 展示
  }

  let typeName = ''
  const visibleContent: string[] = []
  const errorContent: string[] = []
  const techTips: string[] = []
  // -1=intro之前 0=背景色 1=类型 2=可见内容 3=报错 4=技术细节
  let currentField = -1

  for (const line of lines.slice(1)) {
    const trimmed = line.trim()
    if (!trimmed) continue

    // 字段0：截图背景颜色（Vision 新增字段）
    if (/^0[.、]\s*\*?\*?截图背景颜色\*?\*?[：:]/.test(trimmed)) {
      bgColorText = trimmed.replace(/^0[.、]\s*\*?\*?截图背景颜色\*?\*?[：:]\s*/, '').replace(/\*\*/g, '').trim()
      currentField = 0
    // 字段1：界面类型
    } else if (/^1[.、]\s*\*\*截图界面类型\*\*[：:]/.test(trimmed)) {
      typeName = trimmed.replace(/^1[.、]\s*\*\*截图界面类型\*\*[：:]\s*/, '').replace(/\*\*/g, '').trim()
      currentField = 1
    } else if (/^2[.、]\s*\*\*截图中所有可见/.test(trimmed)) {
      currentField = 2
    } else if (/^3[.、]\s*\*\*截图中的报错/.test(trimmed)) {
      currentField = 3
      // 提取内联内容：如 "3. **...**：无" 中的 "无"
      const inline3 = trimmed.replace(/^3[.、]\s*\*\*[^*]+\*\*[：:]\s*/, '').trim()
      if (inline3) errorContent.push(inline3)
    } else if (/^4[.、]\s*\*\*对故障排查/.test(trimmed)) {
      currentField = 4
      // 提取内联内容：如 "4. **...**：无" 中的 "无"
      const inline4 = trimmed.replace(/^4[.、]\s*\*\*[^*]+\*\*[：:]\s*/, '').trim()
      if (inline4) techTips.push(inline4)
    } else if (/^-\s/.test(trimmed)) {
      // 子项 bullet
      const item = trimmed.slice(2).trim()
      if (currentField === 2) visibleContent.push(item)
      else if (currentField === 3) errorContent.push(item)
      else if (currentField === 4) techTips.push(item)
    } else if (currentField > 0 && !/^\d+[.、]/.test(trimmed)) {
      // 字段内的连续文本
      if (currentField === 2) visibleContent.push(trimmed)
      else if (currentField === 3) errorContent.push(trimmed)
      else if (currentField === 4) techTips.push(trimmed)
    }
  }

  const fields: ScreenshotFields = {
    // v1 字段
    intro, bgColorText, typeName, visibleContent, errorContent, techTips,
    // v2 字段（v1 格式时从旧字段映射）
    background: bgColorText,
    screenshotType: typeName,
    fullText: visibleContent,
    key: errorContent,
    tips: techTips,
    description: '',
  }
  const typeInfo = detectScreenshotType(fields)
  return {
    type: 'screenshot',
    typeInfo,
    errorLabel: detectErrorLabel(errorContent),
    fields,
    expanded: false,
  }
}

/**
 * 将 content_md 分割为普通文本段和截图说明段。
 * 截图段以 "> **【截图说明】**" 开头，包含后续编号字段（1-4）和缩进子项。
 * v3 格式：DESCRIPTION 后的纯文字段落也属于截图块（非 bullet 格式）。
 */
function parseContentMd(md: string): ContentSegment[] {
  if (!md) return []
  const lines = md.split('\n')
  const segments: ContentSegment[] = []
  let normalLines: string[] = []
  let screenshotLines: string[] = []
  let inScreenshot = false
  let inDescription = false  // v3: DESCRIPTION section 状态跟踪

  const flushNormal = () => {
    if (normalLines.length > 0) {
      const html = renderMarkdown(normalLines.join('\n'))
      if (html.trim()) segments.push({ type: 'normal', html })
      normalLines = []
    }
  }
  const flushScreenshot = () => {
    if (screenshotLines.length > 0) {
      segments.push(parseScreenshotBlock(screenshotLines))
      screenshotLines = []
    }
  }

  for (const line of lines) {
    const isScreenshotStart = line.trim().startsWith('>') && line.includes('【截图说明】')

    if (isScreenshotStart) {
      flushNormal()
      flushScreenshot()
      inScreenshot = true
      inDescription = false
      screenshotLines = [line]
      continue
    }

    if (inScreenshot) {
      const trimmed = line.trim()
      const isBlank = trimmed === ''

      // 检测截图块结束：只要是一行非空且不以 '>' 开头（且不是下一个 section 的标题 ##），
      // 那么它必然是普通排障正文，代表截图块已经结束，支持前面有缩进空格
      const isEndLine = !isBlank && !line.trim().startsWith('>') && !trimmed.startsWith('##')

      if (trimmed.startsWith('## ') || isEndLine && screenshotLines.length > 1) {
        flushScreenshot()
        inScreenshot = false
        inDescription = false
        normalLines.push(line)
      } else {
        screenshotLines.push(line)

        // 顺便更新 DESCRIPTION 状态跟踪
        if (/^>\s*DESCRIPTION:$/.test(trimmed) || /^DESCRIPTION:$/.test(trimmed)) {
          inDescription = true
        }
        if (inDescription && /^>\s*(BACKGROUND|TYPE|FULL_TEXT|KEY|TIPS):/.test(trimmed)) {
          inDescription = false
        }
      }
    } else {
      normalLines.push(line)
    }
  }
  flushNormal()
  flushScreenshot()
  return segments
}

/** 匹配并关联文档段落中的截图与 images_json 权威 Evidence。 */
function associateSegmentsWithSeq(segments: ContentSegment[], images: ParsedImageJson[]) {
  // fallback 仍按出现顺序分配，但精确内容匹配允许同一图片在不同章节重复引用。
  // 不能把 matchedIndices 应用于精确匹配，否则 KBD27123 这类重复 img_0 会被错配到 img_1。
  const matchedIndices = new Set<number>()
  segments.forEach(seg => {
    if (seg.type === 'screenshot') {
      // 优先匹配内容（DESCRIPTION / visibleContent）
      let matchIdx = images.findIndex((img) => {
        // 比较 DESCRIPTION
        if (seg.fields.description && img.description && img.description !== '（无描述）' && img.description !== '(无描述)') {
          return seg.fields.description === img.description
        }

        // 比较可见内容文字
        if (seg.fields.visibleContent && seg.fields.visibleContent.length && img.visibleContent && img.visibleContent.length) {
          return JSON.stringify(seg.fields.visibleContent) === JSON.stringify(img.visibleContent)
        }

        return false
      })

      // 兜底：若未匹配成功，分配第一个尚未被匹配的图片
      if (matchIdx === -1) {
        matchIdx = images.findIndex((_, idx) => !matchedIndices.has(idx))
      }

      if (matchIdx !== -1) {
        matchedIndices.add(matchIdx)
        seg.seq = images[matchIdx].seq
        seg.evidence = images[matchIdx]
      }
    }
  })
}

function hasImageDescription(image: ParsedImageJson): boolean {
  const description = image.description.trim()
  return Boolean(description && description !== '（无描述）' && description !== '(无描述)')
}

function isExpertConfirmed(image: ParsedImageJson): boolean {
  return image.inferenceStatus === 'expert_confirmed' && !image.inferenceNeedsReview
}

function inferenceStatusLabel(image: ParsedImageJson): string {
  if (isExpertConfirmed(image)) return '专家已确认'
  if (image.inferenceStatus === 'needs_review') return '需专家复核'
  if (image.inferenceStatus === 'not_present') return '无语义推断'
  return '模型推断 · 待确认'
}

// ──────────────────────────────────────────────────────────────────────────────
// 内联编辑（详情弹窗内直接修改 content_md）
// ──────────────────────────────────────────────────────────────────────────────
function startInlineEdit() {
  inlineContent.value = detailEntry.value?.content_md || ''
  editingContent.value = true
}

function cancelInlineEdit() {
  editingContent.value = false
}

async function saveInlineEdit() {
  if (!detailEntry.value) return
  const newContent = inlineContent.value
  if (newContent === detailEntry.value.content_md) {
    editingContent.value = false
    return
  }
  inlineEditLoading.value = true
  try {
    const resp = await fetch(kbdEditEndpoint(detailEntry.value), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ content_md: newContent, lock_version: detailEntry.value.lock_version }),
    })
    const responseBody = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(typeof responseBody?.detail === 'string' ? responseBody.detail : `HTTP ${resp.status}`)
    // 同步更新本地状态
    applyMaintenanceResponse(detailEntry.value, responseBody)
    detailEntry.value.content_md = responseBody.payload?.content_md ?? newContent
    detailEntry.value.lock_version = responseBody?.lock_version ?? detailEntry.value.lock_version
    void fetchRevisionState(detailEntry.value.id)
    void validateCurrentCandidate({ silent: true })
    const idx = entries.value.findIndex((e) => e.id === detailEntry.value!.id)
    if (idx !== -1) entries.value[idx].content_md = newContent
    // 重新解析内容预览
    parsedSegments.value = parseContentMd(newContent)
    associateSegmentsWithSeq(parsedSegments.value, parsedImagesJson.value)
    editingContent.value = false
    ElMessage.success('内容已保存')
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    inlineEditLoading.value = false
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// 展示辅助
// ──────────────────────────────────────────────────────────────────────────────
function confidenceColor(v: number | null): string {
  if (v === null || v === undefined) return '#909399'
  if (v >= 0.8) return '#67c23a'
  if (v >= 0.5) return '#e6a23c'
  return '#f56c6c'
}

function confidenceLabel(v: number | null): string {
  if (v === null || v === undefined) return '—'
  return `${(v * 100).toFixed(0)}%`
}

function formatDate(d: string | null): string {
  if (!d) return '—'
  return new Date(d).toLocaleString('zh-CN')
}

function metaLabel(key: keyof KbdMetadata): string {
  const map: Record<string, string> = {
    sangfor_main_module: '主模块',
    sangfor_sub_module: '子模块',
    suite_version: '套件版本',
    sangfor_updated_at: '官方更新时间',
    sangfor_created_at: '官方创建时间',
    create_admin_id: '创建工程师 ID',
    update_admin_id: '更新工程师 ID',
  }
  return map[key] || key
}

// 解析 images_json 图片描述
function parseImagesJson(images: ImageJsonItem[]): ParsedImageJson[] {
  return images.map((img) => {
    const desc = img.desc || ''

    // 解析 BACKGROUND
    const bgMatch = desc.match(/BACKGROUND:\s*(\S+)/)
    const background = bgMatch ? bgMatch[1] : '其他'

    // 解析 TYPE
    const typeMatch = desc.match(/TYPE:\s*(.+)/)
    const typeName = typeMatch ? typeMatch[1].trim() : '其他截图'
    const typeInfo = typeNameToInfo(typeName)

    // 解析 FULL_TEXT
    const fullTextMatch = desc.match(/FULL_TEXT:\s*\n((?:^-\s.+\n?)+)/m)
    let fullText: string[] = []
    if (fullTextMatch) {
      fullText = fullTextMatch[1]
        .split('\n')
        .map((line) => line.replace(/^-\s*/, '').trim())
        .filter((line) => line && line !== '（无文字）')
    }

    // 可见内容：根据截图类型决定截断方向
    const maxVisible = 12
    const isEndFirst = /终端截图|日志截图/.test(typeName)
    const visibleContent = isEndFirst
      ? fullText.slice(-maxVisible)
      : fullText.slice(0, maxVisible)

    // 解析 DESCRIPTION
    const descMatch = desc.match(/DESCRIPTION:\s*\n(.+?)(?=\n[A-Z_]+:|$)/s)
    const description = descMatch ? descMatch[1].trim() : ''
    const evidence = (img.evidence || {}) as Record<string, any>
    const regions = Array.isArray(evidence.regions) ? evidence.regions : []
    const observedFacts = regions.flatMap((region: Record<string, any>) =>
      Array.isArray(region.observed_facts) ? region.observed_facts.map((item: unknown) => String(item)) : [],
    )
    const inferences = regions.flatMap((region: Record<string, any>) =>
      Array.isArray(region.inferences) ? region.inferences.map((item: unknown) => String(item)) : [],
    )
    const quality = (evidence.quality || {}) as Record<string, any>

    const inferenceNeedsReview = typeof quality.inference_needs_review === 'boolean'
      ? quality.inference_needs_review
      : inferences.length > 0

    return {
      seq: img.seq,
      section: img.section,
      background,
      typeInfo,
      fullText,
      visibleContent,
      description: description || '（无描述）',
      contextBefore: String(img.context_before || evidence.context_before || ''),
      contextAfter: String(img.context_after || evidence.context_after || ''),
      observedFacts,
      inferences,
      qualityStatus: String(quality.status || (desc ? 'legacy' : 'failed')),
      needsReview: Boolean(quality.needs_review || !img.evidence),
      inferenceStatus: String(quality.inference_status || (inferences.length ? 'legacy_unverified' : 'not_present')),
      inferenceNeedsReview,
      inferenceIssues: Array.isArray(quality.inference_issues)
        ? quality.inference_issues.map((item: unknown) => String(item))
        : [],
      provenance: (evidence.provenance || {}) as Record<string, any>,
      expanded: false,
    }
  }).sort((a, b) => a.seq - b.seq)
}

function listToTextarea(items: string[]): string {
  return items.join('\n')
}

function textareaToList(value: string): string[] {
  return value.split('\n').map((item) => item.trim()).filter(Boolean)
}

function startEditImage(image: ParsedImageJson) {
  editingImageSeq.value = image.seq
  imageEditDraft.value = {
    seq: image.seq,
    section: image.section,
    background: image.background,
    screenshotType: image.typeInfo.label,
    fullText: listToTextarea(image.fullText),
    description: image.description === '（无描述）' ? '' : image.description,
    observedFacts: listToTextarea(image.observedFacts),
    inferences: listToTextarea(image.inferences),
  }
  image.expanded = true
}

function cancelEditImage() {
  editingImageSeq.value = null
  imageEditDraft.value = null
}

function formatExpertImageDesc(draft: ImageEditDraft): string {
  const lines = textareaToList(draft.fullText)
  return [
    `BACKGROUND: ${draft.background || '其他'}`,
    `TYPE: ${draft.screenshotType || '其他截图'}`,
    'FULL_TEXT:',
    ...(lines.length ? lines.map((line) => `- ${line}`) : ['- （无文字）']),
    'DESCRIPTION:',
    draft.description.trim() || '（无描述）',
  ].join('\n')
}

async function saveImageEdit() {
  if (!detailEntry.value || !imageEditDraft.value) return
  const draft = imageEditDraft.value
  const images = (detailEntry.value.images_json || []).map((item) => cloneSignal(item as any) as any as ImageJsonItem)
  const index = images.findIndex((item) => item.seq === draft.seq)
  if (index < 0) return
  const evidence = (images[index].evidence && typeof images[index].evidence === 'object')
    ? cloneSignal(images[index].evidence as any) as any
    : {}
  const regions = Array.isArray(evidence.regions) ? evidence.regions : []
  const primaryRegion = regions[0] && typeof regions[0] === 'object' ? { ...regions[0] } : {}
  primaryRegion.observed_facts = textareaToList(draft.observedFacts)
  primaryRegion.inferences = textareaToList(draft.inferences)
  evidence.regions = [primaryRegion, ...regions.slice(1)]
  evidence.quality = {
    ...(evidence.quality || {}),
    status: 'manual_reviewed',
    needs_review: false,
    inference_status: 'expert_confirmed',
    inference_needs_review: false,
  }
  images[index] = {
    ...images[index],
    section: draft.section,
    desc: formatExpertImageDesc(draft),
    evidence,
  }
  imageSaveLoading.value = true
  try {
    const resp = await fetch(kbdEditEndpoint(detailEntry.value), {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        images_json: images,
        reviewed_image_seqs: [draft.seq],
        lock_version: detailEntry.value.lock_version,
      }),
    })
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(typeof body.detail === 'string' ? body.detail : body.detail?.message || `HTTP ${resp.status}`)
    applyMaintenanceResponse(detailEntry.value, body)
    detailEntry.value.images_json = body.payload?.images_json || body.images_json || images
    detailEntry.value.content_md = body.payload?.content_md ?? body.content_md ?? detailEntry.value.content_md
    detailEntry.value.lock_version = body.lock_version ?? detailEntry.value.lock_version
    parsedImagesJson.value = parseImagesJson(detailEntry.value.images_json)
    parsedSegments.value = parseContentMd(detailEntry.value.content_md || '')
    associateSegmentsWithSeq(parsedSegments.value, parsedImagesJson.value)
    cancelEditImage()
    void fetchRevisionState(detailEntry.value.id)
    void validateCurrentCandidate({ silent: true })
    ElMessage.success('截图 Evidence 已按专家确认保存；既有关键信号已标记为过期，请重新抽取并复核')
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '截图修订保存失败')
  } finally {
    imageSaveLoading.value = false
  }
}

const metaKeys: (keyof KbdMetadata)[] = [
  'sangfor_main_module', 'sangfor_sub_module', 'suite_version',
  'sangfor_updated_at', 'sangfor_created_at',
  'create_admin_id', 'update_admin_id',
]

onMounted(() => {
  fetchPending()
  fetchCategories()
  fetchCapabilities()
})
</script>

<template>
  <div class="kbd-review">
    <!-- 页面头部 -->
    <div class="page-header">
      <h2 class="page-title">KBD知识条目管理</h2>
      <p class="page-desc">
        以下是从深信服技术支持门户抓取、经 AI 分类的知识条目，请逐条核实内容与分类后决定是否发布。
      </p>
    </div>

    <!-- 过滤栏 -->
    <el-card class="filter-card" shadow="never">
      <el-row :gutter="16" align="middle">
        <el-col :span="4">
          <el-input
            v-model="supportIdFilter"
            placeholder="按案例 ID 精准搜索"
            clearable
            @clear="fetchPending"
            @keyup.enter="fetchPending"
          />
        </el-col>
        <el-col :span="5">
          <el-input
            v-model="titleKeywordFilter"
            placeholder="按标题关键字搜索"
            clearable
            @clear="fetchPending"
            @keyup.enter="fetchPending"
          />
        </el-col>
        <el-col :span="3">
          <el-select
            v-model="categoryFilter"
            filterable
            allow-create
            clearable
            placeholder="分类筛选"
            style="width: 100%"
            :loading="categoriesLoading"
            @change="fetchPending"
          >
            <el-option
              v-for="cat in categoryOptions"
              :key="cat.code"
              :value="cat.code"
              :label="`${cat.code}  ${cat.name}`"
            >
              <span style="font-family:monospace;color:#606266;font-size:12px">{{ cat.code }}</span>
              <span style="margin-left:8px;color:#909399;font-size:12px">{{ cat.name }}</span>
            </el-option>
          </el-select>
        </el-col>
        <el-col :span="3">
          <el-select v-model="confidenceFilter" clearable placeholder="置信度" style="width: 100%" @change="fetchPending">
            <el-option label="高 ≥0.8" value="0.8," />
            <el-option label="中 0.5-0.8" value="0.5,0.8" />
            <el-option label="低 <0.5" value=",0.5" />
          </el-select>
        </el-col>
        <el-col :span="3">
          <el-select v-model="statusFilter" @change="fetchPending" style="width: 100%" placeholder="全部">
            <el-option label="全部" value="" />
            <el-option label="待审核" value="draft" />
            <el-option label="已发布" value="published" />
            <el-option label="已拒绝" value="rejected" />
            <el-option label="已归档" value="archived" />
          </el-select>
        </el-col>
        <el-col :span="2">
          <div class="filter-btn-group">
            <el-button type="primary" @click="fetchPending">搜索</el-button>
            <el-button @click="resetFilters">重置</el-button>
          </div>
        </el-col>
        <el-col :span="4" class="total-info">
          <span>共 <strong>{{ total }}</strong> 条</span>
        </el-col>
      </el-row>
    </el-card>

    <!-- 列表：按 AI 分类 Tab 分组 -->
    <el-card v-loading="loading" shadow="never" class="table-card">
      <div class="category-nav">
        <el-select
          v-model="activeCategory"
          filterable
          placeholder="选择分类"
          style="width: 360px"
          @change="onTabChange"
        >
          <el-option label="全部" value="__all__">
            <span>全部</span>
            <el-tag size="small" type="info" style="margin-left:8px">{{ categoryStats.reduce((s, c) => s + c.count, 0) }}</el-tag>
          </el-option>
          <el-option
            v-for="stat in categoryStats"
            :key="stat.category_id"
            :label="stat.category_label"
            :value="stat.category_id"
          >
            <span>{{ stat.category_label }}</span>
            <el-tag size="small" type="info" style="margin-left:8px">{{ stat.count }}</el-tag>
          </el-option>
        </el-select>
      </div>
      <el-table :data="entries" row-key="id" style="width: 100%" size="small" @sort-change="handleSortChange">
        <!-- 案例 ID -->
        <el-table-column label="案例 ID" width="100" prop="support_id" sortable="custom">
          <template #default="{ row }">
            <a :href="makeSupportUrl(row.support_id)" target="_blank" rel="noopener noreferrer" class="support-link">
              {{ row.support_id }}
            </a>
          </template>
        </el-table-column>

        <!-- 标题 -->
        <el-table-column label="标题" min-width="280">
          <template #default="{ row }">
            <span class="entry-title">{{ row.title }}</span>
          </template>
        </el-table-column>

        <!-- AI 分类 -->
        <el-table-column label="AI 分类" width="200" prop="ai_category_id" sortable="custom">
          <template #default="{ row }">
            <span class="category-tag">{{ row.ai_category_label || row.ai_category_id || '—' }}</span>
          </template>
        </el-table-column>

        <!-- 置信度 -->
        <el-table-column label="置信度" width="90" align="center" prop="ai_category_conf" sortable="custom">
          <template #default="{ row }">
            <span
              :style="{ color: confidenceColor(row.ai_category_conf), fontWeight: 'bold' }"
            >
              {{ confidenceLabel(row.ai_category_conf) }}
            </span>
            <el-tag
              v-if="row.ai_category_conf !== null && row.ai_category_conf < 0.5"
              type="warning" size="small" style="margin-left: 4px"
            >低</el-tag>
          </template>
        </el-table-column>

        <!-- 状态 -->
        <el-table-column label="状态" width="80" align="center" prop="status" sortable="custom">
          <template #default="{ row }">
            <el-tag
              :type="row.status === 'published' ? 'success' :
                     row.status === 'rejected'  ? 'danger'  :
                     row.status === 'archived'  ? 'info'    : 'warning'"
              size="small"
            >{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>

        <!-- 导入时间 -->
        <el-table-column label="导入时间" width="140" prop="updated_at" sortable="custom">
          <template #default="{ row }">{{ formatDate(row.updated_at) }}</template>
        </el-table-column>

        <!-- 操作 -->
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <div class="action-btn-group">
            <el-button type="info" size="small" text @click="openDetailDialog(row)">详情</el-button>
            <el-button type="primary" size="small" text @click="openEditDialog(row)">编辑</el-button>
            <template v-if="row.status === 'draft'">
              <el-button type="success" size="small" text @click="handleApprove(row)">通过</el-button>
              <el-button type="danger" size="small" text @click="openRejectDialog(row)">拒绝</el-button>
            </template>
            <template v-else-if="row.status === 'rejected'">
              <el-button type="warning" size="small" text @click="handleRepublish(row)">重新发布</el-button>
              <el-button type="info" size="small" text @click="handleRevertToDraft(row)">退回草稿</el-button>
            </template>
            <template v-else>
              <el-button type="info" size="small" text @click="handleRevertToDraft(row)">退回草稿</el-button>
            </template>
            </div>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-wrapper">
        <el-pagination
          background
          layout="total, sizes, prev, pager, next"
          :total="total"
          :page-sizes="[20, 30, 40, 50, 60, 70, 80, 90, 100]"
          :page-size="catPageSize"
          :current-page="catPage"
          @current-change="handleCatPageChange"
          @size-change="(size: number) => { catPageSize = size; catPage = 1; fetchPending() }"
        />
      </div>
    </el-card>

    <!-- 详情弹窗 -->
    <el-dialog
      v-model="detailDialogVisible"
      width="90%"
      class="premium-dialog"
      :fullscreen="detailFullscreen"
      draggable
      align-center
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="custom-dialog-header" style="display: flex; justify-content: space-between; align-items: center; width: 100%; padding-right: 32px; box-sizing: border-box;">
          <span class="el-dialog__title">KBD 条目详情</span>
          <el-button
            type="info"
            text
            circle
            :icon="FullScreen"
            class="fullscreen-toggle-btn"
            @click="detailFullscreen = !detailFullscreen"
            title="切换全屏"
          />
        </div>
      </template>
      <template v-if="detailEntry">
        <el-alert
          v-if="detailEntry.status === 'published'"
          type="info"
          :closable="false"
          show-icon
          title="当前生效版继续由 Agent 使用；已发布维护将通过独立工作稿完成，普通保存不会再静默热发布。"
          style="margin-bottom: 12px"
        />
        <!-- 基本信息 -->
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="案例 ID">
            <a :href="makeSupportUrl(detailEntry.support_id)" target="_blank" rel="noopener noreferrer" class="support-link">
              {{ detailEntry.support_id }}
              <el-icon style="font-size: 11px; margin-left: 3px"><Link /></el-icon>
            </a>
          </el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag
              :type="detailEntry.status === 'published' ? 'success' :
                     detailEntry.status === 'rejected'  ? 'danger'  :
                     detailEntry.status === 'archived'  ? 'info'    : 'warning'"
              size="small"
            >{{ detailEntry.status }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="标题" :span="2">
            <strong>{{ detailEntry.title }}</strong>
          </el-descriptions-item>

          <!-- AI 分类 -->
          <el-descriptions-item label="AI 建议分类">
            <el-tag v-if="detailEntry.ai_category_id" type="primary" size="small">
              {{ detailEntry.ai_category_label || detailEntry.ai_category_id }}
            </el-tag>
            <span v-else class="text-muted">—</span>
            <span
              v-if="detailEntry.ai_category_conf !== null"
              :style="{ marginLeft: '8px', color: confidenceColor(detailEntry.ai_category_conf) }"
            >{{ confidenceLabel(detailEntry.ai_category_conf) }}</span>
            <el-button
              v-if="detailEntry.status !== 'published'"
              type="warning"
              size="small"
              style="margin-left: 8px;"
              :loading="reclassifyLoading === detailEntry.id"
              :disabled="!canEditCurrent"
              @click="handleReclassify(detailEntry)"
              title="重新分类"
            >
              <el-icon style="font-size: 14px;"><Refresh /></el-icon>
              重新分类
            </el-button>
            <el-tag
              v-if="detailEntry.ai_category_conf !== null && detailEntry.ai_category_conf < 0.5"
              type="warning" size="small" style="margin-left: 4px"
            >需人工重新分类</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="确认分类（可修改）">
            <el-select
              v-model="editableCategoryId"
              size="small"
              filterable
              clearable
              placeholder="选择或搜索分类（如 虚拟机-001）"
              style="width: 280px"
              :loading="categoriesLoading"
              :disabled="!canEditCurrent"
            >
              <el-option
                v-for="cat in categoryOptions"
                :key="cat.code"
                :value="cat.code"
                :label="`${cat.code}  ${cat.name}`"
              >
                <span style="font-family:monospace;color:#606266;font-size:12px">{{ cat.code }}</span>
                <span style="margin-left:8px;color:#909399;font-size:12px">{{ cat.name }}</span>
              </el-option>
            </el-select>
          </el-descriptions-item>

          <el-descriptions-item v-if="detailEntry.ai_category_reason" label="AI 分类理由" :span="2">
            <span class="text-muted" style="font-size: 12px">{{ detailEntry.ai_category_reason }}</span>
          </el-descriptions-item>
        </el-descriptions>

        <div class="section-block" v-loading="revisionLoading">
          <div class="section-header-row">
            <h4 class="section-title">版本、生效与发布前检查</h4>
            <el-button
              size="small"
              type="primary"
              plain
              :loading="candidateValidationLoading"
              @click="validateCurrentCandidate()"
            >检查当前内容</el-button>
          </div>
          <el-alert
            v-if="detailEntry.status === 'published' && !detailEntry.maintenance_working"
            type="info"
            :closable="false"
            show-icon
            title="当前展示的是 Agent 生效版。要修改时请先创建维护工作稿；编辑期间 Agent 继续使用此版本。"
            style="margin-bottom: 10px"
          >
            <el-button type="primary" size="small" @click="createMaintenanceWorking">创建维护工作稿</el-button>
          </el-alert>
          <el-alert
            v-else-if="detailEntry.maintenance_working"
            type="warning"
            :closable="false"
            show-icon
            title="当前正在编辑维护工作稿；保存不会影响 Agent，只有点击“发布维护版”才会生效。"
            style="margin-bottom: 10px"
          />
          <el-descriptions :column="3" border size="small">
            <el-descriptions-item label="模型 Proposal">
              <span v-if="revisionState?.latest_proposal_revision_id">#{{ revisionState.latest_proposal_revision_id }}</span>
              <span v-else class="text-muted">首次编辑/发布时自动固化</span>
            </el-descriptions-item>
            <el-descriptions-item label="专家工作稿">
              <span v-if="revisionState?.working_revision_id">#{{ revisionState.working_revision_id }}</span>
              <span v-else class="text-muted">尚未修改</span>
            </el-descriptions-item>
            <el-descriptions-item label="Agent 当前生效">
              <span v-if="revisionState?.active_resource">
                runtime r{{ revisionState.active_resource.revision }} · {{ revisionState.active_resource.checksum.slice(0, 10) }}
              </span>
              <span v-else class="text-muted">未生效</span>
            </el-descriptions-item>
          </el-descriptions>
          <div class="section-hint" style="margin-top: 8px">
            保存只形成专家工作版本；只有“{{ detailEntry.maintenance_working ? '发布维护版' : '审核通过并发布' }}”才切换 Agent 生效版本。
            <template v-if="revisionState?.history?.[0]?.diff_from_parent?.length">
              当前专家稿相对基线修改 {{ revisionState.history[0].diff_from_parent.length }} 项。
            </template>
          </div>
          <div v-if="candidateValidation" class="expert-validation-panel" :class="`is-${candidateValidation.status}`">
            <div class="validation-summary">
              <span class="validation-icon">{{ candidateValidation.status === 'error' ? '✕' : candidateValidation.status === 'warning' ? '!' : '✓' }}</span>
              <div>
                <strong>{{ candidateValidation.status === 'ok' ? '发布前静态检查已通过' : `有 ${candidateValidation.issues.length} 项需要专家处理` }}</strong>
                <div class="section-hint">这里检查内容与参数契约，只显示可通过编辑当前 KBD 解决的问题；真实现场执行验证仍由 Agent 测试链路负责，平台部署状态不会混入专家待办。</div>
              </div>
            </div>
            <div v-for="issue in candidateValidation.issues" :key="`${issue.code}-${issue.location}`" class="validation-issue">
              <div class="validation-issue-content">
                <strong>{{ issue.message }}</strong>
                <el-button
                  v-if="validationIssueSignal(issue)"
                  class="validation-signal-link"
                  type="primary"
                  text
                  size="small"
                  @click="handleValidationAction(issue)"
                >
                  问题关键信号：{{ signalLabel(validationIssueSignal(issue)!.signal, validationIssueSignal(issue)!.index) }}（{{ validationIssueSignal(issue)!.signal.id }}）
                </el-button>
                <code>{{ issue.location }}</code>
              </div>
              <el-button
                v-if="validationIssueSignal(issue) || issue.action?.type === 'edit_signal_role'"
                type="primary"
                size="small"
                @click="handleValidationAction(issue)"
              >定位并编辑</el-button>
            </div>
          </div>
        </div>

        <!-- 元数据面板 -->
        <div class="section-block">
          <h4 class="section-title">来源元数据</h4>
          <el-descriptions :column="3" border size="small">
            <template v-for="key in metaKeys" :key="key">
              <el-descriptions-item v-if="detailEntry.metadata[key]" :label="metaLabel(key)">
                {{ detailEntry.metadata[key] }}
              </el-descriptions-item>
            </template>
          </el-descriptions>
        </div>

        <!-- 关键信号面板（QKV / QFK 分组） -->
        <div class="section-block">
          <div class="section-header-row">
            <h4 class="section-title">关键信号（QKV / QFK）</h4>
            <div class="section-actions">
              <el-dropdown :disabled="!canEditCurrent" @command="(tool: string) => addSignal(tool)">
                <el-button type="primary" size="small">新增信号</el-button>
                <template #dropdown>
                  <el-dropdown-menu>
                    <el-dropdown-item command="qkv_task">任务信号 qkv_task</el-dropdown-item>
                    <el-dropdown-item command="qkv_alert">告警信号 qkv_alert</el-dropdown-item>
                    <el-dropdown-item command="qkv_dialog">纯弹框信号 qkv_dialog</el-dropdown-item>
                    <el-dropdown-item divided command="qfk_log">日志信号 qfk_log</el-dropdown-item>
                    <el-dropdown-item command="qfk_system">系统检查 qfk_system</el-dropdown-item>
                    <el-dropdown-item command="qfk_service">服务检查 qfk_service</el-dropdown-item>
                    <el-dropdown-item command="qfk_vm">虚拟机检查 qfk_vm</el-dropdown-item>
                    <el-dropdown-item command="qfk_network">网络检查 qfk_network</el-dropdown-item>
                    <el-dropdown-item command="qfk_storage">存储检查 qfk_storage</el-dropdown-item>
                    <el-dropdown-item command="qfk_hardware">硬件检查 qfk_hardware</el-dropdown-item>
                    <el-dropdown-item command="qfk_platform">平台检查 qfk_platform</el-dropdown-item>
                  </el-dropdown-menu>
                </template>
              </el-dropdown>
              <el-button
                v-if="detailEntry.status !== 'published'"
                type="warning"
                size="small"
                :loading="reextractSignalsLoading === detailEntry.id"
                :disabled="!canEditCurrent"
                @click="handleReextractSignals(detailEntry)"
                title="用最新 Prompt 重新抽取关键信号抽取"
              >
                <el-icon style="font-size: 14px;"><Refresh /></el-icon>
                重新抽取
              </el-button>
            </div>
          </div>

          <el-alert v-if="stagedSignalEditCount > 0" type="warning" :closable="false" show-icon>
            <template #title>
              已暂存 {{ stagedSignalEditCount }} 条信号的修改。可继续编辑其他信号；仅当整份 signals_json 都通过校验时才会统一保存。
            </template>
            <el-button text type="primary" size="small" @click="clearStagedSignalEdits">放弃全部暂存修改</el-button>
          </el-alert>
          <el-alert
            v-if="signalGenerationMetadata.status === 'stale'"
            type="error"
            :closable="false"
            show-icon
            title="正文、截图或工具契约已变化：当前 Signal/Contract 已过期，禁止自动执行；请重新抽取或完成人工复核。"
          />
          <div v-if="signalGenerationMetadata.model_id" class="proposal-summary">
            <span>AI Proposal：{{ signalGenerationMetadata.model_id }}</span>
            <el-tag
              size="small"
              :type="signalGenerationMetadata.status === 'stale' ? 'danger' : signalGenerationMetadata.status === 'manual_reviewed' ? 'success' : 'info'"
            >{{ signalGenerationMetadata.status === 'stale' ? '已过期' : signalGenerationMetadata.status === 'manual_reviewed' ? '已人工修改' : '未修改' }}</el-tag>
            <span v-if="revisionState?.history?.[0]?.diff_from_parent?.length">
              相对 AI Proposal 修改 {{ revisionState.history[0].diff_from_parent.length }} 项
            </span>
          </div>
          <details v-if="signalGenerationMetadata.generation_fingerprint" class="generation-trace-details">
            <summary>生成追溯详情</summary>
            <div><span>模型</span><code>{{ signalGenerationMetadata.model_id || '—' }}</code></div>
            <div><span>Prompt 版本</span><code>{{ signalGenerationMetadata.prompt_revision || '—' }}</code></div>
            <div><span>生成环境指纹</span><code>{{ signalGenerationMetadata.generation_fingerprint }}</code></div>
            <div class="field-hint">用于问题追溯和模型评估，审核时通常无需处理。</div>
          </details>
          <el-alert
            v-if="rejectedSignalCandidates.length > 0"
            type="warning"
            :closable="false"
            show-icon
            :title="`本轮有 ${rejectedSignalCandidates.length} 条候选未通过工程门禁，已保留原始候选与拒绝原因供复核。`"
          />
          <el-collapse v-if="rejectedSignalCandidates.length > 0" class="rejected-candidates">
            <el-collapse-item
              v-for="(item, index) in rejectedSignalCandidates"
              :key="`rejected-${index}`"
              :title="`拒绝候选 ${index + 1}：${item.reason}`"
              :name="`rejected-${index}`"
            >
              <pre class="code rejected-candidate-json">{{ JSON.stringify(item.candidate, null, 2) }}</pre>
              <el-button type="primary" size="small" @click="restoreRejectedCandidate(item.candidate)">恢复并编辑</el-button>
            </el-collapse-item>
          </el-collapse>

          <div v-if="shouldSignalCount > 0" class="evidence-policy-editor">
            <div>
              <strong>增强证据规则</strong>
              <span>共 {{ shouldSignalCount }} 条，至少满足</span>
              <el-input-number v-model="minimumShouldCount" :min="0" :max="shouldSignalCount" size="small" :disabled="!canEditCurrent" />
              <span>条</span>
              <el-button type="primary" plain size="small" :disabled="!canEditCurrent" :loading="signalSaveLoading" @click="saveMinimumShouldRule">保存规则</el-button>
            </div>
            <div class="field-hint">0 表示这些证据可有可无；1 表示至少满足一条。若 qkv_alert 与 qkv_task 二选一，请都设为“增强证据”，并把这里设为 1。</div>
          </div>

          <!-- 生产者信号（QKV） -->
          <div class="signal-group">
            <div class="signal-group-title">生产者信号（QKV：前端采集，写入变量池）</div>
            <el-empty v-if="producerSignals.length === 0" description="暂无生产者信号" :image-size="44" />
            <div
              v-for="item in producerSignals"
              :id="signalDomId(signalStableId(item.sig, item.origIdx))"
              :key="signalStableId(item.sig, item.origIdx)"
              class="signal-card"
              :class="{ 'is-focused': focusedSignalId === signalStableId(item.sig, item.origIdx) }"
            >
              <div class="signal-card-head">
                <el-tag size="small" type="success">{{ sigTool(item.sig) || 'qkv' }}</el-tag>
                <el-tag v-if="capabilityStatus(sigTool(item.sig)) !== 'declared'" size="small" type="danger" effect="plain">能力未声明，请更换采集类型</el-tag>
                <el-tag size="small" effect="plain">{{ sigRoleLabel(item.sig) }}</el-tag>
                <el-tag v-if="sigProvenance(item.sig).needs_review" size="small" type="warning">需人工复核</el-tag>
                <el-tag v-if="hasStagedSignalEdit(item.sig, item.origIdx)" size="small" type="warning" effect="plain">已暂存</el-tag>
                <div class="signal-card-actions">
                  <el-button text size="small" :disabled="!canEditCurrent || item.origIdx === 0" @click="moveSignal(item.origIdx, -1)">上移</el-button>
                  <el-button text size="small" :disabled="!canEditCurrent || item.origIdx === signalList.length - 1" @click="moveSignal(item.origIdx, 1)">下移</el-button>
                  <el-button text size="small" :disabled="!canEditCurrent" @click="duplicateSignal(item.origIdx)">复制</el-button>
                  <el-button text type="danger" size="small" :disabled="!canEditCurrent" @click="deleteSignal(item.origIdx)">删除</el-button>
                  <el-button v-if="editingSignalIndex !== item.origIdx" text type="primary" size="small" :disabled="!canEditCurrent" @click="startEditSignal(item.origIdx)">编辑</el-button>
                  <template v-else>
                    <el-button text size="small" @click="cancelEditSignal">取消</el-button>
                    <el-button text type="primary" size="small" :loading="signalSaveLoading" @click="saveSignalEdit">保存</el-button>
                  </template>
                </div>
              </div>
              <div class="signal-card-body">
                <div v-if="editingSignalIndex !== item.origIdx">
                  <div class="signal-row"><span class="signal-k">说明</span><span class="signal-v">{{ sigArgs(item.sig).instruction || '—' }}</span></div>
                  <div class="signal-row"><span class="signal-k">关键字</span><span class="signal-v">{{ sigArgs(item.sig).keyword || '—' }}</span></div>
                  <div class="signal-row"><span class="signal-k">产出变量</span><span class="signal-v">{{ (sigOrch(item.sig).produces || []).map((p: any) => p.name).join('、') || '—' }}</span></div>
                  <!-- 来源证据固定在信号卡片最后：它是只读溯源，不应打断专家对可执行字段的阅读。 -->
                  <details class="signal-evidence-details">
                    <summary>来源证据（默认收起，不参与编辑）</summary>
                    <div class="signal-row"><span class="signal-k">证据来源</span><span class="signal-v code">{{ sigSourceRefs(item.sig).join('、') || sigProvenance(item.sig).source_section || '—' }}</span></div>
                    <div class="signal-row"><span class="signal-k">证据原文</span><span class="signal-v">{{ sigProvenance(item.sig).evidence || '—' }}</span></div>
                  </details>
                </div>
                <div v-else>
                  <div class="signal-row"><span class="signal-k">说明</span><el-input v-model="signalEditDraft.acquire.args.instruction" size="small" type="textarea" :rows="2" placeholder="信号说明，如 镜像文件占用检查" /></div>
                  <div class="field-hint">信号语义说明：用自然语言描述这个采集做什么（如「镜像文件占用检查」），是人类可读标题，不是匹配条件</div>
                  <div class="signal-row"><span class="signal-k">证据作用</span><el-select v-model="signalEditDraft.role" size="small"><el-option label="必要证据（必须满足）" value="must" /><el-option label="增强证据（按门槛满足）" value="should" /><el-option label="排除证据（出现即排除）" value="exclude" /><el-option label="上下文证据（执行但不参与结论）" value="context" /></el-select></div>
                  <div class="signal-row"><span class="signal-k">采集类型</span><el-select :model-value="sigTool(signalEditDraft)" size="small" @change="onSignalToolChange"><el-option label="任务 qkv_task" value="qkv_task" /><el-option label="告警 qkv_alert" value="qkv_alert" /><el-option label="纯弹框 qkv_dialog" value="qkv_dialog" /></el-select><span class="signal-nature">{{ qkvNatureLabel(sigTool(signalEditDraft)) }}</span></div>
                  <div class="signal-row"><span class="signal-k">关键字</span><el-input v-model="signalEditDraft.acquire.args.keyword" size="small" :placeholder="qkvKeywordPlaceholder(sigTool(signalEditDraft))" /></div>
                  <div v-if="sigTool(signalEditDraft) === 'qkv_alert'" class="field-hint">告警型关键字（acli alert get -k）：取自「分类基线 · 告警型故障」（标签以「告警」结尾），如 虚拟机CPU或内存占用过高告警、主机网口丢包告警、序列号过期告警。多个用逗号分隔</div>
                  <div v-else-if="sigTool(signalEditDraft) === 'qkv_task'" class="field-hint">任务失败型关键字（acli task get -k）：取自「分类基线 · 任务失败型故障」，如 虚拟机开机失败、虚拟机快照失败、虚拟机scmt迁移失败。多个用逗号分隔</div>
                  <div v-else-if="sigTool(signalEditDraft) === 'qkv_dialog'" class="field-hint">无任务/告警承载的页面弹框原文或稳定片段。运行时在当前主控 /sf/log/today 与 /sf/log/today/vt 检索，并提取 END、REQUEST_ID、HOST；若存在对应失败任务，应优先使用 qkv_task。</div>
                  <div v-else class="field-hint">前端采集匹配关键字（acli &lt;task|dialog|alert&gt; get -k）：取自「分类基线」标签。多个用逗号分隔</div>
                  <template v-if="sigTool(signalEditDraft) === 'qkv_dialog'">
                    <div class="signal-row"><span class="signal-k">搜索目录</span><span class="signal-v code">/sf/log/today、/sf/log/today/vt（固定）</span></div>
                    <div class="signal-row"><span class="signal-k">上下文行</span><el-input-number v-model="signalEditDraft.acquire.args.context_lines" :min="0" :max="10" size="small" /></div>
                    <div class="field-hint">qkv_dialog 不执行虚构的 acli dialog get；两个固定目录用于兼容不同版本的 aCLI 目录搜索深度。</div>
                  </template>
                  <div class="field-hint keyword-check" :class="{ 'is-warn': qkvKeywordMismatch(signalEditDraft) }">校验规则：关键字须与本案例「分类基线」标签语义一致——任务失败型（…失败/卡住/异常/不达预期）用 qkv_task/qkv_dialog；告警型（…告警）用 qkv_alert。类型选错会导致 acli 查不到记录、信号恒为假<template v-if="qkvKeywordMismatch(signalEditDraft)"> ⚠ 当前「{{ sigTool(signalEditDraft) }} + 该关键字」疑似类型不匹配，请复核</template></div>
                  <!-- 产出变量编辑（v2 orchestrate.produces） -->
                  <div class="signal-row">
                    <span class="signal-k">产出变量</span>
                    <div class="produces-editor-mini">
                      <div v-for="(p, idx) in (signalEditDraft.orchestrate.produces || [])" :key="idx" class="produce-item-mini">
                        <el-input v-model="p.name" size="small" placeholder="变量名" style="width: 120px" />
                        <el-input v-model="p.path" size="small" placeholder="JSON路径" style="flex: 1" />
                        <el-button text type="danger" size="small" @click="signalEditDraft.orchestrate.produces?.splice(idx, 1)">删除</el-button>
                      </div>
                      <el-button text type="primary" size="small" @click="signalEditDraft.orchestrate.produces = [...(signalEditDraft.orchestrate.produces || []), { name: '', path: '' }]">+ 添加变量</el-button>
                    </div>
                  </div>
                  <div class="field-hint" v-pre>抽取后写入变量池的变量名(name)与取值路径(path)，供下游消费者信号（QFK）通过 {{变量名}} 引用</div>
                </div>
              </div>
            </div>
          </div>

          <!-- 消费者信号（QFK） -->
          <div class="signal-group">
            <div class="signal-group-title">消费者信号（QFK：后端采集+判定，读取变量池）</div>
            <el-empty v-if="consumerSignals.length === 0" description="暂无消费者信号" :image-size="44" />
            <div
              v-for="item in consumerSignals"
              :id="signalDomId(signalStableId(item.sig, item.origIdx))"
              :key="signalStableId(item.sig, item.origIdx)"
              class="signal-card"
              :class="{ 'is-focused': focusedSignalId === signalStableId(item.sig, item.origIdx) }"
            >
              <div class="signal-card-head">
                <el-tag size="small" type="warning">{{ sigTool(item.sig) || 'qfk' }}</el-tag>
                <el-tag v-if="capabilityStatus(sigTool(item.sig)) !== 'declared'" size="small" type="danger" effect="plain">能力未声明，请更换采集类型</el-tag>
                <el-tag size="small" effect="plain">{{ sigRoleLabel(item.sig) }}</el-tag>
                <el-tag v-if="sigProvenance(item.sig).needs_review" size="small" type="warning">需人工复核</el-tag>
                <el-tag v-if="hasStagedSignalEdit(item.sig, item.origIdx)" size="small" type="warning" effect="plain">已暂存</el-tag>
                <div class="signal-card-actions">
                  <el-button text size="small" :disabled="!canEditCurrent || item.origIdx === 0" @click="moveSignal(item.origIdx, -1)">上移</el-button>
                  <el-button text size="small" :disabled="!canEditCurrent || item.origIdx === signalList.length - 1" @click="moveSignal(item.origIdx, 1)">下移</el-button>
                  <el-button text size="small" :disabled="!canEditCurrent" @click="duplicateSignal(item.origIdx)">复制</el-button>
                  <el-button text type="danger" size="small" :disabled="!canEditCurrent" @click="deleteSignal(item.origIdx)">删除</el-button>
                  <el-button v-if="editingSignalIndex !== item.origIdx" text type="primary" size="small" :disabled="!canEditCurrent" @click="startEditSignal(item.origIdx)">编辑</el-button>
                  <template v-else>
                    <el-button text size="small" @click="cancelEditSignal">取消</el-button>
                    <el-button text type="primary" size="small" :loading="signalSaveLoading" @click="saveSignalEdit">保存</el-button>
                  </template>
                </div>
              </div>
              <div class="signal-card-body">
                <!-- 展示模式 -->
                <div v-if="editingSignalIndex !== item.origIdx">
                  <!-- 共有字段 -->
                  <div class="signal-row"><span class="signal-k">说明</span><span class="signal-v">{{ sigArgs(item.sig).instruction || '—' }}</span></div>
                  <div class="signal-row"><span class="signal-k">主机</span><span class="signal-v code">{{ sigArgs(item.sig).host || '—' }}</span></div>
                  <template v-if="sigTool(item.sig) === 'qfk_system'">
                    <div class="signal-row"><span class="signal-k">容器</span><span class="signal-v">{{ sigArgs(item.sig).container || 'host' }}</span></div>
                    <div v-if="sigArgs(item.sig).cluster" class="signal-row"><span class="signal-k">集群执行</span><span class="signal-v">是（acli --cluster）</span></div>
                    <div v-if="sigArgs(item.sig).formatter" class="signal-row"><span class="signal-k">输出格式</span><span class="signal-v code">{{ sigArgs(item.sig).formatter }}</span></div>
                    <el-alert v-if="String(sigArgs(item.sig).command || '').includes('|')" title="历史命令含 Shell 管道，必须编辑并清理后才能统一保存" type="warning" :closable="false" show-icon />
                  </template>
                  <template v-if="sigTool(item.sig) === 'qfk_service'">
                    <div class="signal-row"><span class="signal-k">容器</span><span class="signal-v">{{ sigArgs(item.sig).container || 'asv' }}</span></div>
                    <div class="signal-row"><span class="signal-k">执行命令</span><span class="signal-v">{{ sigArgs(item.sig).command || sigOrch(item.sig).action || 'status' }}</span></div>
                  </template>
                  <template v-if="['qfk_vm', 'qfk_network', 'qfk_storage', 'qfk_hardware', 'qfk_platform'].includes(sigTool(item.sig))">
                    <div class="signal-row"><span class="signal-k">执行命令</span><span class="signal-v code">{{ sigArgs(item.sig).command || '—' }}</span></div>
                  </template>
                  <div class="signal-row"><span class="signal-k">输入变量</span><span class="signal-v code">{{ (sigOrch(item.sig).requires || []).join('、') || '—' }}</span></div>
                  <div class="signal-row"><span class="signal-k">超时时间</span><span class="signal-v">{{ sigArgs(item.sig).timeout || 120 }}s</span></div>
                  <div class="signal-row"><span class="signal-k">执行模式</span><span class="signal-v">{{ qfkOutputMode(item.sig) === 'produces' ? '产出变量（采集命令结果）' : '匹配模式' }}</span></div>
                  <template v-if="qfkOutputMode(item.sig) === 'produces'">
                    <div v-for="(p, idx) in (sigOrch(item.sig).produces || [])" :key="`output-${idx}`" class="signal-row">
                      <span class="signal-k">{{ idx === 0 ? '产出变量' : '' }}</span>
                      <span class="signal-v code">
                        {{ p.name || '—' }}（{{ p.type || 'string' }} / {{ p.extract?.type === 'json' ? 'JSON 路径' : '声明式文本' }}）
                        · {{ p.extract?.type === 'json' ? (p.extract.path || '根节点') : (p.extract?.columns?.length ? `已选 ${p.extract.columns.length} 列` : '整行') }}
                      </span>
                    </div>
                  </template>
                  <template v-else>
                    <div class="signal-row"><span class="signal-k">判定类型</span><span class="signal-v code">{{ sigMatch(item.sig).type || '—' }}</span></div>
                    <div v-if="sigMatch(item.sig).pattern" class="signal-row"><span class="signal-k">匹配内容</span><span class="signal-v">{{ Array.isArray(sigMatch(item.sig).pattern) ? sigMatch(item.sig).pattern.join(' / ') : sigMatch(item.sig).pattern }}</span></div>
                    <div v-if="sigMatch(item.sig).metric" class="signal-row"><span class="signal-k">指标字段</span><span class="signal-v code">{{ sigMatch(item.sig).metric }}</span></div>
                    <div v-if="sigMatch(item.sig).value !== undefined" class="signal-row"><span class="signal-k">比较条件</span><span class="signal-v">{{ sigMatch(item.sig).operator || sigMatch(item.sig).direction || '' }} {{ sigMatch(item.sig).value }}</span></div>
                    <div class="signal-row"><span class="signal-k">期望</span><span class="signal-v">{{ sigMatch(item.sig).expected === true ? '存在' : sigMatch(item.sig).expected === false ? '不存在' : '—' }}</span></div>
                    <div v-if="sigMatch(item.sig).type === 'keyword'" class="signal-row"><span class="signal-k">组合关系</span><span class="signal-v">{{ sigMatch(item.sig).mode || 'or' }}</span></div>
                  </template>

                  <!-- 其他工具特有字段 -->
                  <div v-if="sigTool(item.sig) === 'qfk_system'" class="signal-row"><span class="signal-k">命令字段</span><span class="signal-v code">{{ qfkSystemCommandText(sigArgs(item.sig)) || '—' }}</span></div>
                  <template v-if="sigTool(item.sig) === 'qfk_log'">
                    <div class="signal-row"><span class="signal-k">文件</span><span class="signal-v code">{{ sigArgs(item.sig).file || '—' }}</span></div>
                    <div class="signal-row"><span class="signal-k">时间</span><span class="signal-v">{{ sigArgs(item.sig).time_window || '—' }}</span></div>
                    <details class="signal-advanced-details">
                      <summary>日志定位高级设置</summary>
                      <div class="signal-row"><span class="signal-k">日志族</span><span class="signal-v">{{ sigArgs(item.sig).source_family || 'auto（按文件/路径推断）' }}</span></div>
                      <div class="signal-row"><span class="signal-k">路径</span><span class="signal-v code">{{ sigArgs(item.sig).path || '通用定位（默认搜索 /sf/log）' }}</span></div>
                      <div class="signal-row"><span class="signal-k">解析器</span><span class="signal-v code">{{ sigArgs(item.sig).parser || '自动选择' }}</span></div>
                    </details>
                  </template>
                  <template v-if="sigTool(item.sig) === 'qfk_service'">
                    <div class="signal-row"><span class="signal-k">服务</span><span class="signal-v code">{{ sigArgs(item.sig).resource_keyword || '—' }}</span></div>
                  </template>
                  <div class="signal-row command-preview-row">
                    <span class="signal-k">完整命令</span>
                    <div class="signal-v">
                      <el-button text type="primary" size="small" :loading="commandPreviewLoading[commandPreviewKey(item.sig, item.origIdx)]" @click="toggleCommandPreview(item.sig, item.origIdx)">
                        {{ hasCommandPreview(item.sig, item.origIdx) ? '收起完整命令' : '查看完整 HCI 执行命令' }}
                      </el-button>
                      <div v-if="hasCommandPreview(item.sig, item.origIdx)" class="command-preview-panel">
                        <template v-if="commandPreviews[commandPreviewKey(item.sig, item.origIdx)]">
                          <code>{{ commandPreviews[commandPreviewKey(item.sig, item.origIdx)]!.command }}</code>
                          <div class="command-preview-meta">
                            <span>SSH 目标主机：{{ commandPreviews[commandPreviewKey(item.sig, item.origIdx)]!.host || '由运行时上下文决定' }}</span>
                            <span v-if="commandPreviews[commandPreviewKey(item.sig, item.origIdx)]!.variables?.length">执行前替换变量：{{ commandPreviews[commandPreviewKey(item.sig, item.origIdx)]!.variables!.join('、') }}</span>
                            <el-button text type="primary" size="small" @click="copyCommandPreview(item.sig, item.origIdx)">复制命令</el-button>
                          </div>
                          <div class="field-hint command-preview-notice">{{ commandPreviews[commandPreviewKey(item.sig, item.origIdx)]!.notice }}</div>
                        </template>
                        <el-alert v-else-if="commandPreviewErrors[commandPreviewKey(item.sig, item.origIdx)]" type="warning" :closable="false" show-icon :title="`无法编译完整命令：${commandPreviewErrors[commandPreviewKey(item.sig, item.origIdx)]}`">
                          <el-button text type="primary" size="small" @click="loadCommandPreview(item.sig, item.origIdx, true)">重新编译</el-button>
                        </el-alert>
                      </div>
                    </div>
                  </div>
                  <!-- 来源证据只用于追溯 LLM/原案例依据；固定放在卡片末尾，默认不展开。 -->
                  <details class="signal-evidence-details">
                    <summary>来源证据（默认收起，不参与编辑）</summary>
                    <div class="signal-row"><span class="signal-k">证据来源</span><span class="signal-v code">{{ sigSourceRefs(item.sig).join('、') || sigProvenance(item.sig).source_section || '—' }}</span></div>
                    <div class="signal-row"><span class="signal-k">证据原文</span><span class="signal-v">{{ sigProvenance(item.sig).evidence || '—' }}</span></div>
                  </details>
                </div>

                <!-- 编辑模式 -->
                <div v-else>
                  <!-- 共有字段 -->
                  <div class="signal-row"><span class="signal-k">说明</span><el-input v-model="signalEditDraft.acquire.args.instruction" size="small" placeholder="信号说明，如 镜像文件占用检查" /></div>
                  <div class="field-hint">信号语义说明：用自然语言描述这个检查/采集做什么（如「镜像文件占用检查」），是人类可读标题，不是匹配条件</div>
                  <div class="signal-row"><span class="signal-k">证据作用</span><el-select v-model="signalEditDraft.role" size="small"><el-option label="必要证据（必须满足）" value="must" /><el-option label="增强证据（按门槛满足）" value="should" /><el-option label="排除证据（出现即排除）" value="exclude" /><el-option label="上下文证据（执行但不参与结论）" value="context" /></el-select></div>
                  <div class="signal-row"><span class="signal-k">采集类型</span><el-select :model-value="sigTool(signalEditDraft)" size="small" filterable @change="onSignalToolChange"><el-option label="日志 qfk_log" value="qfk_log" /><el-option label="系统 qfk_system" value="qfk_system" /><el-option label="服务 qfk_service" value="qfk_service" /><el-option label="虚拟机 qfk_vm" value="qfk_vm" /><el-option label="网络 qfk_network" value="qfk_network" /><el-option label="存储 qfk_storage" value="qfk_storage" /><el-option label="硬件 qfk_hardware" value="qfk_hardware" /><el-option label="平台 qfk_platform" value="qfk_platform" /></el-select></div>
                  <div class="signal-row"><span class="signal-k">主机</span><el-input v-model="signalEditDraft.acquire.args.host" size="small" placeholder="{{HOST}} 或固定主机名/IP" /></div>
                  <div class="field-hint" v-pre>Terminal Bridge 通过此主机选择 SSH 会话；它不是 aCLI 参数。要遍历集群，请在下方启用“集群执行”。</div>
                  <!-- 容器与执行命令：位于输入/输出契约之前，先明确命令在哪里、执行什么。 -->
                  <template v-if="sigTool(signalEditDraft) === 'qfk_system'">
                    <div class="signal-row"><span class="signal-k">容器</span>
                      <el-select v-model="signalEditDraft.acquire.args.container" size="small" placeholder="host">
                        <el-option label="host" value="host" />
                        <el-option label="asv-con" value="asv-con" />
                        <el-option label="vn-con" value="vn-con" />
                        <el-option label="vn-agent" value="vn-agent" />
                        <el-option label="vs-cp-manager" value="vs-cp-manager" />
                      </el-select>
                    </div>
                    <div class="field-hint"><code>host</code> 表示不添加 <code>acli --container</code>；其他选项会作为 aCLI 容器参数。Terminal Bridge 始终在目标主机上启动 aCLI。</div>
                    <div class="signal-row"><span class="signal-k">集群执行</span><el-switch v-model="signalEditDraft.acquire.args.cluster" active-text="添加 acli --cluster" /></div>
                    <div class="signal-row"><span class="signal-k">输出格式</span><el-select v-model="signalEditDraft.acquire.args.formatter" size="small" clearable placeholder="默认文本"><el-option label="json" value="json" /><el-option label="keyvalue" value="keyvalue" /><el-option label="csv" value="csv" /><el-option label="xml" value="xml" /></el-select></div>
                    <div class="signal-row"><span class="signal-k">执行命令</span><el-input :model-value="qfkSystemCommandText(signalEditDraft.acquire.args)" size="small" placeholder="如 ps -p {{PID}} -o cmd=（不含 acli system）" @input="(value: string) => setQfkSystemCommandText(signalEditDraft.acquire.args, value)" /></div>
                    <div v-if="String(signalEditDraft.acquire.args.command || '').includes('|')" class="signal-row pipeline-warning">
                      <span class="signal-k"></span>
                      <div class="signal-v">
                        <el-alert title="检测到 Shell 管道，不能直接保存" type="warning" :closable="false" show-icon />
                        <el-button type="primary" size="small" :loading="pipelineConvertLoading" @click="convertDraftPipeline">安全转换管道</el-button>
                      </div>
                    </div>
                    <div class="field-hint">最终命令：<code>{{ qfkSystemCommandPreview(signalEditDraft.acquire.args) }}</code>。保存时系统会把命令安全规范化为基础命令和 argv；grep/awk/cut 的安全子集改用下方“文本取值”，不执行 Shell 管道。</div>
                  </template>
                  <template v-if="sigTool(signalEditDraft) === 'qfk_service'">
                    <div class="signal-row"><span class="signal-k">容器</span><el-input v-model="signalEditDraft.acquire.args.container" size="small" placeholder="服务容器，如 asv" /></div>
                    <div class="signal-row"><span class="signal-k">执行命令</span>
                      <el-select v-model="signalEditDraft.acquire.args.command" size="small">
                        <el-option label="status" value="status" />
                        <el-option label="start" value="start" />
                        <el-option label="stop" value="stop" />
                        <el-option label="restart" value="restart" />
                      </el-select>
                    </div>
                  </template>
                  <template v-if="['qfk_vm', 'qfk_network', 'qfk_storage', 'qfk_hardware', 'qfk_platform'].includes(sigTool(signalEditDraft))">
                    <div class="signal-row"><span class="signal-k">执行命令</span><el-input v-model="signalEditDraft.acquire.args.command" size="small" placeholder="子命令，如 list / show / get status" /></div>
                    <div class="field-hint">acli 之后的子命令（如 list、show、get status）；命名空间由工具名（qfk_vm 等）隐含，勿含 acli 前缀</div>
                  </template>

                  <div class="signal-row command-preview-row">
                    <span class="signal-k">完整命令</span>
                    <div class="signal-v">
                      <el-button text type="primary" size="small" :loading="commandPreviewLoading[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]" @click="loadCommandPreview(signalEditDraft, editingSignalIndex ?? 0, true)">
                        编译当前草稿的 HCI 执行命令
                      </el-button>
                      <div v-if="commandPreviews[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)] || commandPreviewErrors[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]" class="command-preview-panel">
                        <template v-if="commandPreviews[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]">
                          <code>{{ commandPreviews[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]!.command }}</code>
                          <div class="command-preview-meta">
                            <span>SSH 目标主机：{{ commandPreviews[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]!.host || '由运行时上下文决定' }}</span>
                            <span v-if="commandPreviews[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]!.variables?.length">执行前替换变量：{{ commandPreviews[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]!.variables!.join('、') }}</span>
                            <el-button text type="primary" size="small" @click="copyCommandPreview(signalEditDraft, editingSignalIndex ?? 0)">复制命令</el-button>
                          </div>
                        </template>
                        <el-alert v-else type="warning" :closable="false" show-icon :title="`无法编译完整命令：${commandPreviewErrors[commandPreviewKey(signalEditDraft, editingSignalIndex ?? 0)]}`" />
                      </div>
                      <div class="field-hint command-preview-notice">预览由当前 Agent Handler 编译，不执行命令；未就绪变量会以 <code v-pre>{{变量名}}</code> 形式保留。</div>
                    </div>
                  </div>

                  <!-- 输入变量由所有 {{VAR}} 占位符自动推导，避免重复维护两份契约。 -->
                  <div class="signal-row">
                    <span class="signal-k">输入变量</span>
                    <div class="signal-v variable-chips">
                      <el-tag v-for="name in deriveSignalRequires(signalEditDraft)" :key="name" size="small">{{ name }}</el-tag>
                      <span v-if="deriveSignalRequires(signalEditDraft).length === 0" class="muted">无</span>
                    </div>
                  </div>
                  <div class="field-hint" v-pre>根据主机、命令参数、筛选条件中的 {{变量名}} 自动生成，只读展示。</div>
                  <div class="signal-row"><span class="signal-k">超时时间</span><el-input-number v-model="signalEditDraft.acquire.args.timeout" :min="1" :max="300" size="small" /> 秒</div>
                  <div class="field-hint">命令在 terminal bridge 上的最大实际执行时间，范围 1–300 秒；超时后桥会停止命令并返回 timeout。</div>
                  <div class="signal-row">
                    <span class="signal-k">执行模式</span>
                    <el-radio-group :model-value="qfkOutputMode(signalEditDraft)" size="small" @change="setQfkOutputMode">
                      <el-radio-button label="keyword">匹配模式</el-radio-button>
                      <el-radio-button label="produces">产出变量</el-radio-button>
                    </el-radio-group>
                  </div>
                  <div class="field-hint">二选一：匹配模式用关键字、正则、状态、阈值等规则判断命令结果；产出变量模式把提取结果写入变量池，供后续信号使用。</div>
                  <template v-if="qfkOutputMode(signalEditDraft) === 'keyword' && signalEditDraft.match">
                    <MatcherEditor
                      v-model="signalEditDraft.match"
                      :allowed-types="sigTool(signalEditDraft) === 'qfk_log'
                        ? ['keyword', 'regex', 'state', 'threshold', 'delta', 'trend', 'exists']
                        : undefined"
                    />
                  </template>
                  <template v-else-if="qfkOutputMode(signalEditDraft) === 'produces'">
                    <div class="signal-row">
                      <span class="signal-k">产出变量</span>
                      <div class="produces-editor-mini">
                        <div v-for="(p, idx) in (signalEditDraft.orchestrate.produces || [])" :key="idx" class="produce-item-mini output-extract-card">
                          <div class="output-extract-grid">
                            <label>变量名</label>
                            <el-input v-model="p.name" size="small" placeholder="如 KVM_PID（必填）" />
                            <label>变量类型</label>
                            <el-select v-model="p.type" size="small">
                              <el-option label="字符串" value="string" />
                              <el-option label="整数" value="integer" />
                              <el-option label="数字" value="number" />
                              <el-option label="布尔值" value="boolean" />
                            <el-option label="数组" value="array" />
                            <el-option label="对象" value="object" />
                            <el-option label="对象数组" value="array<object>" />
                          </el-select>
                            <label>声明式取值</label>
                            <ValueExtractEditor v-model="p.extract" :default-value-mode="p.type || 'string'" />
                          </div>
                          <el-button text type="danger" size="small" @click="signalEditDraft.orchestrate.produces?.splice(idx, 1)">删除变量</el-button>
                        </div>
                        <el-button text type="primary" size="small" @click="signalEditDraft.orchestrate.produces = [...(signalEditDraft.orchestrate.produces || []), { name: '', type: 'string', extract: { type: 'text', rows: { mode: 'all' }, cardinality: 'exactly_one', source: 'stdout', value_mode: 'string' } }]">+ 添加变量</el-button>
                      </div>
                    </div>
                    <div class="field-hint">变量和匹配模式使用同一份声明式取值：可选择完整输出、文本行列或 JSON 路径。列号从 1 开始，但不会执行 awk。</div>
                  </template>

                  <!-- 其他工具特有字段 -->
                  <template v-if="sigTool(signalEditDraft) === 'qfk_log'">
                    <div class="signal-row"><span class="signal-k">文件</span><el-input v-model="signalEditDraft.acquire.args.file" size="small" placeholder="安全 basename，如 sfvt_vtpdaemon.log / LOG_ifconfig.txt" /></div>
                    <div class="field-hint">只填 basename，禁止包含目录；扩展名不限。blackbox、whitebox 和其他 /sf/log 日志都使用 qfk_log。</div>
                    <div class="signal-row"><span class="signal-k">时间</span><el-input v-model="signalEditDraft.acquire.args.time_window" size="small" placeholder="{{END}}（推荐）或 YYYY-MM-DD HH:MM:SS" /></div>
                    <div class="field-hint">推荐引用上游 qkv_task/qkv_alert/qkv_dialog 产出的 <code v-pre>{{END}}</code>。Agent 保持绝对时间，再由 qfk_log 按日志族转换为所需日期格式。</div>
                    <div class="signal-row"><span class="signal-k">Request ID</span><el-input v-model="signalEditDraft.acquire.args.request_id" size="small" placeholder="可选，如 {{REQUEST_ID}}" /></div>
                    <div class="signal-row"><span class="signal-k">上下文行</span><el-input-number v-model="signalEditDraft.acquire.args.context_lines" :min="0" :max="50" size="small" /></div>
                    <details class="signal-advanced-details">
                      <summary>日志定位高级设置（通常无需修改）</summary>
                      <div class="field-hint">留空时按文件名和默认 /sf/log 范围自动定位。只有自动定位失败或案例明确指定特殊日志域时才设置。</div>
                      <div class="signal-row"><span class="signal-k">日志族</span>
                        <el-select v-model="signalEditDraft.acquire.args.source_family" size="small" clearable placeholder="auto（按文件/路径推断）">
                          <el-option label="auto（按文件/路径推断）" value="auto" />
                          <el-option label="whitebox（白盒日志）" value="whitebox" />
                          <el-option label="blackbox（黑盒日志）" value="blackbox" />
                          <el-option label="vn_blackbox（虚拟网络黑盒）" value="vn_blackbox" />
                          <el-option label="pod（容器日志）" value="pod" />
                        </el-select>
                      </div>
                      <div class="signal-row"><span class="signal-k">路径</span><el-input v-model="signalEditDraft.acquire.args.path" size="small" placeholder="可选；留空默认搜索 /sf/log" /></div>
                      <div class="field-hint">显式路径只允许受控日志域；&lt;日期&gt;/[日期] 不是可执行路径。</div>
                      <div class="signal-row"><span class="signal-k">解析器</span>
                        <el-select v-model="signalEditDraft.acquire.args.parser" size="small" clearable placeholder="自动选择">
                          <el-option label="plain_text（普通文本）" value="plain_text" />
                          <el-option label="timestamped_lines（带时间行）" value="timestamped_lines" />
                          <el-option label="timestamped_blocks（带时间块）" value="timestamped_blocks" />
                          <el-option label="ifconfig_snapshot（网卡快照）" value="ifconfig_snapshot" />
                          <el-option label="kv_counter_snapshot（计数器快照）" value="kv_counter_snapshot" />
                          <el-option label="process_snapshot（进程快照）" value="process_snapshot" />
                        </el-select>
                      </div>
                      <div class="signal-row"><span class="signal-k">显式路径搜索 .gz</span><el-switch v-model="signalEditDraft.acquire.args.include_archives" /></div>
                      <div v-if="signalEditDraft.acquire.args.include_archives" class="signal-row"><span class="signal-k">前置检查</span><el-select v-model="signalEditDraft.acquire.args.archive_precheck" size="small"><el-option label="已确认磁盘、日期和路径" value="verified" /></el-select></div>
                      <div v-if="signalEditDraft.acquire.args.include_archives" class="field-hint">普通 whitebox 历史日志由 END + aCLI 自动定位和解压；这里只控制显式 path 下的 .gz 搜索。</div>
                    </details>
                  </template>
                  <template v-if="sigTool(signalEditDraft) === 'qfk_service'">
                    <div class="signal-row"><span class="signal-k">服务</span><el-input v-model="signalEditDraft.acquire.args.resource_keyword" size="small" placeholder="服务名，如 asv、nginx、mgmt" /></div>
                    <div class="field-hint">目标服务名；status 查询，start/stop/restart 等写操作仍需人工确认。</div>
                  </template>
                </div>
              </div>
            </div>
          </div>
        </div>

        <!-- content_md 渲染 -->
        <div class="section-block">
          <div class="section-header-row">
            <h4 class="section-title">内容预览</h4>
            <div class="section-actions">
              <el-button
                v-if="!editingContent && canEditCurrent"
                text type="primary" size="small"
                @click="startInlineEdit"
              >✏️ 编辑原文</el-button>
              <template v-else>
                <el-button text size="small" @click="cancelInlineEdit">取消</el-button>
                <el-button
                  text type="primary" size="small"
                  :loading="inlineEditLoading"
                  @click="saveInlineEdit"
                >保存</el-button>
              </template>
            </div>
          </div>

          <!-- 编辑模式：可直接修改 Markdown 原文 -->
          <el-input
            v-if="editingContent"
            v-model="inlineContent"
            type="textarea"
            :rows="22"
            placeholder="Markdown 格式内容"
            style="font-family: monospace; font-size: 13px; margin-top: 8px"
          />

          <!-- 预览模式：普通段 + 截图 accordion 卡片 -->
          <template v-else>
            <template v-for="(seg, i) in parsedSegments" :key="i">
              <!-- 普通 Markdown 段落 -->
              <div v-if="seg.type === 'normal'" class="md-render" v-html="seg.html" />

              <!-- 截图说明 accordion 卡片 -->
              <div
                v-else
                class="screenshot-card"
                :style="{ borderLeftColor: seg.typeInfo.color }"
              >
                <!-- 收起状态：只显示类型标签 -->
                <div
                  class="screenshot-header"
                  :style="{ backgroundColor: seg.typeInfo.bgColor }"
                  @click="seg.expanded = !seg.expanded"
                >
                  <span class="screenshot-badge" :style="{ color: seg.typeInfo.color, borderColor: seg.typeInfo.color }">
                    {{ seg.typeInfo.icon }} {{ seg.typeInfo.label }}
                  </span>
                  <span v-if="seg.fields.intro" class="screenshot-intro-preview">
                    {{ seg.fields.intro.slice(0, 30) }}{{ seg.fields.intro.length > 30 ? '…' : '' }}
                  </span>
                  <el-button
                    v-if="detailEntry.status !== 'published'"
                    type="warning"
                    size="small"
                    style="margin-right: 8px;"
                    :loading="reanalyzeSingleLoading?.kbdId === detailEntry.id && reanalyzeSingleLoading?.seq === seg.seq"
                    :disabled="!canEditCurrent"
                    @click.stop="handleReanalyzeSingleImage(detailEntry, seg.seq !== undefined ? seg.seq : 0)"
                    title="重新识图此张"
                  >
                    <el-icon style="font-size: 14px;"><Refresh /></el-icon>
                    重新识图
                  </el-button>
                  <span class="toggle-arrow">{{ seg.expanded ? '▲' : '▼' }}</span>
                </div>

                <!-- 展开内容 -->
                <div v-if="seg.expanded" class="screenshot-body">
                  <!-- 1. 可见内容（根据截图类型决定截断方向，后端已处理） -->
                  <div class="ss-field">
                    <div class="ss-field-label">1. <strong>可见内容</strong>
                      <span v-if="seg.fields.fullText.length > seg.fields.visibleContent.length" class="ss-truncate-hint">
                        （显示 {{ seg.fields.visibleContent.length }} / 共 {{ seg.fields.fullText.length }} 行）
                      </span>
                    </div>
                    <ul v-if="seg.fields.visibleContent.length" class="ss-field-list">
                      <li v-for="(item, j) in seg.fields.visibleContent" :key="j">{{ item }}</li>
                    </ul>
                    <span v-else class="ss-empty">无</span>
                  </div>
                  <!-- Evidence v3：权威事实与模型推断分栏。未确认推断只供管理端复核。 -->
                  <template v-if="seg.evidence">
                    <div class="ss-field">
                      <div class="ss-field-label evidence-label-row">
                        <span>2. <strong>Observed Facts（可作为关键信号事实）</strong></span>
                        <el-tag size="small" type="success" effect="plain">直接观察</el-tag>
                      </div>
                      <ul v-if="seg.evidence.observedFacts.length" class="ss-field-list evidence-facts">
                        <li v-for="(fact, j) in seg.evidence.observedFacts" :key="`fact-${j}`">{{ fact }}</li>
                      </ul>
                      <span v-else class="ss-empty">未生成独立 Observed Facts；关键信号仍可使用上方 FULL_TEXT/OCR 原文</span>
                    </div>
                    <div
                      v-if="hasImageDescription(seg.evidence) || seg.evidence.inferences.length"
                      class="ss-field evidence-inferences"
                    >
                      <div class="ss-field-label evidence-label-row">
                        <span>3. <strong>语义描述</strong></span>
                        <el-tag
                          size="small"
                          :type="isExpertConfirmed(seg.evidence) ? 'success' : 'warning'"
                          effect="plain"
                        >
                          {{ inferenceStatusLabel(seg.evidence) }}
                        </el-tag>
                      </div>
                      <p v-if="hasImageDescription(seg.evidence)" class="ss-description">
                        {{ seg.evidence.description }}
                      </p>
                      <ul v-else class="ss-field-list">
                        <li v-for="(inference, j) in seg.evidence.inferences" :key="`inference-${j}`">{{ inference }}</li>
                      </ul>
                      <div v-if="!isExpertConfirmed(seg.evidence)" class="evidence-boundary-note">
                        仅供专家复核；不会进入 Agent 文档，也不会参与关键信号运行参数生成。
                      </div>
                    </div>
                  </template>
                  <!-- 历史 v2：只有源数据确实包含 KEY/TIPS 时才展示，不再制造“无”字段。 -->
                  <template v-else>
                    <div v-if="seg.fields.description || seg.fields.key.length" class="ss-field">
                      <div class="ss-field-label">2. <strong>{{ seg.fields.description ? '语义描述' : seg.errorLabel }}</strong></div>
                      <p v-if="seg.fields.description" class="ss-description">{{ seg.fields.description }}</p>
                      <ul v-else class="ss-field-list">
                        <li v-for="(item, j) in seg.fields.key" :key="j">{{ item }}</li>
                      </ul>
                    </div>
                    <div v-if="seg.fields.tips.length" class="ss-field">
                      <div class="ss-field-label">3. <strong>排障建议</strong></div>
                      <ul class="ss-field-list">
                        <li v-for="(item, j) in seg.fields.tips" :key="j">{{ item }}</li>
                      </ul>
                    </div>
                  </template>
                </div>
              </div>
            </template>
          </template>
        </div>

        <!-- 图片列表（从 images_json 渲染，权威数据源） -->
        <div v-if="detailEntry?.images_json?.length" class="section-block">
          <h4 class="section-title">图片列表 ({{ detailEntry.images_json.length }} 张)</h4>
          <div class="images-json-container">
            <template v-for="(img, imgIdx) in parsedImagesJson" :key="imgIdx">
              <div
                class="screenshot-card"
                :style="{ borderLeftColor: img.typeInfo.color }"
              >
                <!-- 收起状态：序号 + 类型标签 -->
                <div
                  class="screenshot-header"
                  :style="{ backgroundColor: img.typeInfo.bgColor }"
                  @click="img.expanded = !img.expanded"
                >
                  <span class="screenshot-badge" :style="{ color: img.typeInfo.color, borderColor: img.typeInfo.color }">
                    {{ img.typeInfo.icon }} img_{{ img.seq }}: {{ img.typeInfo.label }}
                  </span>
                  <span class="screenshot-intro-preview">
                    {{ img.section }}
                  </span>
                  <el-tag :type="qualityTagType(img.qualityStatus)" size="small" effect="plain">
                    {{ img.qualityStatus }}<template v-if="img.needsReview"> · 需复核</template>
                  </el-tag>
                  <el-tag
                    v-if="hasImageDescription(img) || img.inferences.length"
                    :type="isExpertConfirmed(img) ? 'success' : 'warning'"
                    size="small"
                    effect="plain"
                  >
                    {{ inferenceStatusLabel(img) }}
                  </el-tag>
                  <el-button
                    v-if="editingImageSeq !== img.seq"
                    type="primary"
                    size="small"
                    @click.stop="startEditImage(img)"
                    :disabled="!canEditCurrent"
                  >编辑识图内容</el-button>
                  <template v-else>
                    <el-button size="small" @click.stop="cancelEditImage">取消</el-button>
                    <el-button type="primary" size="small" :loading="imageSaveLoading" @click.stop="saveImageEdit">确认并保存修订</el-button>
                  </template>
                  <el-button
                    v-if="detailEntry.status !== 'published'"
                    type="warning"
                    size="small"
                    style="margin-right: 8px;"
                    :loading="reanalyzeSingleLoading?.kbdId === detailEntry.id && reanalyzeSingleLoading?.seq === img.seq"
                    :disabled="!canEditCurrent"
                    @click.stop="handleReanalyzeSingleImage(detailEntry, img.seq)"
                    title="重新识图此张"
                  >
                    <el-icon style="font-size: 14px;"><Refresh /></el-icon>
                    重新识图
                  </el-button>
                  <span class="toggle-arrow">{{ img.expanded ? '▲' : '▼' }}</span>
                </div>

                <!-- 展开内容 -->
                <div v-if="img.expanded" class="screenshot-body">
                  <template v-if="editingImageSeq === img.seq && imageEditDraft">
                    <div class="image-evidence-editor">
                      <label>所属章节</label>
                      <el-select v-model="imageEditDraft.section" size="small">
                        <el-option label="问题描述" value="problem_description" />
                        <el-option label="告警信息" value="alert_info" />
                        <el-option label="有效排查步骤" value="steps_text" />
                        <el-option label="根因" value="root_cause" />
                        <el-option label="解决方案" value="solution" />
                        <el-option label="操作影响" value="operational_impact" />
                        <el-option label="临时方案" value="is_temporary" />
                        <el-option label="建议总结" value="recommendations" />
                      </el-select>
                      <label>截图类型</label>
                      <el-select v-model="imageEditDraft.screenshotType" size="small">
                        <el-option label="任务截图" value="任务截图" />
                        <el-option label="告警截图" value="告警截图" />
                        <el-option label="弹框截图" value="弹框截图" />
                        <el-option label="终端截图" value="终端截图" />
                        <el-option label="日志截图" value="日志截图" />
                        <el-option label="配置截图" value="配置截图" />
                        <el-option label="其他截图" value="其他截图" />
                      </el-select>
                      <label>背景</label>
                      <el-select v-model="imageEditDraft.background" size="small">
                        <el-option v-for="color in ['白色', '黑色', '灰色', '彩色', '其他']" :key="color" :label="color" :value="color" />
                      </el-select>
                      <label>截图可见文字</label>
                      <el-input v-model="imageEditDraft.fullText" type="textarea" :rows="6" placeholder="每行一条，必须忠实记录截图可见文字" />
                      <label>Observed Facts</label>
                      <el-input v-model="imageEditDraft.observedFacts" type="textarea" :rows="4" placeholder="每行一条可直接观察事实，可用于生成关键信号" />
                      <label>Inferences</label>
                      <el-input v-model="imageEditDraft.inferences" type="textarea" :rows="3" placeholder="每行一条推断；不要把推断写成直接观察事实" />
                      <label>语义描述</label>
                      <el-input v-model="imageEditDraft.description" type="textarea" :rows="4" placeholder="这张截图对排障的含义" />
                    </div>
                    <el-alert
                      type="info"
                      :closable="false"
                      show-icon
                      title="确认并保存后，该截图语义标记为 expert_confirmed，并进入当前工作稿的 Agent 文档；既有关键信号会标记为 stale，需重新抽取并复核。模型原稿仍保留在 Revision 历史中。"
                    />
                  </template>
                  <template v-else>
                  <!-- 1. 背景颜色 -->
                  <div class="ss-field">
                    <div class="ss-field-label">背景颜色</div>
                    <span>{{ img.background }}</span>
                  </div>
                  <!-- 2. 截图类型 -->
                  <div class="ss-field">
                    <div class="ss-field-label">截图类型</div>
                    <span :style="{ color: img.typeInfo.color }">{{ img.typeInfo.label }}</span>
                  </div>
                  <!-- 3. 可见内容 -->
                  <div class="ss-field">
                    <div class="ss-field-label">
                      可见内容
                      <span v-if="img.fullText.length > img.visibleContent.length" class="ss-truncate-hint">
                        （显示 {{ img.visibleContent.length }} / 共 {{ img.fullText.length }} 行）
                      </span>
                    </div>
                    <ul v-if="img.visibleContent.length" class="ss-field-list">
                      <li v-for="(item, j) in img.visibleContent" :key="j">{{ item }}</li>
                    </ul>
                    <span v-else class="ss-empty">无</span>
                  </div>
                  <!-- 4. 语义描述 -->
                  <div class="ss-field">
                    <div class="ss-field-label">语义描述</div>
                    <p v-if="img.description" class="ss-description">{{ img.description }}</p>
                    <span v-else class="ss-empty">无</span>
                  </div>
                  <!-- 5. 文档上下文：证明图片位于哪个排障步骤，不把上下文当 OCR 事实 -->
                  <div class="ss-field evidence-context">
                    <div class="ss-field-label">文档上下文（非截图 OCR）</div>
                    <div><strong>前文：</strong>{{ img.contextBefore || '—' }}</div>
                    <div><strong>后文：</strong>{{ img.contextAfter || '—' }}</div>
                  </div>
                  <!-- 6. Evidence IR：观察事实与模型推测必须分栏展示 -->
                  <div class="ss-field">
                    <div class="ss-field-label">Observed Facts（可作为信号事实）</div>
                    <ul v-if="img.observedFacts.length" class="ss-field-list evidence-facts">
                      <li v-for="(fact, j) in img.observedFacts" :key="`fact-${j}`">{{ fact }}</li>
                    </ul>
                    <span v-else class="ss-empty">无；不得据此生成事实型信号</span>
                  </div>
                  <div class="ss-field evidence-inferences">
                    <div class="ss-field-label">
                      Inferences（模型推测，不得独立生成运行参数）
                      <el-tag
                        v-if="img.inferences.length"
                        :type="isExpertConfirmed(img) ? 'success' : 'warning'"
                        size="small"
                        effect="plain"
                      >
                        {{ inferenceStatusLabel(img) }}
                      </el-tag>
                    </div>
                    <ul v-if="img.inferences.length" class="ss-field-list">
                      <li v-for="(inference, j) in img.inferences" :key="`inference-${j}`">{{ inference }}</li>
                    </ul>
                    <span v-else class="ss-empty">无</span>
                    <div v-if="img.inferenceIssues.length" class="ss-empty">
                      风险码：{{ img.inferenceIssues.join(', ') }}
                    </div>
                  </div>
                  <details class="evidence-provenance">
                    <summary>识图追溯详情</summary>
                    <div>图片摘要：<code>{{ img.provenance.image_sha256 || '—' }}</code></div>
                    <div>识图模型：<code>{{ img.provenance.vision_model || '—' }}</code></div>
                    <div>Prompt 版本：<code>{{ img.provenance.prompt_revision || '—' }}</code></div>
                    <div>图像变换：<code>{{ img.provenance.transform || '—' }}</code></div>
                  </details>
                  </template>
                </div>
              </div>
            </template>
          </div>
        </div>

        <!-- 审核备注 -->
        <div class="section-block">
          <h4 class="section-title">审核备注</h4>
          <div class="reviewer-identity-row">
            <span>审核记录身份：</span>
            <strong>{{ currentUser > 0 ? `#${currentUser}` : '未填写' }}</strong>
            <el-tag type="warning" size="small" effect="plain">未接入 SSO，不计为认证 Expert Gold</el-tag>
            <el-button text type="primary" size="small" @click="changeReviewerIdentity">{{ currentUser > 0 ? '更换' : '填写' }}</el-button>
          </div>
          <el-input
            v-model="reviewNote"
            type="textarea"
            :rows="3"
            placeholder="可填写审核意见或修改说明（若需拒绝，点击下方「拒绝」按钮填写原因）"
          />
        </div>
      </template>

      <template #footer>
        <el-button @click="detailDialogVisible = false">关闭</el-button>
        <template v-if="detailEntry && detailEntry.status === 'draft'">
          <el-button type="danger" @click="openRejectDialog(detailEntry)">拒绝</el-button>
          <el-button type="success" @click="handleApprove(detailEntry)">审核通过并发布</el-button>
        </template>
        <template v-else-if="detailEntry && detailEntry.status === 'rejected'">
          <el-button type="warning" @click="handleRepublish(detailEntry)">重新发布</el-button>
          <el-button type="info" @click="handleRevertToDraft(detailEntry)">退回草稿</el-button>
        </template>
        <template v-else-if="detailEntry && detailEntry.status === 'published'">
          <template v-if="detailEntry.maintenance_working">
            <el-button type="danger" plain @click="discardMaintenanceWorking">放弃维护稿</el-button>
            <el-button type="success" @click="publishMaintenanceWorking">发布维护版</el-button>
          </template>
          <el-button v-else type="primary" @click="createMaintenanceWorking">创建维护工作稿</el-button>
        </template>
        <template v-else-if="detailEntry">
          <el-button type="info" @click="handleRevertToDraft(detailEntry)">退回草稿</el-button>
        </template>
      </template>
    </el-dialog>

    <!-- 拒绝弹窗 -->
    <el-dialog
      v-model="rejectDialogVisible"
      title="拒绝 KBD 条目"
      width="500px"
    >
      <p style="color: #606266; margin-bottom: 12px">
        条目：<strong>{{ rejectingEntry?.title }}</strong>
      </p>
      <el-form>
        <el-form-item label="拒绝原因" required>
          <el-input
            v-model="rejectNote"
            type="textarea"
            :rows="4"
            placeholder="请填写拒绝原因（将记录到 review_note）"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="rejectDialogVisible = false">取消</el-button>
        <el-button type="danger" :loading="rejectLoading" @click="submitReject">确认拒绝</el-button>
      </template>
    </el-dialog>

    <!-- 删除是影响模型纠错样本的重要操作：原因必须由专家显式选择，正文/截图不会删除。 -->
    <el-dialog v-model="deleteSignalDialogVisible" title="删除关键信号" width="520px" :close-on-click-modal="false">
      <p class="delete-signal-hint">
        将从 Agent 验证规则中移除此信号；原始 KBD 正文和截图证据保持不变。请选择最符合的原因，供后续模型和工具改进使用。
      </p>
      <el-form label-position="top">
        <el-form-item label="删除原因" required>
          <el-select v-model="deleteSignalReason" placeholder="请选择删除原因" style="width: 100%">
            <el-option v-for="item in changeReasonOptions" :key="item[0]" :label="item[1]" :value="item[0]" />
          </el-select>
        </el-form-item>
        <el-form-item label="补充说明（可选）">
          <el-input
            v-model="deleteSignalNote"
            type="textarea"
            :rows="3"
            maxlength="500"
            show-word-limit
            placeholder="例如：这是正常 GPU 主机的示例说明，不应在异常主机上执行。"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="deleteSignalDialogVisible = false">取消</el-button>
        <el-button type="danger" :loading="signalSaveLoading" @click="submitDeleteSignal">确认删除</el-button>
      </template>
    </el-dialog>

    <!-- 编辑弹窗 -->
    <el-dialog
      v-model="editDialogVisible"
      width="90%"
      class="premium-dialog"
      :fullscreen="editFullscreen"
      draggable
      align-center
      :close-on-click-modal="false"
    >
      <template #header>
        <div class="custom-dialog-header" style="display: flex; justify-content: space-between; align-items: center; width: 100%; padding-right: 32px; box-sizing: border-box;">
          <span class="el-dialog__title">编辑 KBD 条目</span>
          <el-button
            type="info"
            text
            circle
            :icon="FullScreen"
            class="fullscreen-toggle-btn"
            @click="editFullscreen = !editFullscreen"
            title="切换全屏"
          />
        </div>
      </template>
      <el-form label-width="80px" class="premium-form">
        <el-form-item label="标题">
          <el-input v-model="editTitle" placeholder="条目标题" />
        </el-form-item>
        <el-form-item label="分类">
          <el-select
            v-model="editCategoryId"
            filterable
            clearable
            placeholder="选择或搜索分类（如 虚拟机-001）"
            style="width: 300px"
            :loading="categoriesLoading"
          >
            <el-option
              v-for="cat in categoryOptions"
              :key="cat.code"
              :value="cat.code"
              :label="`${cat.code}  ${cat.name}`"
            >
              <span style="font-family:monospace;color:#606266;font-size:12px">{{ cat.code }}</span>
              <span style="margin-left:8px;color:#909399;font-size:12px">{{ cat.name }}</span>
            </el-option>
          </el-select>
        </el-form-item>
        <el-form-item label="内容" class="flex-form-item">
          <el-input
            v-model="editContent"
            type="textarea"
            :rows="18"
            placeholder="Markdown 格式内容"
            style="font-family: monospace; font-size: 13px"
            class="editor-textarea"
          />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="editLoading" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.kbd-review {
  padding: 20px;
}

/* 过滤栏搜索/重置按钮容器：flex 保持同行同高 */
.filter-btn-group {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: nowrap;
}

/* 操作列按钮容器：nowrap 防止换行 */
.action-btn-group {
  display: flex;
  align-items: center;
  flex-wrap: nowrap;
  gap: 2px;
}

.page-header {
  margin-bottom: 20px;
}

.page-title {
  margin: 0 0 8px;
  font-size: 22px;
  color: #303133;
}

.page-desc {
  margin: 0;
  color: #666;
  font-size: 14px;
}

.filter-card {
  margin-bottom: 16px;
}

.total-info {
  text-align: right;
  color: #909399;
  font-size: 14px;
}

.category-nav {
  margin-bottom: 12px;
}

.category-nav :deep(.el-select-dropdown__wrap) {
  max-height: 480px;
}

.table-card {
  min-height: 400px;
}


.support-link {
  color: #409eff;
  text-decoration: none;
  font-family: monospace;
  font-size: 13px;
}
.support-link:hover {
  text-decoration: underline;
}

.entry-title {
  color: #303133;
  line-height: 1.5;
}

.category-tag {
  font-size: 12px;
  color: #909399;
  background: #f5f7fa;
  padding: 2px 6px;
  border-radius: 3px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.section-block {
  margin-top: 20px;
}

.section-title {
  margin: 0 0 10px;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  padding-bottom: 6px;
  border-bottom: 1px solid #ebeef5;
}

.text-muted {
  color: #909399;
}

/* Markdown 渲染区 */
.md-render {
  background: #fafafa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 16px 20px;
  max-height: 480px;
  overflow-y: auto;
  font-size: 14px;
  line-height: 1.7;
  color: #303133;
}

.md-render :deep(.md-h2),
.md-render :deep(h2) {
  font-size: 16px;
  font-weight: 700;
  color: #1a1a2e;
  margin: 18px 0 8px;
  padding-bottom: 4px;
  border-bottom: 2px solid #409eff22;
}

.md-render :deep(.md-h3),
.md-render :deep(h3) {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin: 12px 0 6px;
}

.md-render :deep(.md-p),
.md-render :deep(p) {
  margin: 4px 0;
}

.md-render :deep(.md-blockquote),
.md-render :deep(blockquote) {
  background: #f0f9ff;
  border-left: 4px solid #409eff;
  border-radius: 0 4px 4px 0;
  padding: 8px 14px;
  margin: 8px 0;
  color: #4a6fa5;
  font-size: 13px;
}

.md-render :deep(.md-list),
.md-render :deep(ul),
.md-render :deep(ol) {
  margin: 6px 0 6px 20px;
  padding: 0;
}

.md-render :deep(.md-list li),
.md-render :deep(li) {
  margin: 3px 0;
}

.md-render :deep(code) {
  background: #f5f7fa;
  border: 1px solid #e4e7ed;
  border-radius: 3px;
  padding: 1px 5px;
  font-family: monospace;
  font-size: 13px;
  color: #c0392b;
}

.md-render :deep(strong) {
  font-weight: 700;
  color: #1a1a2e;
}

/* 内容预览：标题行（含编辑按钮） */
.section-header-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  margin-bottom: 10px;
}
.section-header-row .section-title {
  margin: 0;
  border-bottom: none;
  padding-bottom: 0;
}
.section-actions {
  display: flex;
  align-items: center;
  gap: 8px;
}
.generation-fingerprint {
  margin: 8px 0;
  font-family: monospace;
  overflow-wrap: anywhere;
}

.proposal-summary {
  display: flex;
  align-items: center;
  gap: 10px;
  margin: 10px 0;
  color: #606266;
  font-size: 13px;
}

.generation-trace-details,
.evidence-provenance {
  margin: 8px 0;
  color: #606266;
  font-size: 12px;
}

.generation-trace-details summary,
.evidence-provenance summary {
  width: fit-content;
  cursor: pointer;
  color: #409eff;
}

.generation-trace-details > div,
.evidence-provenance > div {
  display: grid;
  grid-template-columns: 100px minmax(0, 1fr);
  gap: 8px;
  margin-top: 6px;
}

.evidence-policy-editor {
  margin: 10px 0 14px;
  padding: 10px 12px;
  border: 1px solid #d9ecff;
  border-radius: 6px;
  background: #f4f9ff;
}
.evidence-policy-editor > div:first-child {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 8px;
}
.evidence-policy-editor .field-hint {
  margin-left: 0;
  margin-bottom: 0;
}

.expert-validation-panel {
  margin-top: 10px;
  padding: 12px 14px;
  border: 1px solid #b3e19d;
  border-radius: 6px;
  background: #f0f9eb;
}

.expert-validation-panel.is-warning {
  border-color: #f3d19e;
  background: #fdf6ec;
}

.expert-validation-panel.is-error {
  border-color: #fab6b6;
  background: #fef0f0;
}

.validation-summary {
  display: flex;
  align-items: flex-start;
  gap: 10px;
}

.validation-icon {
  display: inline-flex;
  align-items: center;
  justify-content: center;
  flex: 0 0 20px;
  width: 20px;
  height: 20px;
  border-radius: 50%;
  color: #fff;
  background: #67c23a;
  font-size: 13px;
  font-weight: 700;
  line-height: 1;
}

.is-warning .validation-icon { background: #e6a23c; }
.is-error .validation-icon { background: #f56c6c; }

.validation-issue {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 16px;
  margin: 10px 0 0 30px;
  padding-top: 10px;
  border-top: 1px solid rgba(144, 147, 153, 0.22);
}

.validation-issue-content {
  display: flex;
  flex-direction: column;
  gap: 4px;
}

.validation-issue-content code {
  color: #909399;
  font-size: 12px;
}

.validation-signal-link {
  align-self: flex-start;
  height: auto;
  padding: 0;
  font-family: monospace;
  line-height: 1.45;
  text-align: left;
  white-space: normal;
}

.image-evidence-editor {
  display: grid;
  grid-template-columns: 120px minmax(0, 1fr);
  align-items: start;
  gap: 10px 12px;
  margin-bottom: 12px;
}

.image-evidence-editor > label {
  padding-top: 6px;
  color: #606266;
  font-size: 13px;
  font-weight: 600;
}

.reviewer-identity-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 10px;
  color: #606266;
  font-size: 13px;
}

.delete-signal-hint {
  margin: 0 0 16px;
  color: var(--el-text-color-regular);
  line-height: 1.65;
}

/* 选中态只改变颜色和背景，不改变文字尺寸/字重，避免切换时布局跳动。 */
.signal-card :deep(.el-radio-button__inner) {
  min-width: 96px;
  height: 32px;
  padding: 7px 15px;
  font-size: 12px;
  font-weight: 400;
  line-height: 16px;
}

/* 图片列表容器（images_json 渲染） */
.images-json-container {
  margin-top: 8px;
}

/* 截图说明 accordion 卡片 */
.screenshot-card {
  border: 1px solid #e4e7ed;
  border-left: 4px solid #909399;  /* 左侧彩色竖线，由 :style 覆盖 */
  border-radius: 4px;
  margin: 8px 0;
  overflow: hidden;
}

.screenshot-header {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 14px;
  cursor: pointer;
  user-select: none;
  transition: filter 0.15s;
}
.screenshot-header:hover {
  filter: brightness(0.97);
}

.screenshot-badge {
  display: inline-flex;
  align-items: center;
  gap: 4px;
  font-size: 12px;
  font-weight: 600;
  padding: 2px 8px;
  border: 1px solid currentColor;
  border-radius: 4px;
  white-space: nowrap;
}

.screenshot-intro-preview {
  flex: 1;
  font-size: 12px;
  color: #909399;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.toggle-arrow {
  font-size: 11px;
  color: #909399;
  margin-left: auto;
}

.screenshot-body {
  padding: 10px 16px 12px;
  background: #fff;
  border-top: 1px solid #ebeef5;
}

.ss-field {
  margin-bottom: 10px;
}
.ss-field:last-child {
  margin-bottom: 0;
}

.ss-field-label {
  font-size: 13px;
  color: #303133;
  margin-bottom: 4px;
}
.evidence-label-row {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
}

.ss-field-list {
  margin: 0 0 0 20px;
  padding: 0;
  list-style: disc;
}
.ss-field-list li {
  font-size: 13px;
  color: #606266;
  line-height: 1.7;
}
.evidence-context,
.evidence-provenance {
  padding: 8px 10px;
  border-radius: 4px;
  background: #f5f7fa;
  color: #606266;
  font-size: 12px;
  line-height: 1.7;
  overflow-wrap: anywhere;
}
.evidence-facts {
  border-left: 3px solid #67c23a;
  padding-left: 18px;
}
.evidence-inferences {
  padding: 8px 10px;
  border: 1px solid #f3d19e;
  border-radius: 4px;
  background: #fdf6ec;
}
.evidence-boundary-note {
  margin-top: 6px;
  color: #b88230;
  font-size: 12px;
  line-height: 1.6;
}
.evidence-provenance code {
  font-size: 11px;
  word-break: break-all;
}
.ss-empty {
  font-size: 13px;
  color: #c0c4cc;
  font-style: italic;
}
.ss-truncate-hint {
  font-size: 11px;
  color: #909399;
  font-weight: normal;
  margin-left: 6px;
}

/* 关键信号面板（QKV / QFK） */
.signal-group {
  margin-bottom: 14px;
}
.signal-group-title {
  font-size: 13px;
  font-weight: 600;
  color: #606266;
  margin: 6px 0 8px;
  padding-left: 8px;
  border-left: 3px solid #409eff;
}
.signal-card {
  border: 1px solid #ebeef5;
  border-radius: 6px;
  margin-bottom: 8px;
  background: #fafafa;
  scroll-margin-top: 28px;
  transition: border-color 0.2s ease, box-shadow 0.2s ease;
}
.signal-card.is-focused {
  border-color: #409eff;
  box-shadow: 0 0 0 3px rgba(64, 158, 255, 0.16);
}
.signal-card-head {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 6px 10px;
  border-bottom: 1px solid #ebeef5;
}
.signal-card-actions {
  display: flex;
  gap: 4px;
}
.signal-card-body {
  padding: 8px 10px;
}
.signal-evidence-details,
.signal-advanced-details {
  margin: 7px 0 2px;
  padding: 6px 8px;
  border: 1px solid #ebeef5;
  border-radius: 5px;
  background: #fff;
  color: #606266;
  font-size: 12px;
}
.signal-evidence-details summary,
.signal-advanced-details summary {
  cursor: pointer;
  color: #409eff;
  user-select: none;
}
.signal-advanced-details .field-hint {
  margin-left: 94px;
}
.signal-row {
  display: flex;
  align-items: flex-start;
  gap: 10px;
  padding: 3px 0;
  font-size: 13px;
}
.signal-k {
  flex: 0 0 84px;
  color: #909399;
}
.signal-v {
  flex: 1;
  color: #303133;
  word-break: break-all;
}
.signal-v.code {
  font-family: monospace;
  background: #f0f2f5;
  padding: 0 4px;
  border-radius: 3px;
}
.command-preview-row .signal-v {
  min-width: 0;
}
.command-preview-panel {
  margin-top: 5px;
  padding: 8px 10px;
  border: 1px solid #d9ecff;
  border-radius: 5px;
  background: #f4f9ff;
}
.command-preview-panel > code {
  display: block;
  overflow-x: auto;
  padding: 7px 9px;
  border-radius: 4px;
  background: #1f2937;
  color: #e5e7eb;
  font-family: ui-monospace, SFMono-Regular, Menlo, Monaco, Consolas, monospace;
  font-size: 12px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-all;
}
.command-preview-meta {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-top: 6px;
  color: #606266;
  font-size: 12px;
  flex-wrap: wrap;
}
.field-hint.command-preview-notice {
  margin: 6px 0 0;
}

/* 产出变量迷你编辑器 */
.produces-editor-mini {
  flex: 1;
  display: flex;
  flex-direction: column;
  gap: 6px;
}
.produce-item-mini {
  display: flex;
  align-items: center;
  gap: 8px;
}
.output-extract-card {
  align-items: flex-start;
  padding: 10px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  background: #fff;
}
.output-extract-grid {
  flex: 1;
  display: grid;
  grid-template-columns: 92px minmax(220px, 1fr);
  align-items: start;
  gap: 8px 10px;
}
.output-extract-grid > label {
  color: #606266;
  line-height: 28px;
}
.inline-controls,
.variable-chips,
.pipeline-warning .signal-v {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}
.pipeline-warning .signal-v {
  align-items: flex-start;
}
.pipeline-warning .el-alert {
  flex: 1;
}
.extract-advanced {
  color: #606266;
}
.extract-advanced summary {
  cursor: pointer;
  line-height: 28px;
}
.advanced-grid {
  display: grid;
  grid-template-columns: 100px minmax(180px, 1fr);
  align-items: center;
  gap: 8px;
  margin-top: 8px;
}
.muted {
  color: #909399;
}

/* 关键信号字段注释说明（实例 + 规则），对齐 MatcherEditor 视觉 */
.field-hint {
  font-size: 12px;
  color: #909399;
  line-height: 1.4;
  margin: 2px 0 6px 94px;
}

/* QKV 采集类型旁的「任务失败型/告警型」性质标注 */
.signal-nature {
  font-size: 12px;
  color: #909399;
  margin-left: 8px;
}

/* 关键字 × 分类基线 软校验命中时高亮为告警色 */
.field-hint.keyword-check.is-warn {
  color: #e6a23c;
  font-weight: 500;
}
</style>
