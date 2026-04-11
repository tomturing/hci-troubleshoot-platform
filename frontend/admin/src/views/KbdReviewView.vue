<script setup lang="ts">
import { ref, reactive, computed, onMounted } from 'vue'
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

// 详情弹窗
const detailDialogVisible = ref(false)
const detailEntry = ref<KbdEntry | null>(null)
const reviewNote = ref('')
const editableCategoryId = ref('')

// 拒绝弹窗
const rejectDialogVisible = ref(false)
const rejectingEntry = ref<KbdEntry | null>(null)
const rejectNote = ref('')
const rejectLoading = ref(false)

// 审核人 ID（实际项目中应来自登录态）
const currentUser = ref('admin')

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
  detailDialogVisible.value = true
}

function handlePageChange(newPage: number) {
  page.value = newPage
  fetchPending()
}

// ──────────────────────────────────────────────────────────────────────────────
// 简易 Markdown → HTML 渲染（避免引入外部库）
// ──────────────────────────────────────────────────────────────────────────────
function renderMarkdown(md: string): string {
  if (!md) return ''
  const lines = md.split('\n')
  const html: string[] = []
  let inList = false
  let inBlockquote = false

  const flushList = () => {
    if (inList) { html.push('</ul>'); inList = false }
  }
  const flushBlockquote = () => {
    if (inBlockquote) { html.push('</blockquote>'); inBlockquote = false }
  }

  for (const rawLine of lines) {
    const line = rawLine

    // 二级标题
    if (line.startsWith('## ')) {
      flushList(); flushBlockquote()
      html.push(`<h3 class="md-h2">${escapeHtml(line.slice(3))}</h3>`)
      continue
    }
    // 三级标题
    if (line.startsWith('### ')) {
      flushList(); flushBlockquote()
      html.push(`<h4 class="md-h3">${escapeHtml(line.slice(4))}</h4>`)
      continue
    }
    // 大引用块（含图片说明）
    if (line.startsWith('> ')) {
      flushList()
      if (!inBlockquote) { html.push('<blockquote class="md-blockquote">'); inBlockquote = true }
      // 去掉 > 前缀，内联转义和加粗
      const inner = inlineRender(line.slice(2))
      html.push(`<p>${inner}</p>`)
      continue
    }
    // 无序列表
    if (line.startsWith('- ') || line.startsWith('* ')) {
      flushBlockquote()
      if (!inList) { html.push('<ul class="md-list">'); inList = true }
      html.push(`<li>${inlineRender(line.slice(2))}</li>`)
      continue
    }
    // 有序列表
    const olMatch = line.match(/^(\d+)\.\s+(.+)$/)
    if (olMatch) {
      flushBlockquote()
      if (!inList) { html.push('<ol class="md-list">'); inList = true }
      html.push(`<li>${inlineRender(olMatch[2])}</li>`)
      continue
    }

    flushList(); flushBlockquote()

    if (line.trim() === '') {
      html.push('<br>')
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

onMounted(() => fetchPending())
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
        <el-table-column label="操作" width="180" fixed="right">
          <template #default="{ row }">
            <el-button type="info" size="small" text @click="openDetailDialog(row)">详情</el-button>
            <template v-if="row.status === 'draft'">
              <el-button type="success" size="small" text @click="handleApprove(row)">通过</el-button>
              <el-button type="danger" size="small" text @click="openRejectDialog(row)">拒绝</el-button>
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
            <el-input
              v-model="editableCategoryId"
              size="small"
              placeholder="输入 kb_category.code，如 虚拟机-001"
              style="width: 200px"
            />
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
          <h4 class="section-title">内容预览</h4>
          <div
            class="md-render"
            v-html="renderMarkdown(detailEntry.content_md)"
          />
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
</style>
