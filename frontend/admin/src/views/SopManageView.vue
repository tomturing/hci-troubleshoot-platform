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

// 发布加载状态（按文档 ID 记录）
const approveLoading = ref<Record<number, boolean>>({})
const archiveLoading = ref<Record<number, boolean>>({})

// 审核人 ID（实际项目中应来自登录态）
const currentUser = ref('admin')

// ──────────────────────────────────────────────────────────────────────────────
// API
// ──────────────────────────────────────────────────────────────────────────────
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

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

async function handleApprove(doc: SopDocument) {
  try {
    await ElMessageBox.confirm(
      `确认发布此 SOP 文档？\n\n「${doc.title}」\n\n将遍历 ${doc.chunk_count} 个分块生成向量索引，耗时较长。`,
      '发布 SOP',
      { confirmButtonText: '确认发布', cancelButtonText: '取消', type: 'success' },
    )
    approveLoading.value[doc.id] = true
    const resp = await fetch(`/api/v1/sop/${doc.id}/approve`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ reviewer_id: currentUser.value }),
    })
    if (!resp.ok) {
      const err = await resp.json().catch(() => ({}))
      throw new Error(err.detail || `HTTP ${resp.status}`)
    }
    const result = await resp.json()
    ElMessage.success(`发布成功，生成了 ${result.chunks_embedded} 个向量`)
    // 刷新该行状态
    const idx = documents.value.findIndex((d) => d.id === doc.id)
    if (idx !== -1) {
      documents.value[idx].status = 'published'
      documents.value[idx].published_at = result.published_at
    }
  } catch (e: unknown) {
    if ((e as { message?: string })?.message !== 'cancel') {
      ElMessage.error(`操作失败：${(e as { message?: string })?.message || '请重试'}`)
    }
  } finally {
    delete approveLoading.value[doc.id]
  }
}

async function handleArchive(doc: SopDocument) {
  try {
    await ElMessageBox.confirm(
      `确认归档此 SOP 文档？\n\n「${doc.title}」\n\n归档后将不再出现在搜索结果中。`,
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
    if ((e as { message?: string })?.message !== 'cancel') {
      ElMessage.error('操作失败，请重试')
    }
  } finally {
    delete archiveLoading.value[doc.id]
  }
}

function handlePageChange(newPage: number) {
  page.value = newPage
  fetchDocuments()
}

// ──────────────────────────────────────────────────────────────────────────────
// 辅助函数
// ──────────────────────────────────────────────────────────────────────────────
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
  const map: Record<string, string> = {
    draft: '待发布',
    published: '已发布',
    archived: '已归档',
  }
  return map[s] || s
}

onMounted(() => fetchDocuments())
</script>

<template>
  <div class="sop-manage">
    <!-- 页面头部 -->
    <div class="page-header">
      <h2 class="page-title">SOP 文档管理</h2>
      <p class="page-desc">
        管理排障手册（SOP）文档的发布状态。草稿文档需发布后方可被 AI 搜索引用。
      </p>
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
          <el-input
            v-model="categoryFilter"
            placeholder="按分类 ID 筛选（如 虚拟机-003）"
            clearable
            @clear="fetchDocuments"
            @keyup.enter="fetchDocuments"
          />
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="fetchDocuments">搜索</el-button>
          <el-button @click="statusFilter = ''; categoryFilter = ''; fetchDocuments()">重置</el-button>
        </el-col>
        <el-col :span="9" class="total-info">
          <span>共 <strong>{{ total }}</strong> 个文档</span>
        </el-col>
      </el-row>
    </el-card>

    <!-- 列表 -->
    <el-card v-loading="loading" shadow="never" class="table-card">
      <el-table :data="documents" row-key="id" style="width: 100%">
        <!-- ID -->
        <el-table-column label="ID" width="70" align="center">
          <template #default="{ row }">
            <span class="doc-id">#{{ row.id }}</span>
          </template>
        </el-table-column>

        <!-- 标题 -->
        <el-table-column label="文档标题" min-width="320">
          <template #default="{ row }">
            <span class="doc-title">{{ row.title }}</span>
          </template>
        </el-table-column>

        <!-- 分类 -->
        <el-table-column label="分类" width="140">
          <template #default="{ row }">
            <span v-if="row.category_id" class="category-tag">{{ row.category_id }}</span>
            <span v-else class="text-muted">—</span>
          </template>
        </el-table-column>

        <!-- 状态 -->
        <el-table-column label="状态" width="100" align="center">
          <template #default="{ row }">
            <el-tag :type="statusType(row.status)" size="small">{{ statusLabel(row.status) }}</el-tag>
          </template>
        </el-table-column>

        <!-- 分块数 -->
        <el-table-column label="分块数" width="90" align="center">
          <template #default="{ row }">
            <span class="chunk-count">{{ row.chunk_count }}</span>
          </template>
        </el-table-column>

        <!-- 发布时间 -->
        <el-table-column label="发布时间" width="160">
          <template #default="{ row }">
            <span v-if="row.published_at" class="date-text">{{ formatDate(row.published_at) }}</span>
            <span v-else class="text-muted">未发布</span>
          </template>
        </el-table-column>

        <!-- 导入时间 -->
        <el-table-column label="导入时间" width="160">
          <template #default="{ row }">
            <span class="date-text">{{ formatDate(row.created_at) }}</span>
          </template>
        </el-table-column>

        <!-- 操作 -->
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <template v-if="row.status === 'draft'">
              <el-button
                type="success"
                size="small"
                text
                :loading="!!approveLoading[row.id]"
                @click="handleApprove(row)"
              >发布</el-button>
            </template>
            <template v-else-if="row.status === 'published'">
              <el-button
                type="warning"
                size="small"
                text
                :loading="!!archiveLoading[row.id]"
                @click="handleArchive(row)"
              >归档</el-button>
            </template>
            <span v-else class="text-muted" style="font-size: 13px">已归档</span>
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
  </div>
</template>

<style scoped>
.sop-manage {
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

.doc-id {
  color: #909399;
  font-family: monospace;
  font-size: 13px;
}

.doc-title {
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

.chunk-count {
  font-family: monospace;
  font-size: 13px;
  color: #606266;
}

.date-text {
  font-size: 13px;
  color: #606266;
}

.text-muted {
  color: #c0c4cc;
  font-size: 13px;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
