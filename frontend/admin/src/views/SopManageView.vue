<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { Warning } from '@element-plus/icons-vue'

// ──────────────────────────────────────────────────────────────────────────────
// 类型定义
// ──────────────────────────────────────────────────────────────────────────────
interface SopDocument {
  id: number
  source_id: string | null
  category_id: string | null
  title: string
  status: string
  tree_leaf_count: number // 决策树叶节点数量
  tree_validation_issues?: ValidationIssue[] // 决策树校验问题（有告警时存储）
  content_md?: string
  tree_validation_status: string | null // 决策树校验状态：valid/warnings/error
  has_tree: boolean // 是否有决策树
  reviewer_id: number | null
  reviewed_at: string | null
  published_at: string | null
  created_at: string
  updated_at: string
}

interface SopListResponse {
  documents: SopDocument[]
  total: number
  page: number
  page_size: number
}

// 决策树校验问题（后端返回，含行号）
interface ValidationIssue {
  level: 'error' | 'warning'
  location: string
  line_number: number | null
  message: string
}

// ──────────────────────────────────────────────────────────────────────────────
// SOP 决策树类型定义
// ──────────────────────────────────────────────────────────────────────────────
interface PrerequisiteItem {
  description: string
  type: 'filter' | 'priority'
  target_node_hint?: string
}

interface DiagnosisDetail {
  acli_methods: string[]
  page_methods?: string[]
  analysis_steps?: string[]
  possible_causes?: string[]
}

interface SolutionDetail {
  quick_recovery: string[]
  thorough_fix: string[]
}

interface SOPNode {
  id: string
  title: string
  level: number
  line_number: number
  children: SOPNode[]
  prerequisite_items: PrerequisiteItem[]
  diagnosis?: DiagnosisDetail
  solution?: SolutionDetail
}

// 分类基线选项
interface CategoryOption {
  code: string
  name: string
}

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态
// ──────────────────────────────────────────────────────────────────────────────
const loading = ref(false)
const documents = ref<SopDocument[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const statusFilter = ref('')
const categoryFilter = ref('')

// 操作加载状态（按文档 ID 记录）
const approveLoading = ref<Record<number, boolean>>({})
const archiveLoading = ref<Record<number, boolean>>({})

// 查看弹窗
const viewDialogVisible = ref(false)
const viewDoc = ref<SopDocument | null>(null)

// 编辑弹窗
const editDialogVisible = ref(false)
const editDoc = ref<SopDocument | null>(null)
const editTitle = ref('')
const editCategoryId = ref('')
const editContentMd = ref('')
const editOriginalContentMd = ref('')  // 用于检测正文是否变更
const editLoadingContent = ref(false)
const editLoading = ref(false)

// 分类基线（用于 select）
const categoriesLoading = ref(false)
const categoryOptions = ref<CategoryOption[]>([])

// 行号编辑器
const editTextareaRef = ref<HTMLTextAreaElement | null>(null)
const lineNumbersRef = ref<HTMLDivElement | null>(null)
const editLineCount = computed(() => {
  const lines = editContentMd.value.split('\n').length
  return Array.from({ length: Math.max(lines, 1) }, (_, i) => i + 1)
})
function syncLineNumbersScroll() {
  if (lineNumbersRef.value && editTextareaRef.value) {
    lineNumbersRef.value.scrollTop = editTextareaRef.value.scrollTop
  }
}

// 校验问题弹窗
const validationDialogVisible = ref(false)
const validationIssues = ref<ValidationIssue[]>([])
const validationDocTitle = ref('')

// 导入弹窗
const importDialogVisible = ref(false)
const importFile = ref<File | null>(null)
const importCategoryId = ref('')
const importLoading = ref(false)
const importFileInput = ref<HTMLInputElement | null>(null)

// ─── 决策树可视化 ──────────────────────────────────────────────────────────────
const viewTreeLoading = ref(false)
const viewTreeData = ref<SOPNode | null>(null)
const viewTreeExpandedKeys = ref<string[]>([])

// el-tree 配置
const treeProps = {
  children: 'children',
  label: 'title',
}

// ──────────────────────────────────────────────────────────────────────────────
// API
// ──────────────────────────────────────────────────────────────────────────────
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

/** 统一错误信息提取：处理 FastAPI 422 数组 detail */
function extractErrorMsg(e: unknown): string {
  if (typeof e === 'string') return e
  const err = e as { message?: string }
  if (err?.message && err.message !== '[object Object]') return err.message
  return '操作失败，请重试'
}

/** 从非 2xx 的 fetch Response 中提取可读错误消息，格式为 "HTTP <status>：<detail>" */
async function parseHttpError(resp: Response): Promise<{ msg: string; issues?: ValidationIssue[] }> {
  const err = await resp.json().catch(() => ({}))
  const detail = (err as { detail?: unknown; message?: string }).detail
  let detailMsg: string
  let issues: ValidationIssue[] | undefined

  // 处理 SOP 解析失败的 422 错误格式
  if (detail != null && typeof detail === 'object' && 'validation_issues' in detail) {
    const d = detail as { message?: string; validation_issues?: unknown[] }
    detailMsg = d.message || '决策树解析失败'
    if (Array.isArray(d.validation_issues)) {
      issues = d.validation_issues as ValidationIssue[]
    }
  } else if (Array.isArray(detail)) {
    detailMsg = detail
      .map((d: { msg?: string; loc?: string[] }) => {
        const loc = d.loc && d.loc.length > 0 ? `[${d.loc.join('.')}] ` : ''
        return loc + (d.msg || JSON.stringify(d))
      })
      .join('; ')
  } else if (typeof detail === 'string') {
    detailMsg = detail
  } else if (detail != null) {
    detailMsg = JSON.stringify(detail)
  } else if (typeof (err as { message?: string }).message === 'string') {
    detailMsg = (err as { message: string }).message
  } else {
    detailMsg = resp.statusText || '未知错误'
  }
  return { msg: `HTTP ${resp.status}：${detailMsg}`, issues }
}

async function fetchDocuments() {
  loading.value = true
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
    })
    if (statusFilter.value) params.append('status', statusFilter.value)
    if (categoryFilter.value) params.append('category_id', categoryFilter.value)

    const resp = await fetch(`/api/v1/sop?${params}`, { headers: authHeader })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data: SopListResponse = await resp.json()
    documents.value = data.documents
    total.value = data.total
  } catch {
    ElMessage.error('加载 SOP 文档列表失败，请刷新重试')
  } finally {
    loading.value = false
  }
}

// ─── 发布 ────────────────────────────────────────────────────────────────────
async function handleApprove(doc: SopDocument) {
  try {
    await ElMessageBox.confirm(
      `确认发布 SOP 文档？\n\n「${doc.title}」\n\n将解析生成决策树，耗时较长，请耐心等待。`,
      '发布 SOP',
      { confirmButtonText: '确认发布', cancelButtonText: '取消', type: 'success' },
    )
    approveLoading.value[doc.id] = true
    const resp = await fetch(`/api/v1/sop/${doc.id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ reviewer_id: 1 }),
    })
    if (!resp.ok) {
      // 解析错误，如果包含 validation_issues 则显示详情弹窗
      const { msg, issues } = await parseHttpError(resp)
      if (issues && issues.length > 0) {
        validationIssues.value = issues
        validationDocTitle.value = doc.title
        validationDialogVisible.value = true
        ElMessage.error(`发布失败：${msg}`)
      } else {
        throw new Error(msg)
      }
      return
    }
    const result = await resp.json()

    // 收集 validation_issues（含行号）
    const issues: ValidationIssue[] = result.validation_issues || []

    if (result.tree_validation_status === 'error') {
      ElMessage.error('发布完成但决策树解析失败，请修复文档格式后重新发布')
    } else if (issues.length > 0) {
      ElMessage.warning(`发布成功，存在 ${issues.length} 条校验警告`)
    } else {
      ElMessage.success(`发布成功，决策树状态：${treeValidationLabel(result.tree_validation_status, result.tree_generated)}`)
    }

    // 若存在校验问题（包括 error 和 warning），弹出详情弹窗
    if (issues.length > 0) {
      validationIssues.value = issues
      validationDocTitle.value = doc.title
      validationDialogVisible.value = true
    }

    const idx = documents.value.findIndex((d) => d.id === doc.id)
    if (idx !== -1) {
      documents.value[idx].status = 'published'
      documents.value[idx].published_at = result.published_at
      documents.value[idx].tree_validation_status = result.tree_validation_status
      documents.value[idx].has_tree = result.tree_generated
    }
    viewDialogVisible.value = false
  } catch (e: unknown) {
    const msg = extractErrorMsg(e)
    if (msg !== 'cancel') ElMessage.error(`发布失败：${msg}`)
  } finally {
    delete approveLoading.value[doc.id]
  }
}

// ─── 归档 ────────────────────────────────────────────────────────────────────
async function handleArchive(doc: SopDocument) {
  try {
    await ElMessageBox.confirm(
      `确认归档「${doc.title}」？\n归档后将不再出现在 AI 搜索结果中。`,
      '归档 SOP',
      { confirmButtonText: '确认归档', cancelButtonText: '取消', type: 'warning' },
    )
    archiveLoading.value[doc.id] = true
    const resp = await fetch(`/api/v1/sop/${doc.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ status: 'archived' }),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('已归档')
    const idx = documents.value.findIndex((d) => d.id === doc.id)
    if (idx !== -1) documents.value[idx].status = 'archived'
  } catch (e: unknown) {
    const msg = extractErrorMsg(e)
    if (msg !== 'cancel') ElMessage.error(`归档失败：${msg}`)
  } finally {
    delete archiveLoading.value[doc.id]
  }
}

// ─── 查看内容 ────────────────────────────────────────────────────────────────
function openViewDialog(doc: SopDocument) {
  viewDoc.value = doc
  viewDialogVisible.value = true
  // 重置决策树状态
  viewTreeData.value = null
  viewTreeExpandedKeys.value = []
  // 若文档有决策树，异步加载
  if (doc.has_tree) {
    fetchViewTree(doc.id)
  }
}

// ─── 决策树数据获取 ──────────────────────────────────────────────────────────
async function fetchViewTree(documentId: number) {
  viewTreeLoading.value = true
  try {
    const resp = await fetch(`/api/v1/sop/${documentId}/tree`, { headers: authHeader })
    if (resp.ok) {
      viewTreeData.value = await resp.json()
      // 默认展开第一层节点
      if (viewTreeData.value?.id) {
        viewTreeExpandedKeys.value = [viewTreeData.value.id]
      }
    } else if (resp.status !== 404) {
      ElMessage.warning('决策树加载失败')
    }
  } catch {
    ElMessage.warning('决策树加载失败，请稍后重试')
  } finally {
    viewTreeLoading.value = false
  }
}

// ─── 决策树展开/折叠 ──────────────────────────────────────────────────────────
function expandAllNodes() {
  if (!viewTreeData.value) return
  viewTreeExpandedKeys.value = collectAllNodeIds(viewTreeData.value)
}

function collapseAllNodes() {
  viewTreeExpandedKeys.value = []
}

function collectAllNodeIds(node: SOPNode): string[] {
  const ids = [node.id]
  if (node.children?.length) {
    for (const child of node.children) {
      ids.push(...collectAllNodeIds(child))
    }
  }
  return ids
}

// ─── 编辑 ────────────────────────────────────────────────────────────────────
async function openEditDialog(doc: SopDocument) {
  editDoc.value = doc
  editTitle.value = doc.title
  editCategoryId.value = doc.category_id || ''
  editContentMd.value = doc.content_md || ''
  editOriginalContentMd.value = doc.content_md || ''
  editDialogVisible.value = true

  // 列表接口不返回 content_md，需单独请求
  if (!doc.content_md) {
    editLoadingContent.value = true
    try {
      const resp = await fetch(`/api/v1/sop/${doc.id}`, { headers: authHeader })
      if (resp.ok) {
        const detail = await resp.json()
        editContentMd.value = detail.content_md || ''
        editOriginalContentMd.value = detail.content_md || ''
        const idx = documents.value.findIndex((d) => d.id === doc.id)
        if (idx !== -1) documents.value[idx].content_md = detail.content_md
      }
    } catch {
      ElMessage.warning('正文加载失败，可手动输入内容')
    } finally {
      editLoadingContent.value = false
    }
  }
}

async function submitEdit() {
  if (!editDoc.value) return
  if (!editTitle.value.trim()) {
    ElMessage.warning('标题不能为空')
    return
  }
  editLoading.value = true
  try {
    const payload: Record<string, string | null> = {}
    if (editTitle.value.trim() !== editDoc.value.title) payload.title = editTitle.value.trim()
    const newCat = editCategoryId.value.trim() || null
    if (newCat !== editDoc.value.category_id) payload.category_id = newCat
    const newContent = editContentMd.value.trim()
    if (newContent && newContent !== editOriginalContentMd.value.trim()) {
      payload.content_md = newContent
    }

    if (Object.keys(payload).length === 0) {
      ElMessage.info('内容未变更')
      editDialogVisible.value = false
      return
    }
    const resp = await fetch(`/api/v1/sop/${editDoc.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify(payload),
    })
    if (!resp.ok) {
      const { msg } = await parseHttpError(resp)
      throw new Error(msg)
    }
    const result = await resp.json()
    const successMsg = result.message
      ? `保存成功：${result.message}`
      : (result.chunks_updated !== undefined
        ? `保存成功，已重新分块 ${result.chunks_updated} 个`
        : '保存成功')
    ElMessage.success(successMsg)
    const idx = documents.value.findIndex((d) => d.id === editDoc.value!.id)
    if (idx !== -1) {
      if (payload.title) documents.value[idx].title = payload.title as string
      if ('category_id' in payload) documents.value[idx].category_id = payload.category_id
      if (payload.content_md) {
        documents.value[idx].content_md = payload.content_md as string
        if (result.status === 'draft' && documents.value[idx].status === 'published') {
          documents.value[idx].status = 'draft'
        }
      }
    }
    editDialogVisible.value = false
  } catch (e: unknown) {
    ElMessage.error(extractErrorMsg(e))
  } finally {
    editLoading.value = false
  }
}

// ─── 导入 ────────────────────────────────────────────────────────────────────
function openImportDialog() {
  importFile.value = null
  importCategoryId.value = ''
  importDialogVisible.value = true
  if (importFileInput.value) importFileInput.value.value = ''
}

function handleFileChange(e: Event) {
  const input = e.target as HTMLInputElement
  if (input.files && input.files[0]) {
    const f = input.files[0]
    const ext = f.name.toLowerCase().split('.').pop()
    if (ext !== 'docx' && ext !== 'md') {
      ElMessage.warning('仅支持 .docx 或 .md 格式文件')
      input.value = ''
      return
    }
    importFile.value = f
  }
}

async function submitImport() {
  if (!importFile.value) {
    ElMessage.warning('请选择文件')
    return
  }
  importLoading.value = true
  try {
    const formData = new FormData()
    formData.append('file', importFile.value)
    if (importCategoryId.value.trim()) {
      formData.append('category_id', importCategoryId.value.trim())
    }
    const resp = await fetch('/api/v1/sop/upload', {
      method: 'POST',
      headers: { Authorization: `Bearer ${internalToken}` },
      body: formData,
    })
    if (!resp.ok) {
      const { msg } = await parseHttpError(resp)
      throw new Error(msg)
    }
    const result = await resp.json()
    if (result.duplicate) {
      ElMessage.warning(result.message || '文件已存在，跳过导入')
    } else {
      ElMessage.success(`导入成功：「${result.title}」，状态为草稿，请发布后使用`)
    }
    importDialogVisible.value = false
    await fetchDocuments()
  } catch (e: unknown) {
    ElMessage.error(`导入失败：${extractErrorMsg(e)}`)
  } finally {
    importLoading.value = false
  }
}

// ─── 分类基线 ────────────────────────────────────────────────────────────────
async function fetchCategories() {
  categoriesLoading.value = true
  try {
    const resp = await fetch('/api/kb/categories?grouped=true', { headers: authHeader })
    if (!resp.ok) return
    // API 返回 { "domains": { domain: [category, ...] }, "total_domains": N }
    const data: { domains?: Record<string, CategoryOption[]> } = await resp.json()
    const domains = data.domains ?? {}
    categoryOptions.value = Object.values(domains).flat().sort((a, b) => a.code.localeCompare(b.code))
  } catch { /* 分类加载失败时仍允许手动输入 */ } finally {
    categoriesLoading.value = false
  }
}

// ─── 通用辅助 ────────────────────────────────────────────────────────────────
function handlePageChange(newPage: number) {
  page.value = newPage
  fetchDocuments()
}

function formatDate(d: string | null): string {
  if (!d) return '—'
  return new Date(d).toLocaleString('zh-CN')
}

function statusType(s: string): 'success' | 'warning' | 'info' | 'danger' {
  if (s === 'published') return 'success'
  if (s === 'draft') return 'warning'
  if (s === 'archived') return 'info'
  return 'danger'
}

function statusLabel(s: string): string {
  const map: Record<string, string> = { draft: '待发布', published: '已发布', archived: '已归档' }
  return map[s] || s
}

// 决策树状态徽章类型
function treeValidationType(s: string | null): 'success' | 'warning' | 'danger' | 'info' {
  if (s === 'valid') return 'success'
  if (s === 'warnings') return 'warning'
  if (s === 'error') return 'danger'
  return 'info' // null 或其他状态
}

// 决策树状态文案
function treeValidationLabel(s: string | null, hasTree: boolean): string {
  if (s === 'valid') return '正常'
  if (s === 'warnings') return '有警告'
  if (s === 'error') return '解析失败'
  if (hasTree) return '未校验'
  return '无决策树'
}

// 打开决策树告警详情弹窗
function openValidationDialog(doc: SopDocument) {
  validationDocTitle.value = doc.title
  // 如果行数据中有 tree_validation_issues，直接使用
  if (doc.tree_validation_issues?.length) {
    validationIssues.value = doc.tree_validation_issues
    validationDialogVisible.value = true
  } else {
    // 否则提示没有告警详情数据
    ElMessage.info('该文档的告警详情需从查看弹窗中获取')
    openViewDialog(doc)
  }
}

onMounted(() => {
  fetchDocuments()
  fetchCategories()
})
</script>

<template>
  <div class="sop-manage">
    <!-- 页面头部 -->
    <div class="page-header">
      <div class="header-row">
        <div>
          <h2 class="page-title">SOP 文档管理</h2>
          <p class="page-desc">管理排障手册（SOP）文档的发布状态。草稿需发布后方可被 AI 搜索引用。</p>
        </div>
        <el-button type="primary" @click="openImportDialog">＋ 导入文档</el-button>
      </div>
    </div>

    <!-- 过滤栏 -->
    <el-card class="filter-card" shadow="never">
      <el-row :gutter="16" align="middle">
        <el-col :span="5">
          <el-select v-model="statusFilter" placeholder="全部状态" clearable @change="fetchDocuments" style="width: 100%">
            <el-option label="全部" value="" />
            <el-option label="待发布 (draft)" value="draft" />
            <el-option label="已发布 (published)" value="published" />
            <el-option label="已归档 (archived)" value="archived" />
          </el-select>
        </el-col>
        <el-col :span="6">
          <el-input v-model="categoryFilter" placeholder="按分类 ID 筛选（如 虚拟机-003）" clearable @clear="fetchDocuments" @keyup.enter="fetchDocuments" />
        </el-col>
        <el-col :span="4">
          <div class="filter-btn-group">
            <el-button type="primary" @click="fetchDocuments">搜索</el-button>
            <el-button @click="statusFilter = ''; categoryFilter = ''; fetchDocuments()">重置</el-button>
          </div>
        </el-col>
        <el-col :span="9" class="total-info">共 <strong>{{ total }}</strong> 个文档</el-col>
      </el-row>
    </el-card>

    <!-- 列表 -->
    <el-card v-loading="loading" shadow="never" class="table-card">
      <el-table :data="documents" row-key="id" style="width: 100%">
        <el-table-column label="ID" width="70" align="center">
          <template #default="{ row }"><span class="doc-id">#{{ row.id }}</span></template>
        </el-table-column>
        <el-table-column label="文档标题" min-width="300">
          <template #default="{ row }"><span class="doc-title">{{ row.title }}</span></template>
        </el-table-column>
        <el-table-column label="分类" width="140">
          <template #default="{ row }">
            <span v-if="row.category_id" class="category-tag">{{ row.category_id }}</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column label="决策树" width="120" align="center">
          <template #default="{ row }">
            <div style="display:flex;align-items:center;gap:4px;justify-content:center">
              <el-tag :type="treeValidationType(row.tree_validation_status)" size="small">
                {{ treeValidationLabel(row.tree_validation_status, row.has_tree) }}
              </el-tag>
              <el-button
                v-if="row.tree_validation_status === 'warnings'"
                type="warning"
                size="small"
                text
                circle
                @click.stop="openValidationDialog(row)"
                title="查看告警详情"
              >
                <el-icon><Warning /></el-icon>
              </el-button>
            </div>
          </template>
        </el-table-column>
        <el-table-column label="节点数" width="90" align="center">
          <template #default="{ row }"><span class="node-count">{{ row.tree_leaf_count }}</span></template>
        </el-table-column>
        <el-table-column label="发布时间" width="160">
          <template #default="{ row }">
            <span v-if="row.published_at" class="date-text">{{ formatDate(row.published_at) }}</span>
            <span v-else class="text-muted">未发布</span>
          </template>
        </el-table-column>
        <el-table-column label="导入时间" width="160">
          <template #default="{ row }"><span class="date-text">{{ formatDate(row.created_at) }}</span></template>
        </el-table-column>
        <el-table-column label="操作" width="210" fixed="right">
          <template #default="{ row }">
            <el-button type="info" size="small" text @click="openViewDialog(row)">查看</el-button>
            <el-button type="primary" size="small" text @click="openEditDialog(row)">编辑</el-button>
            <template v-if="row.status === 'draft'">
              <el-button type="success" size="small" text :loading="!!approveLoading[row.id]" @click="handleApprove(row)">发布</el-button>
            </template>
            <template v-else-if="row.status === 'published'">
              <el-button type="warning" size="small" text :loading="!!archiveLoading[row.id]" @click="handleArchive(row)">归档</el-button>
            </template>
            <span v-else class="text-muted" style="font-size:13px;margin-left:4px">已归档</span>
          </template>
        </el-table-column>
      </el-table>
      <div class="pagination-wrapper">
        <el-pagination background layout="total, prev, pager, next" :total="total" :page-size="pageSize" :current-page="page" @current-change="handlePageChange" />
      </div>
    </el-card>

    <!-- ── 查看弹窗 ── -->
    <el-dialog v-model="viewDialogVisible" title="SOP 文档详情" width="900px" top="4vh">
      <template v-if="viewDoc">
        <!-- 基础信息区 -->
        <el-descriptions :column="4" border size="small" style="margin-bottom:16px">
          <el-descriptions-item label="ID">#{{ viewDoc.id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusType(viewDoc.status)" size="small">{{ statusLabel(viewDoc.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="决策树">
            <el-tag :type="treeValidationType(viewDoc.tree_validation_status)" size="small">
              {{ treeValidationLabel(viewDoc.tree_validation_status, viewDoc.has_tree) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="分类">{{ viewDoc.category_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="导入时间">{{ formatDate(viewDoc.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="发布时间">{{ formatDate(viewDoc.published_at) }}</el-descriptions-item>
          <el-descriptions-item label="标题" :span="2"><strong>{{ viewDoc.title }}</strong></el-descriptions-item>
        </el-descriptions>

        <!-- 决策树可视化区 -->
        <div v-if="viewDoc.has_tree" class="tree-section">
          <div class="tree-header">
            <span class="tree-title">决策树结构</span>
            <div class="tree-actions">
              <el-button size="small" text @click="expandAllNodes">全部展开</el-button>
              <el-button size="small" text @click="collapseAllNodes">全部折叠</el-button>
            </div>
          </div>
          <div v-loading="viewTreeLoading" class="tree-container">
            <template v-if="viewTreeData">
              <el-tree
                :data="[viewTreeData]"
                :props="treeProps"
                node-key="id"
                :expand-on-click-node="false"
                :default-expanded-keys="viewTreeExpandedKeys"
                highlight-current
              >
                <template #default="{ data }">
                  <div class="tree-node-content">
                    <!-- 节点标题行 -->
                    <div class="node-header">
                      <span :class="['node-level', `level-${data.level}`]">L{{ data.level }}</span>
                      <span class="node-title">{{ data.title }}</span>
                      <el-tag v-if="!data.children?.length" type="success" size="small" class="node-type-tag">叶节点</el-tag>
                      <el-tag v-else type="info" size="small" class="node-type-tag">路由节点</el-tag>
                      <span class="node-line">行 {{ data.line_number }}</span>
                    </div>
                    <!-- 前置条件（路由节点） -->
                    <div v-if="data.prerequisite_items?.length" class="node-prerequisites">
                      <span class="section-label">前置条件：</span>
                      <div class="prerequisite-list">
                        <div v-for="(p, idx) in data.prerequisite_items" :key="idx" class="prerequisite-item">
                          <el-tag :type="p.type === 'filter' ? 'primary' : 'warning'" size="small">
                            {{ p.type === 'filter' ? '过滤' : '优先' }}
                          </el-tag>
                          <span class="prerequisite-desc">{{ p.description }}</span>
                        </div>
                      </div>
                    </div>
                    <!-- 诊断方法（叶节点） -->
                    <div v-if="data.diagnosis" class="node-diagnosis">
                      <div class="diagnosis-section">
                        <span class="section-label">诊断方法：</span>
                        <ul class="method-list">
                          <li v-for="(m, idx) in data.diagnosis.acli_methods" :key="idx">
                            <code>{{ m }}</code>
                          </li>
                          <template v-if="data.diagnosis.page_methods?.length">
                            <li v-for="(m, idx) in data.diagnosis.page_methods" :key="'page-'+idx" class="page-method">
                              <span class="method-type">页面：</span><code>{{ m }}</code>
                            </li>
                          </template>
                        </ul>
                      </div>
                      <div v-if="data.diagnosis.possible_causes?.length" class="causes-section">
                        <span class="section-label">可能原因：</span>
                        <ul class="cause-list">
                          <li v-for="(c, idx) in data.diagnosis.possible_causes" :key="idx">{{ c }}</li>
                        </ul>
                      </div>
                    </div>
                    <!-- 解决方案（叶节点） -->
                    <div v-if="data.solution" class="node-solution">
                      <div v-if="data.solution.quick_recovery?.length" class="solution-section">
                        <span class="section-label success">快速恢复：</span>
                        <ol class="solution-steps">
                          <li v-for="(s, idx) in data.solution.quick_recovery" :key="idx">{{ s }}</li>
                        </ol>
                      </div>
                      <div v-if="data.solution.thorough_fix?.length" class="solution-section">
                        <span class="section-label primary">彻底修复：</span>
                        <ol class="solution-steps">
                          <li v-for="(s, idx) in data.solution.thorough_fix" :key="idx">{{ s }}</li>
                        </ol>
                      </div>
                    </div>
                  </div>
                </template>
              </el-tree>
            </template>
            <el-empty v-else-if="!viewTreeLoading" description="决策树数据加载失败" />
          </div>
        </div>
        <!-- 无决策树时的提示 -->
        <el-alert v-else type="info" :closable="false" show-icon style="margin-top:16px">
          <template #title>决策树说明</template>
          该文档尚未发布或决策树解析失败，请先点击「发布」按钮生成决策树。
        </el-alert>
      </template>
      <template #footer>
        <el-button @click="viewDialogVisible = false">关闭</el-button>
        <el-button type="primary" @click="viewDialogVisible = false; viewDoc && openEditDialog(viewDoc)">编辑</el-button>
        <el-button v-if="viewDoc && viewDoc.status === 'draft'" type="success" :loading="viewDoc ? !!approveLoading[viewDoc.id] : false" @click="viewDoc && handleApprove(viewDoc)">发布</el-button>
      </template>
    </el-dialog>

    <!-- ── 编辑弹窗 ── -->
    <el-dialog v-model="editDialogVisible" title="编辑 SOP 文档" width="900px" :close-on-click-modal="false">
      <div v-loading="editLoadingContent" style="min-height:80px">
        <el-form label-width="80px">
          <el-form-item label="标题" required>
            <el-input v-model="editTitle" placeholder="SOP 文档标题" />
          </el-form-item>
          <el-form-item label="分类">
            <el-select
              v-model="editCategoryId"
              filterable
              clearable
              allow-create
              placeholder="选择或搜索分类（如 虚拟机-003）"
              style="width: 100%"
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
          <el-form-item label="正文内容">
            <el-alert
              v-if="editDoc?.status === 'published'"
              type="warning"
              :closable="false"
              style="margin-bottom:8px;font-size:12px"
            >修改正文后文档将变为「草稿」，需重新发布才可被 AI 搜索引用。</el-alert>
            <div class="code-editor-wrapper">
              <div ref="lineNumbersRef" class="line-numbers">
                <div v-for="n in editLineCount" :key="n" class="line-num">{{ n }}</div>
              </div>
              <textarea
                ref="editTextareaRef"
                v-model="editContentMd"
                class="code-textarea"
                wrap="off"
                placeholder="Markdown 格式正文..."
                @scroll="syncLineNumbersScroll"
              />
            </div>
          </el-form-item>
        </el-form>
      </div>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="editLoading" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- ── 校验问题弹窗 ── -->
    <el-dialog
      v-model="validationDialogVisible"
      :title="`校验问题：${validationDocTitle}`"
      width="680px"
    >
      <el-table :data="validationIssues" border style="width:100%" max-height="480">
        <el-table-column label="级别" width="80" align="center">
          <template #default="{ row }">
            <el-tag :type="row.level === 'error' ? 'danger' : 'warning'" size="small">
              {{ row.level === 'error' ? '错误' : '警告' }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column label="行号" width="70" align="center">
          <template #default="{ row }">
            <span v-if="row.line_number" style="font-family:monospace;color:#409eff">{{ row.line_number }}</span>
            <span v-else style="color:#c0c4cc">—</span>
          </template>
        </el-table-column>
        <el-table-column label="位置" prop="location" width="120" show-overflow-tooltip />
        <el-table-column label="问题描述" prop="message" show-overflow-tooltip />
      </el-table>
      <template #footer>
        <el-button @click="validationDialogVisible = false">关闭</el-button>
      </template>
    </el-dialog>

    <!-- ── 导入弹窗 ── -->
    <el-dialog v-model="importDialogVisible" title="导入 SOP 文档" width="520px">
      <el-alert type="info" :closable="false" style="margin-bottom:16px">
        <template #title>导入说明</template>
        上传 Word（.docx）或 Markdown（.md）文档。导入后状态为「草稿」，需手动点击「发布」后生成决策树，AI 才可搜索引用。相同文件（SHA256）不会重复导入。
      </el-alert>
      <el-form label-width="90px">
        <el-form-item label="文档文件" required>
          <input ref="importFileInput" type="file" accept=".docx,.md" class="file-input" @change="handleFileChange" />
          <div v-if="importFile" class="file-name-hint">已选：{{ importFile.name }}（{{ (importFile.size / 1024).toFixed(1) }} KB）</div>
        </el-form-item>
        <el-form-item label="分类">
          <el-select
            v-model="importCategoryId"
            filterable
            clearable
            allow-create
            placeholder="选择或搜索分类（可选，后续可编辑）"
            style="width: 100%"
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
      </el-form>
      <template #footer>
        <el-button @click="importDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="importLoading" @click="submitImport">开始导入</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.sop-manage { padding: 20px; }
.page-header { margin-bottom: 20px; }
.header-row { display: flex; justify-content: space-between; align-items: flex-start; }
.page-title { margin: 0 0 8px; font-size: 22px; color: #303133; }
.page-desc { margin: 0; color: #666; font-size: 14px; }
.filter-card { margin-bottom: 16px; }
.filter-btn-group { display: flex; align-items: center; gap: 8px; flex-wrap: nowrap; }
.total-info { text-align: right; color: #909399; font-size: 14px; }
.table-card { min-height: 400px; }
.doc-id { color: #909399; font-family: monospace; font-size: 13px; }
.doc-title { color: #303133; line-height: 1.5; }
.category-tag { font-size: 12px; color: #909399; background: #f5f7fa; padding: 2px 6px; border-radius: 3px; }
.node-count { font-family: monospace; font-size: 13px; color: #606266; }
.date-text { font-size: 13px; color: #606266; }
.text-muted { color: #c0c4cc; font-size: 13px; }
.pagination-wrapper { display: flex; justify-content: flex-end; margin-top: 16px; }
.file-input { display: block; width: 100%; font-size: 14px; color: #606266; cursor: pointer; }
.file-name-hint { margin-top: 6px; font-size: 12px; color: #409eff; }

/* 行号编辑器 */
.code-editor-wrapper {
  display: flex;
  width: 100%;
  height: 440px;
  border: 1px solid #dcdfe6;
  border-radius: 4px;
  overflow: hidden;
  transition: border-color .2s;
}
.code-editor-wrapper:focus-within { border-color: #409eff; }
.line-numbers {
  background: #f5f7fa;
  border-right: 1px solid #e4e7ed;
  padding: 8px 6px 8px 4px;
  text-align: right;
  height: 100%;
  overflow-y: auto;
  overflow-x: hidden;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  color: #909399;
  user-select: none;
  min-width: 42px;
  flex-shrink: 0;
}
.line-num::-webkit-scrollbar { width: 0; height: 0; }
.line-numbers::-webkit-scrollbar { width: 0; }
.line-num { height: calc(13px * 1.6); }
.code-textarea {
  flex: 1;
  height: 100%;
  border: none;
  outline: none;
  padding: 8px;
  font-family: 'SFMono-Regular', Consolas, 'Liberation Mono', Menlo, monospace;
  font-size: 13px;
  line-height: 1.6;
  resize: none;
  color: #303133;
  background: #fff;
  overflow: auto;
  tab-size: 2;
}

/* 决策树可视化 */
.tree-section {
  border: 1px solid #e4e7ed;
  border-radius: 4px;
  margin-top: 16px;
  background: #fafafa;
}
.tree-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 12px 16px;
  border-bottom: 1px solid #e4e7ed;
  background: #f5f7fa;
}
.tree-title {
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}
.tree-actions {
  display: flex;
  gap: 4px;
}
.tree-container {
  max-height: 480px;
  overflow-y: auto;
  padding: 12px;
}
.tree-node-content {
  padding: 8px 4px;
  font-size: 13px;
  line-height: 1.6;
}
.node-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}
.node-level {
  font-family: monospace;
  font-size: 11px;
  font-weight: 600;
  padding: 2px 6px;
  border-radius: 3px;
  background: #e6f7ff;
  color: #1890ff;
}
.node-level.level-1 { background: #f6ffed; color: #52c41a; }
.node-level.level-2 { background: #fffbe6; color: #faad14; }
.node-level.level-3 { background: #fff1f0; color: #f5222d; }
.node-level.level-4 { background: #f9f0ff; color: #722ed1; }
.node-title {
  font-weight: 500;
  color: #303133;
  flex: 1;
}
.node-type-tag {
  margin-left: 4px;
}
.node-line {
  font-family: monospace;
  font-size: 11px;
  color: #909399;
  margin-left: 8px;
}
.node-prerequisites {
  margin-top: 6px;
  padding: 6px 10px;
  background: #f5f7fa;
  border-radius: 3px;
}
.section-label {
  font-size: 12px;
  color: #606266;
  font-weight: 500;
  margin-right: 6px;
}
.prerequisite-list {
  margin-top: 4px;
}
.prerequisite-item {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 4px;
}
.prerequisite-desc {
  color: #303133;
  font-size: 13px;
}
.node-diagnosis {
  margin-top: 6px;
  padding: 6px 10px;
  background: #e6f7ff;
  border-radius: 3px;
  border-left: 3px solid #1890ff;
}
.diagnosis-section {
  margin-bottom: 6px;
}
.method-list {
  margin: 4px 0 0 0;
  padding-left: 20px;
  list-style: disc;
}
.method-list li {
  margin-bottom: 2px;
}
.method-list code {
  font-family: 'SFMono-Regular', Consolas, monospace;
  background: #f0f0f0;
  padding: 1px 4px;
  border-radius: 2px;
  font-size: 12px;
  color: #1890ff;
}
.method-list .page-method {
  list-style: circle;
}
.method-type {
  font-size: 12px;
  color: #909399;
  margin-right: 4px;
}
.causes-section {
  margin-top: 8px;
}
.cause-list {
  margin: 4px 0 0 0;
  padding-left: 20px;
  list-style: square;
}
.cause-list li {
  margin-bottom: 2px;
  color: #606266;
}
.node-solution {
  margin-top: 6px;
  padding: 6px 10px;
  background: #f6ffed;
  border-radius: 3px;
  border-left: 3px solid #52c41a;
}
.solution-section {
  margin-bottom: 8px;
}
.solution-section:last-child {
  margin-bottom: 0;
}
.section-label.success {
  color: #52c41a;
}
.section-label.primary {
  color: #409eff;
}
.solution-steps {
  margin: 4px 0 0 0;
  padding-left: 20px;
}
.solution-steps li {
  margin-bottom: 4px;
  color: #303133;
}
</style>
