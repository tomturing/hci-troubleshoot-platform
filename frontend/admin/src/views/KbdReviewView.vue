<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

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
  support_url: string
  title: string
  content_md: string
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
}

interface PendingKbdResponse {
  entries: KbdEntry[]
  total: number
  page: number
  page_size: number
}

// 分类基线选项
interface CategoryOption {
  code: string          // "虚拟机-001"
  name: string          // "虚拟机创建失败"
  domain: string        // "虚拟机"
  path_labels: string[]
}

// 截图说明解析类型
interface ScreenshotTypeInfo {
  label: string   // "告警截图"
  color: string   // 前景色
  bgColor: string // 背景色
  icon: string    // emoji 图标
}

interface ScreenshotFields {
  intro: string
  bgColorText: string    // Vision 字段0：截图背景颜色（黑色/白色/其他）
  typeName: string       // Vision 字段1：界面类型（参考用，不作主要分类依据）
  visibleContent: string[]
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
}

type ContentSegment = NormalSegment | ScreenshotSegment

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态
// ──────────────────────────────────────────────────────────────────────────────
const loading = ref(false)
const entries = ref<KbdEntry[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const categoryFilter = ref('')
const statusFilter = ref('draft')

// 分类基线（用于 select）
const categoriesLoading = ref(false)
const categoryOptions = ref<CategoryOption[]>([])

// 详情弹窗
const detailDialogVisible = ref(false)
const detailEntry = ref<KbdEntry | null>(null)
const reviewNote = ref('')
const editableCategoryId = ref('')

// 详情弹窗 — 内容内联编辑
const editingContent = ref(false)
const inlineContent = ref('')
const inlineEditLoading = ref(false)
const parsedSegments = ref<ContentSegment[]>([])

// 拒绝弹窗
const rejectDialogVisible = ref(false)
const rejectingEntry = ref<KbdEntry | null>(null)
const rejectNote = ref('')
const rejectLoading = ref(false)

// 审核人 ID（实际项目中应来自登录态）
const currentUser = ref('admin')

// 编辑弹窗
const editDialogVisible = ref(false)
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
      page: String(page.value),
      page_size: String(pageSize.value),
      status: statusFilter.value,
    })
    if (categoryFilter.value) {
      params.append('category_id', categoryFilter.value)
    }
    const resp = await fetch(`/api/v1/kbd/pending?${params}`, {
      headers: authHeader,
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data: PendingKbdResponse = await resp.json()
    entries.value = data.entries
    total.value = data.total
  } catch {
    ElMessage.error('加载 KBD 条目失败，请刷新重试')
  } finally {
    loading.value = false
  }
}

// 加载分类基线（用于 select 选项）
async function fetchCategories() {
  categoriesLoading.value = true
  try {
    const resp = await fetch('/api/kb/categories?grouped=true', { headers: authHeader })
    if (!resp.ok) return
    const data: Record<string, CategoryOption[]> = await resp.json()
    categoryOptions.value = Object.values(data).flat().sort((a, b) => a.code.localeCompare(b.code))
  } catch { /* 分类加载失败时仍允许手动输入 */ } finally {
    categoriesLoading.value = false
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
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('审核通过，KBD 条目已发布')
    entries.value = entries.value.filter((e) => e.id !== entry.id)
    total.value -= 1
    detailDialogVisible.value = false
  } catch (e: unknown) {
    if ((e as { message?: string })?.message !== 'cancel') {
      ElMessage.error('操作失败，请重试')
    }
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

function openDetailDialog(entry: KbdEntry) {
  detailEntry.value = entry
  reviewNote.value = entry.review_note || ''
  editableCategoryId.value = entry.category_id || entry.ai_category_id || ''
  editingContent.value = false
  inlineContent.value = entry.content_md || ''
  parsedSegments.value = parseContentMd(entry.content_md || '')
  detailDialogVisible.value = true
}

function handlePageChange(newPage: number) {
  page.value = newPage
  fetchPending()
}

function openEditDialog(entry: KbdEntry) {
  editingEntry.value = entry
  editTitle.value = entry.title
  editContent.value = entry.content_md || ''
  editCategoryId.value = entry.category_id || entry.ai_category_id || ''
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
    // 更新本地数据
    const idx = entries.value.findIndex((e) => e.id === editingEntry.value!.id)
    if (idx !== -1) {
      if (payload.title) entries.value[idx].title = payload.title
      if (payload.content_md !== undefined) entries.value[idx].content_md = payload.content_md
      if (payload.category_id !== undefined) entries.value[idx].category_id = payload.category_id
    }
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    editLoading.value = false
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
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('重新发布成功')
    entries.value = entries.value.filter((e) => e.id !== entry.id)
    total.value -= 1
    detailDialogVisible.value = false
  } catch (e: unknown) {
    if ((e as { message?: string })?.message !== 'cancel') {
      ElMessage.error('操作失败，请重试')
    }
  }
}

// ──────────────────────────────────────────────────────────────────────────────
// Markdown 渲染（修复 ol/ul 状态混乱 + 缩进子列表）
// ──────────────────────────────────────────────────────────────────────────────
function renderMarkdown(md: string): string {
  if (!md) return ''
  const lines = md.split('\n')
  const html: string[] = []
  let listType: 'none' | 'ul' | 'ol' = 'none'
  let inBlockquote = false

  const flushList = () => {
    if (listType === 'ul') { html.push('</ul>'); listType = 'none' }
    else if (listType === 'ol') { html.push('</ol>'); listType = 'none' }
  }
  const flushBlockquote = () => {
    if (inBlockquote) { html.push('</blockquote>'); inBlockquote = false }
  }

  for (const line of lines) {
    if (line.startsWith('## ')) {
      flushList(); flushBlockquote()
      html.push(`<h3 class="md-h2">${escapeHtml(line.slice(3))}</h3>`)
      continue
    }
    if (line.startsWith('### ')) {
      flushList(); flushBlockquote()
      html.push(`<h4 class="md-h3">${escapeHtml(line.slice(4))}</h4>`)
      continue
    }
    if (line.startsWith('> ')) {
      flushList()
      if (!inBlockquote) { html.push('<blockquote class="md-blockquote">'); inBlockquote = true }
      html.push(`<p>${inlineRender(line.slice(2))}</p>`)
      continue
    }
    // 无序列表（含 2+ 空格缩进的子项）
    const ulMatch = line.match(/^(\s*)[-*]\s+(.+)$/)
    if (ulMatch) {
      flushBlockquote()
      if (listType !== 'ul') { flushList(); html.push('<ul class="md-list">'); listType = 'ul' }
      const indentPx = ulMatch[1].length * 10
      const style = indentPx > 0 ? ` style="margin-left:${indentPx}px"` : ''
      html.push(`<li${style}>${inlineRender(ulMatch[2])}</li>`)
      continue
    }
    // 有序列表（中文 1、 或英文 1. 开头）
    const olMatch = line.match(/^(\s*)\d+[.、]\s+(.+)$/)
    if (olMatch) {
      flushBlockquote()
      if (listType !== 'ol') { flushList(); html.push('<ol class="md-list">'); listType = 'ol' }
      html.push(`<li>${inlineRender(olMatch[2])}</li>`)
      continue
    }
    flushList(); flushBlockquote()
    if (line.trim() === '') {
      // 跳过多余空行，段间距由 CSS 控制
    } else {
      html.push(`<p class="md-p">${inlineRender(line)}</p>`)
    }
  }
  flushList(); flushBlockquote()
  return html.join('\n')
}

function inlineRender(text: string): string {
  return escapeHtml(text)
    .replace(/`([^`]+)`/g, '<code>$1</code>')
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/\*([^*]+)\*/g, '<em>$1</em>')
}

function escapeHtml(s: string): string {
  return s
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
}

// ──────────────────────────────────────────────────────────────────────────────
// 截图说明解析（accordion 卡片）
// ──────────────────────────────────────────────────────────────────────────────

/**
 * 截图多维度分类（优先级由高到低）
 * 判断依据：Vision 字段0（背景色）+ visibleContent + errorContent 全文关键词
 *
 * 1. 白色背景 + 含「紧急/普通/历史告警数/未处理」   → 告警截图
 * 2. 白色背景 + 含任务表格字段 或 中文状态词        → 任务截图
 * 3. 黑色背景 + 含 Shell 命令特征                → 终端截图
 * 4. 黑色背景 + Vision 标注「日志」或含时间戳格式   → 日志截图
 * 5. 其他                                        → 其他截图
 */
function detectScreenshotType(fields: ScreenshotFields): ScreenshotTypeInfo {
  const isBlack = /黑/.test(fields.bgColorText)
  const isWhite = /白/.test(fields.bgColorText)
  // 将可见内容和报错内容合并为全文便于关键词搜索
  const fullText = [...fields.visibleContent, ...fields.errorContent].join(' ')

  // 告警截图：白色背景且含告警级别词
  if (isWhite && /紧急|普通|历史告警数|未处理/.test(fullText))
    return { label: '告警截图', color: '#E6A23C', bgColor: '#FEF7EC', icon: '🔔' }

  // 任务截图：白色背景且含任务表格字段或任务状态词
  if (isWhite && (/操作人|对象类型|行为|开始时间|结束时间/.test(fullText)
    || /完成|失败|进行中/.test(fullText)))
    return { label: '任务截图', color: '#409EFF', bgColor: '#EEF6FF', icon: '📋' }

  // 终端截图：黑色背景且含 Shell 命令特征
  if (isBlack && /\$\s|#\s|sudo|chmod|\/var\/|\/etc\/|0x[0-9a-fA-F]/.test(fullText))
    return { label: '终端截图', color: '#722ED1', bgColor: '#F5EEFF', icon: '💻' }

  // 日志截图：黑色背景且 Vision 界面类型含「日志」或全文含时间戳格式日志行
  if (isBlack && (/日志/.test(fields.typeName) || /\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2}/.test(fullText)))
    return { label: '日志截图', color: '#67C23A', bgColor: '#F0F9EB', icon: '📄' }

  // 兜底：其他截图
  return { label: '其他截图', color: '#909399', bgColor: '#F5F7FA', icon: '🖼️' }
}

/** 根据报错内容决定字段标签名 */
function detectErrorLabel(items: string[]): string {
  const joined = items.join(' ')
  if (/告警/.test(joined)) return '告警信息'
  if (/红框|标注/.test(joined)) return '红框标注'
  if (/失败|任务/.test(joined)) return '失败任务'
  return '报错日志'
}

/** 将截图说明行组解析为 ScreenshotSegment */
function parseScreenshotBlock(lines: string[]): ScreenshotSegment {
  // 第一行: > **【截图说明】**：[可能直接是字段0内容]
  const introLine = lines[0] || ''
  const introRaw = introLine.replace(/^>\s*\*\*【截图说明】\*\*[：:]\s*/, '').trim()

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

  const fields: ScreenshotFields = { intro, bgColorText, typeName, visibleContent, errorContent, techTips }
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
 */
function parseContentMd(md: string): ContentSegment[] {
  if (!md) return []
  const lines = md.split('\n')
  const segments: ContentSegment[] = []
  let normalLines: string[] = []
  let screenshotLines: string[] = []
  let inScreenshot = false

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
    const isScreenshotStart = line.startsWith('> ') && line.includes('【截图说明】')

    if (isScreenshotStart) {
      flushNormal()
      flushScreenshot()
      inScreenshot = true
      screenshotLines = [line]
      continue
    }

    if (inScreenshot) {
      const trimmed = line.trim()
      // 截图块内的行：空行、缩进子项、以 "1. **" 开头的字段行、普通 bullet
      const isFieldLine = /^\d+[.、]\s+\*\*/.test(trimmed)
      const isBulletLine = /^-\s/.test(trimmed) || /^\s{2,}-\s/.test(line)
      const isBlank = trimmed === ''
      // 检测截图块结束：有内容的非截图行
      const isEndLine = !isBlank && !isFieldLine && !isBulletLine && !/^\d+\.\s+\*\*/.test(trimmed)

      if (isEndLine && screenshotLines.length > 1) {
        flushScreenshot()
        inScreenshot = false
        normalLines.push(line)
      } else {
        screenshotLines.push(line)
      }
    } else {
      normalLines.push(line)
    }
  }
  flushNormal()
  flushScreenshot()
  return segments
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
        <el-col :span="6">
          <el-input
            v-model="categoryFilter"
            placeholder="按 AI 分类 ID 筛选（如 虚拟机-001）"
            clearable
            @clear="fetchPending"
            @keyup.enter="fetchPending"
          />
        </el-col>
        <el-col :span="5">
          <el-select v-model="statusFilter" @change="fetchPending" style="width: 100%">
            <el-option label="待审核 (draft)" value="draft" />
            <el-option label="已发布 (published)" value="published" />
            <el-option label="已拒绝 (rejected)" value="rejected" />
            <el-option label="已归档 (archived)" value="archived" />
          </el-select>
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="fetchPending">搜索</el-button>
          <el-button @click="categoryFilter = ''; statusFilter = 'draft'; fetchPending()">重置</el-button>
        </el-col>
        <el-col :span="9" class="total-info">
          <span>共 <strong>{{ total }}</strong> 条</span>
        </el-col>
      </el-row>
    </el-card>

    <!-- 列表 -->
    <el-card v-loading="loading" shadow="never" class="table-card">
      <el-table :data="entries" row-key="id" style="width: 100%">
        <!-- 案例 ID -->
        <el-table-column label="案例 ID" width="100">
          <template #default="{ row }">
            <a :href="row.support_url" target="_blank" class="support-link">
              {{ row.support_id }}
            </a>
          </template>
        </el-table-column>

        <!-- 标题 -->
        <el-table-column label="标题" min-width="300">
          <template #default="{ row }">
            <span class="entry-title">{{ row.title }}</span>
          </template>
        </el-table-column>

        <!-- AI 分类 -->
        <el-table-column label="AI 分类" width="140">
          <template #default="{ row }">
            <el-tooltip v-if="row.ai_category_reason" :content="row.ai_category_reason" placement="top">
              <span class="category-tag">{{ row.ai_category_label || row.ai_category_id || '—' }}</span>
            </el-tooltip>
            <span v-else class="category-tag">{{ row.ai_category_label || row.ai_category_id || '—' }}</span>
          </template>
        </el-table-column>

        <!-- 置信度 -->
        <el-table-column label="置信度" width="90" align="center">
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
        <el-table-column label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag
              :type="row.status === 'published' ? 'success' :
                     row.status === 'rejected'  ? 'danger'  :
                     row.status === 'archived'  ? 'info'    : 'warning'"
              size="small"
            >{{ row.status }}</el-tag>
          </template>
        </el-table-column>

        <!-- 导入时间 -->
        <el-table-column label="导入时间" width="150">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>

        <!-- 操作 -->
        <el-table-column label="操作" width="220" fixed="right">
          <template #default="{ row }">
            <el-button type="info" size="small" text @click="openDetailDialog(row)">详情</el-button>
            <el-button type="primary" size="small" text @click="openEditDialog(row)">编辑</el-button>
            <template v-if="row.status === 'draft'">
              <el-button type="success" size="small" text @click="handleApprove(row)">通过</el-button>
              <el-button type="danger" size="small" text @click="openRejectDialog(row)">拒绝</el-button>
            </template>
            <template v-else-if="row.status === 'rejected'">
              <el-button type="warning" size="small" text @click="handleRepublish(row)">重新发布</el-button>
            </template>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-wrapper">
        <el-pagination
          background
          layout="total, prev, pager, next"
          :total="total"
          :page-size="pageSize"
          :current-page="page"
          @current-change="handlePageChange"
        />
      </div>
    </el-card>

    <!-- 详情弹窗 -->
    <el-dialog
      v-model="detailDialogVisible"
      title="KBD 条目详情"
      width="860px"
      top="4vh"
      :close-on-click-modal="false"
    >
      <template v-if="detailEntry">
        <!-- 基本信息 -->
        <el-descriptions :column="2" border size="small">
          <el-descriptions-item label="案例 ID">
            <a :href="detailEntry.support_url" target="_blank" class="support-link">
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
                  <span class="toggle-arrow">{{ seg.expanded ? '▲' : '▼' }}</span>
                </div>

                <!-- 展开内容 -->
                <div v-if="seg.expanded" class="screenshot-body">
                  <!-- 1. 可见内容 -->
                  <div v-if="seg.fields.visibleContent.length" class="ss-field">
                    <div class="ss-field-label">1. <strong>可见内容</strong></div>
                    <ul class="ss-field-list">
                      <li v-for="(item, j) in seg.fields.visibleContent" :key="j">{{ item }}</li>
                    </ul>
                  </div>
                  <!-- 2. 报错/状态（标签动态选择）-->
                  <div class="ss-field">
                    <div class="ss-field-label">2. <strong>{{ seg.errorLabel }}</strong></div>
                    <ul v-if="seg.fields.errorContent.length" class="ss-field-list">
                      <li v-for="(item, j) in seg.fields.errorContent" :key="j">{{ item }}</li>
                    </ul>
                    <span v-else class="ss-empty">无</span>
                  </div>
                  <!-- 3. 排障建议 -->
                  <div class="ss-field">
                    <div class="ss-field-label">3. <strong>排障建议</strong></div>
                    <ul v-if="seg.fields.techTips.length" class="ss-field-list">
                      <li v-for="(item, j) in seg.fields.techTips" :key="j">{{ item }}</li>
                    </ul>
                    <span v-else class="ss-empty">无</span>
                  </div>
                </div>
              </div>
            </template>
          </template>
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
      title="编辑 KBD 条目"
      width="800px"
      top="4vh"
      :close-on-click-modal="false"
    >
      <el-form label-width="80px">
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
        <el-form-item label="内容">
          <el-input
            v-model="editContent"
            type="textarea"
            :rows="18"
            placeholder="Markdown 格式内容"
            style="font-family: monospace; font-size: 13px"
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

.md-render :deep(.md-h2) {
  font-size: 16px;
  font-weight: 700;
  color: #1a1a2e;
  margin: 18px 0 8px;
  padding-bottom: 4px;
  border-bottom: 2px solid #409eff22;
}

.md-render :deep(.md-h3) {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
  margin: 12px 0 6px;
}

.md-render :deep(.md-p) {
  margin: 4px 0;
}

.md-render :deep(.md-blockquote) {
  background: #f0f9ff;
  border-left: 4px solid #409eff;
  border-radius: 0 4px 4px 0;
  padding: 8px 14px;
  margin: 8px 0;
  color: #4a6fa5;
  font-size: 13px;
}

.md-render :deep(.md-list) {
  margin: 6px 0 6px 20px;
  padding: 0;
}

.md-render :deep(.md-list li) {
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
  gap: 4px;
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
</style>
