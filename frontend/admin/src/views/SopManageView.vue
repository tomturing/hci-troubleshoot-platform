<script setup lang="ts">
import { ref, computed, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'
import { FullScreen, Warning } from '@element-plus/icons-vue'
import SopTreeNode from './SopTreeNode.vue'
import { useCategories } from '../composables/useCategories'

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
  content_type?: 'text' | 'command'
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

interface TreeDataResponse {
  tree: SOPNode
  tree_leaf_count: number
  tree_validation_status: string
}

const { categoryOptions, categoriesLoading, fetchCategories } = useCategories()

// ─── 响应式状态 ───
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
const treeLoading = ref(false)
const treeData = ref<TreeDataResponse | null>(null)
const expandedKeys = ref<Set<string>>(new Set())
const viewFullscreen = ref(false)

// 编辑弹窗
const editDialogVisible = ref(false)
const editDoc = ref<SopDocument | null>(null)
const editTitle = ref('')
const editCategoryId = ref('')
const editContentMd = ref('')
const editOriginalContentMd = ref('')  // 用于检测正文是否变更
const editLoadingContent = ref(false)
const editLoading = ref(false)

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

// ─── 决策树查询与折叠逻辑 ───────────────────────────────────────────────────
async function loadTreeData(docId: number) {
  treeLoading.value = true
  treeData.value = null
  expandedKeys.value = new Set()
  try {
    const resp = await fetch(`/api/v1/sop/${docId}/tree`, { headers: authHeader })
    if (resp.ok) {
      const data = await resp.json()
      treeData.value = data
      // 默认全部展开
      expandAll()
    } else if (resp.status === 404) {
      treeData.value = null
    } else {
      const { msg } = await parseHttpError(resp)
      ElMessage.warning(`无法获取决策树：${msg}`)
    }
  } catch (err) {
    console.error(err)
    ElMessage.error('加载决策树出错')
  } finally {
    treeLoading.value = false
  }
}

function handleToggleExpand(nodeId: string) {
  if (expandedKeys.value.has(nodeId)) {
    expandedKeys.value.delete(nodeId)
  } else {
    expandedKeys.value.add(nodeId)
  }
  expandedKeys.value = new Set(expandedKeys.value)
}

function expandAll() {
  const keys = new Set<string>()
  const traverse = (node: SOPNode | undefined) => {
    if (!node) return
    keys.add(node.id)
    if (node.children) {
      node.children.forEach(traverse)
    }
  }
  if (treeData.value?.tree) {
    traverse(treeData.value.tree)
  }
  expandedKeys.value = keys
}

function collapseAll() {
  expandedKeys.value = new Set()
}

// ─── 获取文档详情（含告警信息）───────────────────────────────────────────────
async function fetchViewDocDetail(documentId: number) {
  try {
    const resp = await fetch(`/api/v1/sop/${documentId}`, { headers: authHeader })
    if (resp.ok) {
      const detail = await resp.json()
      if (viewDoc.value && viewDoc.value.id === documentId) {
        viewDoc.value.tree_validation_issues = detail.tree_validation_issues || []
        viewDoc.value.tree_leaf_count = detail.tree_leaf_count
      }
    }
  } catch {
    // 静默失败，不影响弹窗显示
  }
}

// ─── 显示告警详情弹窗 ─────────────────────────────────────────────────────────
function fetchValidationIssues() {
  if (!viewDoc.value) return
  if (viewDoc.value.tree_validation_issues?.length) {
    validationIssues.value = viewDoc.value.tree_validation_issues
    validationDocTitle.value = viewDoc.value.title
    validationDialogVisible.value = true
  } else {
    // 如果没有数据，提示用户
    ElMessage.info('暂无告警详情数据')
  }
}

// ─── 查看内容 ────────────────────────────────────────────────────────────────
async function openViewDialog(doc: SopDocument) {
  viewDoc.value = doc
  viewFullscreen.value = false
  viewDialogVisible.value = true

  // 获取文档详情（包含 tree_validation_issues）
  if (doc.tree_validation_status === 'warnings' && !doc.tree_validation_issues) {
    await fetchViewDocDetail(doc.id)
  }

  // 加载决策树数据
  loadTreeData(doc.id)
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
        <el-table-column label="决策树" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="treeValidationType(row.tree_validation_status)" size="small">
              {{ treeValidationLabel(row.tree_validation_status, row.has_tree) }}
            </el-tag>
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
    <el-dialog
      v-model="viewDialogVisible"
      width="90%"
      class="premium-dialog"
      :fullscreen="viewFullscreen"
      draggable
      align-center
    >
      <!-- 自定义弹窗 Header，含全屏切换按钮 -->
      <template #header>
        <div class="custom-dialog-header">
          <span class="el-dialog__title">SOP 文档详情</span>
          <el-button
            type="info"
            text
            circle
            :icon="FullScreen"
            class="fullscreen-toggle-btn"
            @click="viewFullscreen = !viewFullscreen"
            title="切换全屏"
          />
        </div>
      </template>

      <template v-if="viewDoc">
        <el-row :gutter="20" class="sop-detail-layout">
          <!-- 左栏：基本元数据描述 -->
          <el-col :span="6" class="sop-meta-col">
            <div class="sop-meta-card">
              <h3 class="side-panel-title">元数据信息</h3>
              <el-descriptions :column="1" border size="small" class="sleek-descriptions">
                <el-descriptions-item label="文档 ID">
                  <span class="mono-id">#{{ viewDoc.id }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="发布状态">
                  <el-tag :type="statusType(viewDoc.status)" size="small" effect="dark">{{ statusLabel(viewDoc.status) }}</el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="决策树节点数">
                  <span v-if="treeData" class="badge-num green-glow">{{ treeData.tree_leaf_count || '—' }} 节点</span>
                  <span v-else class="text-muted">—</span>
                  <!-- 告警提示：有警告时显示查看按钮 -->
                  <el-tag v-if="viewDoc.tree_validation_status === 'warnings'" type="warning" size="small" style="margin-left:8px">
                    有警告
                    <el-button type="warning" size="small" text circle @click="fetchValidationIssues" title="查看告警详情" style="margin-left:4px">
                      <el-icon><Warning /></el-icon>
                    </el-button>
                  </el-tag>
                  <el-tag v-else-if="viewDoc.tree_validation_status === 'error'" type="danger" size="small" style="margin-left:8px">解析失败</el-tag>
                </el-descriptions-item>
                <el-descriptions-item label="分类基线">
                  <el-tag v-if="viewDoc.category_id" type="info" size="small" effect="plain">{{ viewDoc.category_id }}</el-tag>
                  <span v-else class="text-muted">—</span>
                </el-descriptions-item>
                <el-descriptions-item label="导入时间">
                  <span class="date-desc">{{ formatDate(viewDoc.created_at) }}</span>
                </el-descriptions-item>
                <el-descriptions-item label="最近发布">
                  <span class="date-desc">{{ formatDate(viewDoc.published_at) }}</span>
                </el-descriptions-item>
              </el-descriptions>

              <div class="sop-meta-title-box">
                <span class="meta-label">文档标题</span>
                <div class="meta-val">{{ viewDoc.title }}</div>
              </div>

              <!-- 贴心小指南 -->
              <div class="guide-box">
                <div class="guide-title">💡 交互小提示</div>
                <ul class="guide-list">
                  <li>右侧多叉决策树支持点击节点**头部**快速折叠/展开分支；</li>
                  <li>支持一键**全部展开**或**全部折叠**以快速审阅结构；</li>
                  <li>叶子节点中推荐的 `acli` 命令支持**一键复制**；</li>
                  <li>如发现逻辑有误，可点击下方"编辑"直接调整 Markdown 内容。</li>
                </ul>
              </div>
            </div>
          </el-col>

          <!-- 右栏：决策树渲染区域 -->
          <el-col :span="18" class="sop-tree-col">
            <div class="tree-display-panel" v-loading="treeLoading">
              <!-- 决策树操作栏 -->
              <div class="tree-toolbar">
                <div class="toolbar-left">
                  <span class="tree-section-title">决策树结构（Decision Tree Flow）</span>
                  <el-tag
                    v-if="treeData"
                    :type="treeValidationType(treeData.tree_validation_status)"
                    size="small"
                    class="validation-status-tag"
                  >
                    校验状态: {{ treeValidationLabel(treeData.tree_validation_status, true) }}
                  </el-tag>
                </div>
                <div class="toolbar-right" v-if="treeData">
                  <el-button-group>
                    <el-button type="primary" size="small" plain @click="expandAll">全部展开</el-button>
                    <el-button type="primary" size="small" plain @click="collapseAll">全部折叠</el-button>
                  </el-button-group>
                </div>
              </div>

              <!-- 树体内容区 -->
              <div class="tree-scroll-container">
                <template v-if="treeData && treeData.tree">
                  <SopTreeNode
                    :node="treeData.tree"
                    :expanded-keys="expandedKeys"
                    @toggle-expand="handleToggleExpand"
                  />
                </template>
                <div v-else-if="treeLoading" class="tree-empty-state">
                  <span class="loading-text">正在加载多叉决策树数据...</span>
                </div>
                <div v-else class="tree-empty-state">
                  <div class="empty-icon">📂</div>
                  <div class="empty-title">暂无决策树结构</div>
                  <div class="empty-desc">该文档目前无决策树 JSON。可能文档处于"草稿"状态，请点击下方"发布"按钮触发语法校验并生成决策树。</div>
                </div>
              </div>
            </div>
          </el-col>
        </el-row>
      </template>
      <template #footer>
        <div class="dialog-footer-actions">
          <el-button @click="viewDialogVisible = false">关闭</el-button>
          <el-button type="primary" @click="viewDialogVisible = false; viewDoc && openEditDialog(viewDoc)">编辑正文</el-button>
          <el-button v-if="viewDoc && viewDoc.status === 'draft'" type="success" :loading="viewDoc ? !!approveLoading[viewDoc.id] : false" @click="viewDoc && handleApprove(viewDoc)">发布并同步</el-button>
        </div>
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
.node-count-value { font-family: monospace; font-size: 14px; color: #303133; font-weight: 500; }
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

/* 高端详情弹窗样式 */
.custom-dialog-header {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding-right: 32px;
}

.fullscreen-toggle-btn {
  font-size: 16px;
  color: #606266;
  transition: all 0.2s;
}

.fullscreen-toggle-btn:hover {
  background: #f1f5f9;
  color: #409eff;
  transform: scale(1.1);
}

.sop-detail-layout {
  min-height: 520px;
}

/* 左侧栏：元数据卡片 */
.sop-meta-col {
  border-right: 1px solid #e4e7ed;
}

.sop-meta-card {
  display: flex;
  flex-direction: column;
  gap: 16px;
  padding-right: 10px;
}

.side-panel-title {
  margin: 0;
  font-size: 15px;
  font-weight: 700;
  color: #303133;
}

.sleek-descriptions :deep(.el-descriptions__label) {
  font-weight: 600;
  color: #606266;
  width: 100px;
  background: #f8fafc;
}

.sleek-descriptions :deep(.el-descriptions__content) {
  color: #303133;
}

.mono-id {
  font-family: monospace;
  font-weight: 700;
  color: #909399;
}

.badge-num {
  font-family: monospace;
  font-weight: 700;
  color: #606266;
}

.badge-num.green-glow {
  color: #16a34a;
  background: #f0fdf4;
  padding: 1px 6px;
  border-radius: 4px;
}

.date-desc {
  font-size: 12px;
  color: #606266;
}

.sop-meta-title-box {
  display: flex;
  flex-direction: column;
  gap: 6px;
  background: #f8fafc;
  padding: 12px;
  border-radius: 6px;
  border: 1px solid #f1f5f9;
}

.meta-label {
  font-size: 11px;
  font-weight: 700;
  color: #909399;
  text-transform: uppercase;
}

.meta-val {
  font-size: 14px;
  font-weight: 700;
  color: #1e293b;
  line-height: 1.5;
}

/* 提示指南框 */
.guide-box {
  background: #f0f7ff;
  border: 1px solid #e0eaff;
  border-radius: 6px;
  padding: 12px;
}

.guide-title {
  font-size: 12.5px;
  font-weight: 700;
  color: #1d4ed8;
  margin-bottom: 6px;
}

.guide-list {
  margin: 0;
  padding-left: 14px;
  font-size: 12px;
  color: #4b5563;
  line-height: 1.6;
}

.guide-list li {
  margin-bottom: 4px;
}

/* 右侧栏：决策树渲染区域 */
.sop-tree-col {
  display: flex;
  flex-direction: column;
}

.tree-display-panel {
  display: flex;
  flex-direction: column;
  border: 1px solid #e2e8f0;
  border-radius: 8px;
  background: #f8fafc;
  overflow: hidden;
  height: 100%;
  min-height: 480px;
}

.tree-toolbar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 10px 16px;
  background: #ffffff;
  border-bottom: 1px solid #e2e8f0;
}

.toolbar-left {
  display: flex;
  align-items: center;
  gap: 12px;
}

.tree-section-title {
  font-size: 14px;
  font-weight: 700;
  color: #1e293b;
}

.validation-status-tag {
  font-weight: 600;
  border-radius: 4px;
}

.tree-scroll-container {
  padding: 16px;
  overflow-y: auto;
  flex: 1;
  max-height: 60vh;
}

/* 全屏状态下，将树体高度拉大 */
.el-dialog.is-fullscreen .tree-scroll-container {
  max-height: 78vh;
}

.tree-empty-state {
  display: flex;
  flex-direction: column;
  align-items: center;
  justify-content: center;
  height: 100%;
  padding: 40px;
  text-align: center;
  background: #ffffff;
  border-radius: 6px;
  border: 1px dashed #e2e8f0;
  margin: 10px;
}

.empty-icon {
  font-size: 40px;
  margin-bottom: 12px;
}

.empty-title {
  font-size: 16px;
  font-weight: 700;
  color: #334155;
  margin-bottom: 6px;
}

.empty-desc {
  font-size: 13px;
  color: #64748b;
  max-width: 460px;
  line-height: 1.6;
}

.loading-text {
  font-size: 13px;
  color: #64748b;
}

.dialog-footer-actions {
  display: flex;
  justify-content: flex-end;
  gap: 10px;
}
</style>
