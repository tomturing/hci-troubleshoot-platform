<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

// ──────────────────────────────────────────────────────────────────────────────
// 类型定义
// ──────────────────────────────────────────────────────────────────────────────
interface SopDocument {
  id: number
  source_id: string | null
  category_id: string | null
  title: string
  status: string
  chunk_count: number
  content_md?: string
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
const editLoading = ref(false)

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
      `确认发布 SOP 文档？\n\n「${doc.title}」\n\n将为 ${doc.chunk_count} 个分块生成向量索引，耗时较长，请耐心等待。`,
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
      const err = await resp.json().catch(() => ({}))
      const detail = err.detail
      const msg = Array.isArray(detail)
        ? detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join('; ')
        : (typeof detail === 'string' ? detail : `HTTP ${resp.status}`)
      throw new Error(msg)
    }
    const result = await resp.json()
    ElMessage.success(`发布成功，生成了 ${result.chunks_embedded} 个向量`)
    const idx = documents.value.findIndex((d) => d.id === doc.id)
    if (idx !== -1) {
      documents.value[idx].status = 'published'
      documents.value[idx].published_at = result.published_at
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
}

// ─── 编辑 ────────────────────────────────────────────────────────────────────
function openEditDialog(doc: SopDocument) {
  editDoc.value = doc
  editTitle.value = doc.title
  editCategoryId.value = doc.category_id || ''
  editDialogVisible.value = true
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
      const err = await resp.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }
    ElMessage.success('保存成功')
    const idx = documents.value.findIndex((d) => d.id === editDoc.value!.id)
    if (idx !== -1) {
      if (payload.title) documents.value[idx].title = payload.title as string
      if ('category_id' in payload) documents.value[idx].category_id = payload.category_id
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
    if (!f.name.toLowerCase().endsWith('.docx')) {
      ElMessage.warning('仅支持 .docx 格式文件')
      input.value = ''
      return
    }
    importFile.value = f
  }
}

async function submitImport() {
  if (!importFile.value) {
    ElMessage.warning('请选择 .docx 文件')
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
      const err = await resp.json().catch(() => ({}))
      const detail = err.detail
      const msg = Array.isArray(detail)
        ? detail.map((d: { msg?: string }) => d.msg || JSON.stringify(d)).join('; ')
        : (typeof detail === 'string' ? detail : `HTTP ${resp.status}`)
      throw new Error(msg)
    }
    const result = await resp.json()
    if (result.duplicate) {
      ElMessage.warning(result.message || '文件已存在，跳过导入')
    } else {
      ElMessage.success(`导入成功：「${result.title}」，共 ${result.chunks_created} 个分块，状态为草稿`)
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

onMounted(() => fetchDocuments())
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
        <el-button type="primary" @click="openImportDialog">＋ 导入 .docx</el-button>
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
          <el-button type="primary" @click="fetchDocuments">搜索</el-button>
          <el-button @click="statusFilter = ''; categoryFilter = ''; fetchDocuments()">重置</el-button>
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
        <el-table-column label="分块数" width="90" align="center">
          <template #default="{ row }"><span class="chunk-count">{{ row.chunk_count }}</span></template>
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
    <el-dialog v-model="viewDialogVisible" title="SOP 文档详情" width="760px" top="4vh">
      <template v-if="viewDoc">
        <el-descriptions :column="3" border size="small" style="margin-bottom:16px">
          <el-descriptions-item label="ID">#{{ viewDoc.id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="statusType(viewDoc.status)" size="small">{{ statusLabel(viewDoc.status) }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="分块数">{{ viewDoc.chunk_count }}</el-descriptions-item>
          <el-descriptions-item label="分类">{{ viewDoc.category_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="导入时间">{{ formatDate(viewDoc.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="发布时间">{{ formatDate(viewDoc.published_at) }}</el-descriptions-item>
          <el-descriptions-item label="标题" :span="3"><strong>{{ viewDoc.title }}</strong></el-descriptions-item>
        </el-descriptions>
        <el-alert type="info" :closable="false" show-icon>
          <template #title>内容说明</template>
          文档共 {{ viewDoc.chunk_count }} 个章节分块，已按标题拆分存入向量数据库。如需查看原始内容，请参考导入时的 .docx 源文件。
        </el-alert>
      </template>
      <template #footer>
        <el-button @click="viewDialogVisible = false">关闭</el-button>
        <el-button type="primary" @click="viewDialogVisible = false; viewDoc && openEditDialog(viewDoc)">编辑</el-button>
        <el-button v-if="viewDoc && viewDoc.status === 'draft'" type="success" :loading="viewDoc ? !!approveLoading[viewDoc.id] : false" @click="viewDoc && handleApprove(viewDoc)">发布</el-button>
      </template>
    </el-dialog>

    <!-- ── 编辑弹窗 ── -->
    <el-dialog v-model="editDialogVisible" title="编辑 SOP 文档信息" width="500px">
      <el-form label-width="80px">
        <el-form-item label="标题" required>
          <el-input v-model="editTitle" placeholder="SOP 文档标题" />
        </el-form-item>
        <el-form-item label="分类 ID">
          <el-input v-model="editCategoryId" placeholder="如 虚拟机-003（留空则清除分类）" />
        </el-form-item>
      </el-form>
      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" :loading="editLoading" @click="submitEdit">保存</el-button>
      </template>
    </el-dialog>

    <!-- ── 导入弹窗 ── -->
    <el-dialog v-model="importDialogVisible" title="导入 SOP 文档（.docx）" width="520px">
      <el-alert type="info" :closable="false" style="margin-bottom:16px">
        <template #title>导入说明</template>
        上传 Word 文档（.docx），系统自动按章节标题分块。导入后状态为「草稿」，需手动点击「发布」后 AI 才可搜索引用。相同文件（SHA256）不会重复导入。
      </el-alert>
      <el-form label-width="90px">
        <el-form-item label=".docx 文件" required>
          <input ref="importFileInput" type="file" accept=".docx" class="file-input" @change="handleFileChange" />
          <div v-if="importFile" class="file-name-hint">已选：{{ importFile.name }}（{{ (importFile.size / 1024).toFixed(1) }} KB）</div>
        </el-form-item>
        <el-form-item label="分类 ID">
          <el-input v-model="importCategoryId" placeholder="如 虚拟机-003（可选，后续可编辑）" />
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
.total-info { text-align: right; color: #909399; font-size: 14px; }
.table-card { min-height: 400px; }
.doc-id { color: #909399; font-family: monospace; font-size: 13px; }
.doc-title { color: #303133; line-height: 1.5; }
.category-tag { font-size: 12px; color: #909399; background: #f5f7fa; padding: 2px 6px; border-radius: 3px; }
.chunk-count { font-family: monospace; font-size: 13px; color: #606266; }
.date-text { font-size: 13px; color: #606266; }
.text-muted { color: #c0c4cc; font-size: 13px; }
.pagination-wrapper { display: flex; justify-content: flex-end; margin-top: 16px; }
.file-input { display: block; width: 100%; font-size: 14px; color: #606266; cursor: pointer; }
.file-name-hint { margin-top: 6px; font-size: 12px; color: #409eff; }
</style>
