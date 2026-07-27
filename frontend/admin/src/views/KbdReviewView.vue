<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FullScreen, Refresh } from '@element-plus/icons-vue'
import { useCategories } from '../composables/useCategories'
import { marked } from 'marked'
import DOMPurify from 'dompurify'

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
}

// ============ 关键信号 v2 数据模型（RFC §7 前端原生读 v2 对象化，2026-07-22） ============
// GET 边界直接返回 v2 文档，前端不再归一/适配，直接基于该结构渲染与编辑；
// 回写时仍发回完整 v2 文档（{schema_version, signals}），后端 update_kbd_entry 幂等归约。
interface SignalV2 {
  id?: number | string
  acquire: { tool: string; args: Record<string, any> }
  match: { type?: string; pattern?: string; mode?: string; expected?: boolean } | null
  orchestrate: Record<string, any>
  provenance?: Record<string, any>
  review?: { require_human_confirm?: boolean }
}
interface SignalsDoc {
  schema_version: number
  signals: SignalV2[]
}

// 图片描述项（images_json 数组元素）
interface ImageJsonItem {
  seq: number           // 图片序号
  section: string       // 所属章节字段
  desc: string          // desc.txt v3 内容
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
  /** 截图类型（终端截图/日志截图/告警截图/任务截图/其他截图），后端 LLM 权威判断 */
  screenshotType: string
  /** PaddleOCR 全量文字行 */
  fullText: string[]
  /**
   * 截断后的可见内容（根据截图类型决定方向）：
   *   终端/日志截图 → FULL_TEXT 后 N 行（最新输出在末尾）
   *   告警/任务截图 → FULL_TEXT 前 N 行（最新内容在最前）
   */
  visibleContent: string[]
  /** 类型相关关键内容（KEY 字段）：终端→命令返回；日志→错误日志；告警→重要告警；任务→失败任务 */
  key: string[]
  /** 排障建议（TIPS 字段） */
  tips: string[]
  /** 语义描述（DESCRIPTION 字段，v3 格式）：Vision LLM 生成的图片语义摘要，供 RAG 召回 */
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
  expanded: boolean
}
const parsedImagesJson = ref<ParsedImageJson[]>([])

// 拒绝弹窗
const rejectDialogVisible = ref(false)
const rejectingEntry = ref<KbdEntry | null>(null)
const rejectNote = ref('')
const rejectLoading = ref(false)

// 审核人 ID（实际项目中应来自登录态，当前临时使用 1）
const currentUser = ref(1)

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
        review_note: entry.review_note || '',
        category_id: editableCategoryId.value || entry.ai_category_id || null,
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

function openRejectDialog(entry: KbdEntry) {
  rejectingEntry.value = entry
  rejectNote.value = ''
  rejectDialogVisible.value = true
}

async function submitReject() {
  if (!rejectingEntry.value) return
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
  cancelEditSignal()
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
}

// ──────────────────────────────────────────────────────────────────────────────
// 关键信号面板（signals_json）：基于 v2 文档直接渲染/编辑（RFC §7 前端原生读 v2 对象化）
// ──────────────────────────────────────────────────────────────────────────────
// v2 原生读取辅助：直接从 v2 结构各段取值，不拍平/不更名。
function sigTool(sig: SignalV2): string { return sig.acquire?.tool || '' }
function sigArgs(sig: SignalV2): Record<string, any> { return sig.acquire?.args || {} }
function sigMatch(sig: SignalV2): Record<string, any> { return sig.match || {} }
function sigOrch(sig: SignalV2): Record<string, any> { return sig.orchestrate || {} }
function isBackendSig(sig: SignalV2): boolean {
  return sigTool(sig).startsWith('qfk') || sig.provenance?.category === 'backend'
}

const signalList = computed<SignalV2[]>(() =>
  (detailEntry.value?.signals_json as SignalsDoc | undefined)?.signals || [],
)
const producerSignals = computed(() =>
  signalList.value.map((s, i) => ({ sig: s, origIdx: i })).filter((x) => !isBackendSig(x.sig)),
)
const consumerSignals = computed(() =>
  signalList.value.map((s, i) => ({ sig: s, origIdx: i })).filter((x) => isBackendSig(x.sig)),
)

// ── QKV 生产者关键字 × 分类基线 辅助（实例/注释/软校验）───────────────────────
// 分类基线（category_baseline.yaml, 198 类）按标签语义分两性：
//   · 任务失败型故障：标签多以 失败/卡住/异常/不达预期 结尾 → acli task/dialog get -k（qkv_task/qkv_dialog）
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
  for (const produce of sig.orchestrate?.produces || []) collect(produce?.extract || {})
  return [...found].sort()
}

function syncDraftRequires() {
  signalEditDraft.value.orchestrate = signalEditDraft.value.orchestrate || {}
  signalEditDraft.value.orchestrate.requires = deriveSignalRequires(signalEditDraft.value)
}

function produceOutputFormat(produce: any): 'json' | 'text' {
  return produce?.extract ? 'text' : 'json'
}

function normalizeTextExtract(produce: any) {
  if (!produce) return
  produce.type ??= 'string'
  if (!produce.extract) return
  produce.extract.type = 'text'
  produce.extract.column_mode ??= produce.extract.column ? 'index' : 'whole'
  produce.extract.include_mode ??= 'all'
  produce.extract.case_sensitive ??= true
  produce.extract.cardinality ??= 'exactly_one'
  produce.extract.source ??= 'stdout'
  produce.extract.delimiter ??= 'whitespace'
}

function setProduceOutputFormat(produce: any, format: 'json' | 'text') {
  if (format === 'text') {
    delete produce.path
    produce.extract = { type: 'text', column_mode: 'whole' }
    normalizeTextExtract(produce)
  } else {
    delete produce.extract
    produce.path = ''
  }
  syncDraftRequires()
}

function extractLinesText(produce: any, field: 'include' | 'exclude'): string {
  return Array.isArray(produce?.extract?.[field]) ? produce.extract[field].join('\n') : ''
}

function setExtractLines(produce: any, field: 'include' | 'exclude', value: string) {
  const lines = value.split('\n').map(item => item.trim()).filter(Boolean)
  if (lines.length) produce.extract[field] = lines
  else delete produce.extract[field]
  syncDraftRequires()
}

async function convertDraftPipeline() {
  const command = String(signalEditDraft.value.acquire?.args?.command || '')
  if (!command.includes('|')) return
  pipelineConvertLoading.value = true
  try {
    const resp = await fetch('/api/v1/kbd/tools/convert-safe-pipeline', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ command }),
    })
    const body = await resp.json().catch(() => ({}))
    if (!resp.ok) throw new Error(body?.detail || `HTTP ${resp.status}`)
    setQfkOutputMode('produces')
    const produce = signalEditDraft.value.orchestrate.produces[0]
    delete produce.path
    produce.extract = body.extract
    normalizeTextExtract(produce)
    signalEditDraft.value.acquire.args.command = body.command
    syncDraftRequires()
    const removed = Array.isArray(body.removed_segments) && body.removed_segments.length
      ? `；已移除：${body.removed_segments.join('、')}` : ''
    ElMessage.success(`已安全转换为“筛选行 + 提取值”${removed}`)
  } catch (error) {
    ElMessage.error(error instanceof Error ? error.message : '管道转换失败，请人工复核')
  } finally {
    pipelineConvertLoading.value = false
  }
}

function startEditSignal(origIdx: number) {
  const sig = signalList.value[origIdx]
  if (!sig) return
  editingSignalIndex.value = origIdx
  const draft: SignalV2 = JSON.parse(JSON.stringify(sig))
  // 确保嵌套对象存在，便于 v-model 直接绑定 v2 字段路径
  draft.acquire = draft.acquire || { tool: '', args: {} }
  draft.acquire.args = draft.acquire.args || {}
  // QFK 的“产出变量”模式必须保留 match=null，不能在进入编辑态时无条件补成关键字 matcher。
  if (!isBackendSig(draft) && !draft.match) {
    draft.match = { type: 'keyword', pattern: '', mode: 'or', expected: true }
  }
  draft.orchestrate = draft.orchestrate || {}
  for (const produce of draft.orchestrate.produces || []) normalizeTextExtract(produce)
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
    // 输出采集不做关键字判定，命令成功后把 stdout/JSON 路径结果写入变量池。
    draft.match = null
    if (!Array.isArray(draft.orchestrate.produces) || draft.orchestrate.produces.length === 0) {
      draft.orchestrate.produces = [{ name: '', type: 'string', path: '' }]
    }
    return
  }
  // 关键字判定不应残留输出变量，否则服务端会拒绝二义信号。
  draft.orchestrate.produces = []
  draft.match = { type: 'keyword', pattern: '', mode: 'or', expected: true }
}

function cancelEditSignal() {
  editingSignalIndex.value = null
  signalEditDraft.value = {
    acquire: { tool: '', args: {} },
    match: { type: 'keyword', pattern: '', mode: 'or', expected: true },
    orchestrate: {},
  }
}

async function saveSignalEdit() {
  if (editingSignalIndex.value === null || !detailEntry.value) return
  if (isBackendSig(signalEditDraft.value)) {
    const produces = signalEditDraft.value.orchestrate?.produces || []
    const hasProduces = produces.some((item: any) => String(item?.name || '').trim())
    const hasMatch = Boolean(signalEditDraft.value.match)
    if (hasProduces === hasMatch) {
      ElMessage.error('后端信号必须且只能选择“关键字判定”或“产出变量”之一')
      return
    }
    if (hasMatch && !String(signalEditDraft.value.match?.pattern || '').trim()) {
      ElMessage.error('请填写用于判定命令结果的关键字')
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
        if (item?.extract?.column_mode !== 'whole' && (!Number.isInteger(item?.extract?.column) || item.extract.column < 1)) {
          ElMessage.error(`产出变量 ${item.name} 的列号必须从 1 开始`)
          return
        }
      }
    }
    syncDraftRequires()
  }
  signalSaveLoading.value = true
  try {
    const list: SignalV2[] = JSON.parse(JSON.stringify(signalList.value))
    list[editingSignalIndex.value] = JSON.parse(JSON.stringify(signalEditDraft.value))
    // 回写完整 v2 文档（后端 update_kbd_entry 幂等归约），不再发扁平 list
    const payload: SignalsDoc = { schema_version: 2, signals: list }
    const resp = await fetch(`/api/v1/kbd/${detailEntry.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ signals_json: payload }),
    })
    if (!resp.ok) {
      let detail = `HTTP ${resp.status}`
      try {
        const errorBody = await resp.json()
        if (typeof errorBody?.detail === 'string' && errorBody.detail.trim()) {
          detail = errorBody.detail
        }
      } catch {
        // 非 JSON 错误响应继续使用 HTTP 状态码提示
      }
      throw new Error(detail)
    }
    const updated = (await resp.json()) as any
    const newDoc: SignalsDoc = updated?.signals_json || payload
    detailEntry.value.signals_json = newDoc
    const idx = entries.value.findIndex((e) => e.id === detailEntry.value!.id)
    if (idx !== -1) entries.value[idx].signals_json = newDoc
    ElMessage.success('关键信号已保存')
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
    const payload: Record<string, string> = {}
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

/** 匹配并关联文档段落中的截图与 parsedImagesJson 中的 seq 序号 */
function associateSegmentsWithSeq(segments: ContentSegment[], images: ParsedImageJson[]) {
  const matchedIndices = new Set<number>()
  segments.forEach(seg => {
    if (seg.type === 'screenshot') {
      // 优先匹配内容（DESCRIPTION / visibleContent）
      let matchIdx = images.findIndex((img, idx) => {
        if (matchedIndices.has(idx)) return false

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
      }
    }
  })
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
    const resp = await fetch(`/api/v1/kbd/${detailEntry.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ content_md: newContent }),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    // 同步更新本地状态
    detailEntry.value.content_md = newContent
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

    return {
      seq: img.seq,
      section: img.section,
      background,
      typeInfo,
      fullText,
      visibleContent,
      description: description || '（无描述）',
      expanded: false,
    }
  }).sort((a, b) => a.seq - b.seq)
}

const metaKeys: (keyof KbdMetadata)[] = [
  'sangfor_main_module', 'sangfor_sub_module', 'suite_version',
  'sangfor_updated_at', 'sangfor_created_at',
  'create_admin_id', 'update_admin_id',
]

onMounted(() => {
  fetchPending()
  fetchCategories()
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
              type="warning"
              size="small"
              style="margin-left: 8px;"
              :loading="reclassifyLoading === detailEntry.id"
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
              <span class="section-hint">占位符统一为 &#123;&#123;VAR&#125;&#125; 大写；每条可编辑后 PATCH 回写</span>
              <el-button
                type="warning"
                size="small"
                :loading="reextractSignalsLoading === detailEntry.id"
                @click="handleReextractSignals(detailEntry)"
                title="用最新 Prompt 重新抽取关键信号抽取"
              >
                <el-icon style="font-size: 14px;"><Refresh /></el-icon>
                重新抽取
              </el-button>
            </div>
          </div>

          <!-- 生产者信号（QKV） -->
          <div class="signal-group">
            <div class="signal-group-title">生产者信号（QKV：前端采集，写入变量池）</div>
            <el-empty v-if="producerSignals.length === 0" description="暂无生产者信号" :image-size="44" />
            <div v-for="item in producerSignals" :key="'p-' + item.origIdx" class="signal-card">
              <div class="signal-card-head">
                <el-tag size="small" type="success">{{ sigTool(item.sig) || 'qkv' }}</el-tag>
                <div class="signal-card-actions">
                  <el-button text size="small" @click="goToToolManage(sigTool(item.sig))">工具管理</el-button>
                  <el-button v-if="editingSignalIndex !== item.origIdx" text type="primary" size="small" @click="startEditSignal(item.origIdx)">编辑</el-button>
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
                </div>
                <div v-else>
                  <div class="signal-row"><span class="signal-k">说明</span><el-input v-model="signalEditDraft.acquire.args.instruction" size="small" type="textarea" :rows="2" placeholder="信号说明，如 镜像文件占用检查" /></div>
                  <div class="field-hint">信号语义说明：用自然语言描述这个采集做什么（如「镜像文件占用检查」），是人类可读标题，不是匹配条件</div>
                  <div class="signal-row"><span class="signal-k">采集类型</span><span class="signal-v code">{{ sigTool(signalEditDraft) || 'qkv' }}</span><span class="signal-nature">{{ qkvNatureLabel(sigTool(signalEditDraft)) }}</span></div>
                  <div class="signal-row"><span class="signal-k">关键字</span><el-input v-model="signalEditDraft.acquire.args.keyword" size="small" :placeholder="qkvKeywordPlaceholder(sigTool(signalEditDraft))" /></div>
                  <div v-if="sigTool(signalEditDraft) === 'qkv_alert'" class="field-hint">告警型关键字（acli alert get -k）：取自「分类基线 · 告警型故障」（标签以「告警」结尾），如 虚拟机CPU或内存占用过高告警、主机网口丢包告警、序列号过期告警。多个用逗号分隔</div>
                  <div v-else-if="sigTool(signalEditDraft) === 'qkv_task'" class="field-hint">任务失败型关键字（acli task get -k）：取自「分类基线 · 任务失败型故障」，如 虚拟机开机失败、虚拟机快照失败、虚拟机scmt迁移失败。多个用逗号分隔</div>
                  <div v-else-if="sigTool(signalEditDraft) === 'qkv_dialog'" class="field-hint">任务失败型弹框关键字（acli dialog get -k）：取自「分类基线 · 任务失败型故障」，如 虚拟机创建失败、磁盘替换失败、版本升级失败。多个用逗号分隔</div>
                  <div v-else class="field-hint">前端采集匹配关键字（acli &lt;task|dialog|alert&gt; get -k）：取自「分类基线」标签。多个用逗号分隔</div>
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
            <div v-for="item in consumerSignals" :key="'c-' + item.origIdx" class="signal-card">
              <div class="signal-card-head">
                <el-tag size="small" type="warning">{{ sigTool(item.sig) || 'qfk' }}</el-tag>
                <div class="signal-card-actions">
                  <el-button text size="small" @click="goToToolManage(sigTool(item.sig))">工具管理</el-button>
                  <el-button v-if="editingSignalIndex !== item.origIdx" text type="primary" size="small" @click="startEditSignal(item.origIdx)">编辑</el-button>
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
                    <div class="signal-row"><span class="signal-k">容器</span><span class="signal-v">{{ sigArgs(item.sig).container || 'asv-con' }}</span></div>
                    <div class="signal-row"><span class="signal-k">执行命令</span><span class="signal-v code">{{ sigArgs(item.sig).command || '—' }}</span></div>
                  </template>
                  <template v-if="sigTool(item.sig) === 'qfk_service'">
                    <div class="signal-row"><span class="signal-k">容器</span><span class="signal-v">{{ sigArgs(item.sig).container || 'asv' }}</span></div>
                    <div class="signal-row"><span class="signal-k">执行命令</span><span class="signal-v">{{ sigArgs(item.sig).command || sigOrch(item.sig).action || 'status' }}</span></div>
                  </template>
                  <template v-if="['qfk_vm', 'qfk_network', 'qfk_storage', 'qfk_hardware', 'qfk_platform'].includes(sigTool(item.sig))">
                    <div class="signal-row"><span class="signal-k">执行命令</span><span class="signal-v code">{{ sigArgs(item.sig).command || '—' }}</span></div>
                  </template>
                  <div class="signal-row"><span class="signal-k">输入变量</span><span class="signal-v code">{{ (sigOrch(item.sig).requires || []).join('、') || '—' }}</span></div>
                  <div class="signal-row"><span class="signal-k">超时时间</span><span class="signal-v">{{ sigArgs(item.sig).timeout || 10 }}s</span></div>
                  <div class="signal-row"><span class="signal-k">执行模式</span><span class="signal-v">{{ qfkOutputMode(item.sig) === 'produces' ? '产出变量（采集命令结果）' : '关键字判定' }}</span></div>
                  <template v-if="qfkOutputMode(item.sig) === 'produces'">
                    <div v-for="(p, idx) in (sigOrch(item.sig).produces || [])" :key="`output-${idx}`" class="signal-row">
                      <span class="signal-k">{{ idx === 0 ? '产出变量' : '' }}</span>
                      <span class="signal-v code">
                        {{ p.name || '—' }}（{{ p.type || 'string' }} / {{ p.extract ? '文本' : 'JSON' }}）
                        <template v-if="p.extract">
                          · 包含 {{ (p.extract.include || []).join(' + ') || '全部行' }}
                          · {{ p.extract.column_mode === 'index' ? `第 ${p.extract.column} 列` : p.extract.column_mode === 'from_index' ? `从第 ${p.extract.column} 列到末尾` : '整行' }}
                        </template>
                        <template v-else>· path={{ p.path || '完整 stdout' }}</template>
                      </span>
                    </div>
                  </template>
                  <template v-else>
                    <div class="signal-row"><span class="signal-k">关键字</span><span class="signal-v">{{ sigMatch(item.sig).pattern || '—' }}</span></div>
                    <div class="signal-row"><span class="signal-k">期望</span><span class="signal-v">{{ sigMatch(item.sig).expected === true ? '存在' : sigMatch(item.sig).expected === false ? '不存在' : '—' }}</span></div>
                    <div class="signal-row"><span class="signal-k">匹配模式</span><span class="signal-v">{{ sigMatch(item.sig).mode || 'or' }}</span></div>
                  </template>

                  <!-- 其他工具特有字段 -->
                  <div v-if="sigTool(item.sig) === 'qfk_system' && sigArgs(item.sig).resource_keyword" class="signal-row"><span class="signal-k">命令参数</span><span class="signal-v code">{{ sigArgs(item.sig).resource_keyword }}</span></div>
                  <template v-if="sigTool(item.sig) === 'qfk_log'">
                    <div class="signal-row"><span class="signal-k">文件</span><span class="signal-v code">{{ sigArgs(item.sig).file || '—' }}</span></div>
                    <div class="signal-row"><span class="signal-k">结束时间</span><span class="signal-v">{{ sigArgs(item.sig).time_window || '—' }}</span></div>
                  </template>
                  <template v-if="sigTool(item.sig) === 'qfk_service'">
                    <div class="signal-row"><span class="signal-k">服务</span><span class="signal-v code">{{ sigArgs(item.sig).resource_keyword || '—' }}</span></div>
                  </template>
                </div>

                <!-- 编辑模式 -->
                <div v-else>
                  <!-- 共有字段 -->
                  <div class="signal-row"><span class="signal-k">说明</span><el-input v-model="signalEditDraft.acquire.args.instruction" size="small" placeholder="信号说明，如 镜像文件占用检查" /></div>
                  <div class="field-hint">信号语义说明：用自然语言描述这个检查/采集做什么（如「镜像文件占用检查」），是人类可读标题，不是匹配条件</div>
                  <div class="signal-row"><span class="signal-k">主机</span><el-input v-model="signalEditDraft.acquire.args.host" size="small" placeholder="{{HOST}} 或 cluster" /></div>
                  <div class="field-hint" v-pre>采集目标主机，使用变量池占位符 {{HOST}}（由上游生产者信号产出）或固定值 cluster</div>
                  <!-- 容器与执行命令：位于输入/输出契约之前，先明确命令在哪里、执行什么。 -->
                  <template v-if="sigTool(signalEditDraft) === 'qfk_system'">
                    <div class="signal-row"><span class="signal-k">容器</span>
                      <el-select v-model="signalEditDraft.acquire.args.container" size="small">
                        <el-option label="host（宿主机，不进入容器）" value="host" />
                        <el-option label="asv-con" value="asv-con" />
                        <el-option label="vn-con" value="vn-con" />
                        <el-option label="vn-agent" value="vn-agent" />
                        <el-option label="vs-cp-manager" value="vs-cp-manager" />
                      </el-select>
                    </div>
                    <div class="signal-row"><span class="signal-k">执行命令</span><el-input v-model="signalEditDraft.acquire.args.command" size="small" placeholder="执行命令（必填，不含 acli system 前缀）" /></div>
                    <div v-if="String(signalEditDraft.acquire.args.command || '').includes('|')" class="signal-row pipeline-warning">
                      <span class="signal-k"></span>
                      <div class="signal-v">
                        <el-alert title="检测到 Shell 管道，不能直接保存" type="warning" :closable="false" show-icon />
                        <el-button type="primary" size="small" :loading="pipelineConvertLoading" @click="convertDraftPipeline">安全转换管道</el-button>
                      </div>
                    </div>
                    <div class="field-hint">宿主机只执行基础命令；grep/awk/cut 的安全子集由平台转换为内存中的“筛选行 + 提取值”。</div>
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
                      <el-radio-button label="keyword">关键字判定</el-radio-button>
                      <el-radio-button label="produces">产出变量</el-radio-button>
                    </el-radio-group>
                  </div>
                  <div class="field-hint">二选一：关键字模式判断命令结果；产出变量模式把命令结果写入变量池，供后续信号使用。</div>
                  <template v-if="qfkOutputMode(signalEditDraft) === 'keyword' && signalEditDraft.match">
                    <div class="signal-row"><span class="signal-k">关键字</span><el-input v-model="signalEditDraft.match.pattern" size="small" placeholder="检查命令结果是否包含的关键字" /></div>
                    <div class="signal-row"><span class="signal-k">期望</span>
                      <el-switch v-model="signalEditDraft.match.expected" :active-value="true" :inactive-value="false" active-text="存在" inactive-text="不存在" />
                    </div>
                    <div class="signal-row"><span class="signal-k">匹配模式</span>
                      <el-select v-model="signalEditDraft.match.mode" size="small">
                        <el-option label="or（任一匹配）" value="or" />
                        <el-option label="and（全部匹配）" value="and" />
                        <el-option label="not（均不出现）" value="not" />
                      </el-select>
                    </div>
                    <div class="field-hint">or 任一出现即命中，and 全部出现才命中，not 均不出现才命中。</div>
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
                            </el-select>
                            <label>输出格式</label>
                            <el-radio-group :model-value="produceOutputFormat(p)" size="small" @change="(value: any) => setProduceOutputFormat(p, value)">
                              <el-radio-button label="json">JSON</el-radio-button>
                              <el-radio-button label="text">文本</el-radio-button>
                            </el-radio-group>
                            <template v-if="produceOutputFormat(p) === 'json'">
                              <label>取值路径</label>
                              <el-input v-model="p.path" size="small" placeholder="如 data.0.pid；空=完整 stdout" />
                            </template>
                            <template v-else>
                              <label>筛选行（包含）</label>
                              <el-input :model-value="extractLinesText(p, 'include')" type="textarea" :rows="2" placeholder="每行一个条件；多条件默认同时满足" @input="(value: string) => setExtractLines(p, 'include', value)" />
                              <label>筛选行（不包含）</label>
                              <el-input :model-value="extractLinesText(p, 'exclude')" type="textarea" :rows="2" placeholder="可选，每行一个排除条件" @input="(value: string) => setExtractLines(p, 'exclude', value)" />
                              <label>提取值</label>
                              <div class="inline-controls">
                                <el-select v-model="p.extract.column_mode" size="small">
                                  <el-option label="整行" value="whole" />
                                  <el-option label="第 N 列" value="index" />
                                  <el-option label="从第 N 列到末尾" value="from_index" />
                                </el-select>
                                <el-input-number v-if="p.extract.column_mode !== 'whole'" v-model="p.extract.column" :min="1" :max="999" size="small" />
                              </div>
                              <label>高级设置</label>
                              <details class="extract-advanced">
                                <summary>默认：空白分隔、区分大小写、唯一匹配、stdout</summary>
                                <div class="advanced-grid">
                                  <span>包含关系</span><el-select v-model="p.extract.include_mode" size="small"><el-option label="全部满足（AND）" value="all" /><el-option label="任一满足（OR）" value="any" /></el-select>
                                  <span>区分大小写</span><el-switch v-model="p.extract.case_sensitive" />
                                  <span>匹配数量</span><el-select v-model="p.extract.cardinality" size="small"><el-option label="必须唯一" value="exactly_one" /><el-option label="第一行" value="first" /><el-option label="最后一行" value="last" /><el-option label="全部行" value="all" /></el-select>
                                  <span>输出来源</span><el-select v-model="p.extract.source" size="small"><el-option label="stdout" value="stdout" /><el-option label="stderr" value="stderr" /></el-select>
                                  <span>分隔符</span><el-input v-model="p.extract.delimiter" size="small" placeholder="whitespace 或单字符" />
                                </div>
                              </details>
                            </template>
                          </div>
                          <el-button text type="danger" size="small" @click="signalEditDraft.orchestrate.produces?.splice(idx, 1)">删除变量</el-button>
                        </div>
                        <el-button text type="primary" size="small" @click="signalEditDraft.orchestrate.produces = [...(signalEditDraft.orchestrate.produces || []), { name: '', type: 'string', path: '' }]">+ 添加变量</el-button>
                      </div>
                    </div>
                    <div class="field-hint">JSON 使用 path；文本使用“筛选行 + 提取值”。列号从 1 开始，等价于安全的 <code>awk '{print $N}'</code>，但不会执行 awk。</div>
                  </template>

                  <!-- 其他工具特有字段 -->
                  <template v-if="sigTool(signalEditDraft) === 'qfk_system'">
                    <div class="signal-row"><span class="signal-k">命令参数</span><el-input v-model="signalEditDraft.acquire.args.resource_keyword" size="small" placeholder="可选，如 {{VM}}、{{PID}}、设备名" /></div>
                    <div class="field-hint">作为一个安全参数追加到执行命令，例如 <code v-pre>lsof + {{VM}}</code>；完整命令已经包含参数时可留空。它不参与结果匹配。</div>
                  </template>
                  <template v-if="sigTool(signalEditDraft) === 'qfk_log'">
                    <div class="signal-row"><span class="signal-k">文件</span><el-input v-model="signalEditDraft.acquire.args.file" size="small" placeholder="日志文件/来源，如 /var/log/messages、dmesg" /></div>
                    <div class="field-hint">日志文件名或来源；支持路径，留空则取默认日志。</div>
                    <div class="signal-row"><span class="signal-k">结束时间</span><el-input v-model="signalEditDraft.acquire.args.time_window" size="small" placeholder="结束时间窗，如 now/-1h 或 2026-07-23T10:00" /></div>
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
                v-if="!editingContent"
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
                    type="warning"
                    size="small"
                    style="margin-right: 8px;"
                    :loading="reanalyzeSingleLoading?.kbdId === detailEntry.id && reanalyzeSingleLoading?.seq === seg.seq"
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
                  <!-- 2. 语义描述（v3 DESCRIPTION）或类型关键内容（v2 KEY）-->
                  <div class="ss-field">
                    <div class="ss-field-label">2. <strong>{{ seg.fields.description ? '语义描述' : seg.errorLabel }}</strong></div>
                    <p v-if="seg.fields.description" class="ss-description">{{ seg.fields.description }}</p>
                    <ul v-else-if="seg.fields.key.length" class="ss-field-list">
                      <li v-for="(item, j) in seg.fields.key" :key="j">{{ item }}</li>
                    </ul>
                    <span v-else class="ss-empty">无</span>
                  </div>
                  <!-- 3. 排障建议（v2 TIPS，v3 无此字段时隐藏）-->
                  <div v-if="!seg.fields.description || seg.fields.tips.length" class="ss-field">
                    <div class="ss-field-label">3. <strong>排障建议</strong></div>
                    <ul v-if="seg.fields.tips.length" class="ss-field-list">
                      <li v-for="(item, j) in seg.fields.tips" :key="j">{{ item }}</li>
                    </ul>
                    <span v-else class="ss-empty">无</span>
                  </div>
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
                  <el-button
                    type="warning"
                    size="small"
                    style="margin-right: 8px;"
                    :loading="reanalyzeSingleLoading?.kbdId === detailEntry.id && reanalyzeSingleLoading?.seq === img.seq"
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
                </div>
              </div>
            </template>
          </div>
        </div>

        <!-- 审核备注 -->
        <div class="section-block">
          <h4 class="section-title">审核备注</h4>
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
