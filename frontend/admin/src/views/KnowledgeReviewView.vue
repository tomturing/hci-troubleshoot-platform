<script setup lang="ts">
import { ref, reactive, onMounted } from 'vue'
import { ElMessage, ElMessageBox } from 'element-plus'

// ──────────────────────────────────────────────────────────────────────────────
// 类型定义
// ──────────────────────────────────────────────────────────────────────────────
interface KnowledgeAtom {
  id: string
  atom_type: string
  category_id: string
  trigger_json: Record<string, unknown>
  content_json: Record<string, unknown>
  source_type: string
  source_ref: string
  confidence: number
  created_at: string
}

interface PendingAtomsResponse {
  atoms: KnowledgeAtom[]
  total: number
  page: number
  page_size: number
}

// ──────────────────────────────────────────────────────────────────────────────
// 响应式状态
// ──────────────────────────────────────────────────────────────────────────────
const loading = ref(false)
const atoms = ref<KnowledgeAtom[]>([])
const total = ref(0)
const page = ref(1)
const pageSize = ref(20)
const categoryFilter = ref('')

// 编辑弹窗
const editDialogVisible = ref(false)
const editingAtom = ref<KnowledgeAtom | null>(null)
const editForm = reactive({
  content_full_text: '',
  confidence: 0.7,
})

// 详情弹窗
const detailDialogVisible = ref(false)
const detailAtom = ref<KnowledgeAtom | null>(null)

// 审核人 ID（实际项目中应来自登录态）
const currentUser = ref('admin')

// ──────────────────────────────────────────────────────────────────────────────
// API 调用（使用 fetch，与项目整体一致，不引入额外依赖）
// ──────────────────────────────────────────────────────────────────────────────
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

async function fetchPending() {
  loading.value = true
  try {
    const params = new URLSearchParams({
      page: String(page.value),
      page_size: String(pageSize.value),
    })
    if (categoryFilter.value) {
      params.append('category_id', categoryFilter.value)
    }
    const resp = await fetch(`/api/v1/atoms/pending?${params}`)
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const data: PendingAtomsResponse = await resp.json()
    atoms.value = data.atoms
    total.value = data.total
  } catch (e) {
    ElMessage.error('加载知识原子失败，请刷新重试')
  } finally {
    loading.value = false
  }
}

async function handleVerify(atom: KnowledgeAtom) {
  try {
    await ElMessageBox.confirm(
      `确认通过这条知识原子？\n\n「${getAtomSummary(atom)}」`,
      '审核通过',
      { confirmButtonText: '确认通过', cancelButtonText: '取消', type: 'success' },
    )
    const resp = await fetch(`/api/v1/atoms/${atom.id}/verify`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ verified_by: currentUser.value }),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('审核通过，知识原子已加入知识库')
    atoms.value = atoms.value.filter((a) => a.id !== atom.id)
    total.value -= 1
  } catch (e: unknown) {
    if ((e as { message?: string })?.message !== 'cancel') {
      ElMessage.error('操作失败，请重试')
    }
  }
}

async function handleReject(atom: KnowledgeAtom) {
  try {
    await ElMessageBox.confirm(
      `确认拒绝并删除这条知识原子？此操作不可撤销。\n\n「${getAtomSummary(atom)}」`,
      '拒绝知识原子',
      { confirmButtonText: '确认拒绝', cancelButtonText: '取消', type: 'warning' },
    )
    const resp = await fetch(`/api/v1/atoms/${atom.id}`, {
      method: 'DELETE',
      headers: authHeader,
    })
    if (!resp.ok && resp.status !== 204) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('已拒绝并删除')
    atoms.value = atoms.value.filter((a) => a.id !== atom.id)
    total.value -= 1
  } catch (e: unknown) {
    if ((e as { message?: string })?.message !== 'cancel') {
      ElMessage.error('操作失败，请重试')
    }
  }
}

function openEditDialog(atom: KnowledgeAtom) {
  editingAtom.value = atom
  const contentJson = atom.content_json as Record<string, unknown>
  editForm.content_full_text = String(contentJson?.full_text || '')
  editForm.confidence = atom.confidence
  editDialogVisible.value = true
}

async function submitEdit() {
  if (!editingAtom.value) return
  try {
    const updatedContent = {
      ...(editingAtom.value.content_json as Record<string, unknown>),
      full_text: editForm.content_full_text,
    }
    const resp = await fetch(`/api/v1/atoms/${editingAtom.value.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({
        content_json: updatedContent,
        confidence: editForm.confidence,
      }),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    ElMessage.success('修改已保存')
    editDialogVisible.value = false
    // 刷新列表
    await fetchPending()
  } catch (e) {
    ElMessage.error('保存失败，请重试')
  }
}

function openDetailDialog(atom: KnowledgeAtom) {
  detailAtom.value = atom
  detailDialogVisible.value = true
}

function handlePageChange(newPage: number) {
  page.value = newPage
  fetchPending()
}

// ──────────────────────────────────────────────────────────────────────────────
// 展示辅助函数
// ──────────────────────────────────────────────────────────────────────────────
function getAtomSummary(atom: KnowledgeAtom): string {
  const text = (atom.content_json as Record<string, unknown>)?.full_text
  if (typeof text === 'string' && text) return text.slice(0, 60) + (text.length > 60 ? '…' : '')
  return atom.id
}

function typeTagType(type: string): 'primary' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, 'primary' | 'success' | 'warning'> = {
    diagnostic_step: 'primary',
    fix_action: 'success',
    decision_gate: 'warning',
  }
  return map[type] || 'info'
}

function typeLabel(type: string): string {
  const map: Record<string, string> = {
    diagnostic_step: '诊断步骤',
    fix_action: '修复操作',
    decision_gate: '决策门',
  }
  return map[type] || type
}

function formatDate(d: string) {
  return new Date(d).toLocaleString('zh-CN')
}

function confidenceColor(v: number): string {
  if (v >= 0.8) return '#67c23a'
  if (v >= 0.6) return '#e6a23c'
  return '#f56c6c'
}

onMounted(() => fetchPending())
</script>

<template>
  <div class="knowledge-review">
    <!-- 页面头部 -->
    <div class="page-header">
      <h2 class="page-title">知识原子审核</h2>
      <p class="page-desc">
        以下是 AI 从成功排障会话（S6 阶段）中自动提炼的知识候选，请逐条审核后决定是否加入知识库。
      </p>
    </div>

    <!-- 过滤栏 -->
    <el-card class="filter-card" shadow="never">
      <el-row :gutter="16" align="middle">
        <el-col :span="6">
          <el-input
            v-model="categoryFilter"
            placeholder="按故障分类筛选（如 vm_power_failure）"
            clearable
            @clear="fetchPending"
            @keyup.enter="fetchPending"
          />
        </el-col>
        <el-col :span="4">
          <el-button type="primary" @click="fetchPending">搜索</el-button>
          <el-button @click="categoryFilter = ''; fetchPending()">重置</el-button>
        </el-col>
        <el-col :span="14" class="total-info">
          <span>待审核共 <strong>{{ total }}</strong> 条</span>
        </el-col>
      </el-row>
    </el-card>

    <!-- 知识原子列表 -->
    <el-card v-loading="loading" shadow="never" class="table-card">
      <el-table :data="atoms" row-key="id" style="width: 100%">
        <!-- 类型 -->
        <el-table-column label="类型" width="110">
          <template #default="{ row }">
            <el-tag :type="typeTagType(row.atom_type)" size="small">
              {{ typeLabel(row.atom_type) }}
            </el-tag>
          </template>
        </el-table-column>

        <!-- 摘要 -->
        <el-table-column label="内容摘要" min-width="260">
          <template #default="{ row }">
            <span class="atom-summary">{{ getAtomSummary(row) }}</span>
          </template>
        </el-table-column>

        <!-- 故障分类 -->
        <el-table-column label="故障分类" width="160" prop="category_id">
          <template #default="{ row }">
            <span class="category-tag">{{ row.category_id || '—' }}</span>
          </template>
        </el-table-column>

        <!-- 来源会话 -->
        <el-table-column label="来源会话" width="140">
          <template #default="{ row }">
            <el-tooltip :content="row.source_ref" placement="top">
              <span class="mono-text">{{ row.source_ref.slice(-8) }}</span>
            </el-tooltip>
          </template>
        </el-table-column>

        <!-- 置信度 -->
        <el-table-column label="置信度" width="100" align="center">
          <template #default="{ row }">
            <span :style="{ color: confidenceColor(row.confidence), fontWeight: 'bold' }">
              {{ (row.confidence * 100).toFixed(0) }}%
            </span>
          </template>
        </el-table-column>

        <!-- 提炼时间 -->
        <el-table-column label="提炼时间" width="160">
          <template #default="{ row }">
            {{ formatDate(row.created_at) }}
          </template>
        </el-table-column>

        <!-- 操作 -->
        <el-table-column label="操作" width="200" fixed="right">
          <template #default="{ row }">
            <el-button type="info" size="small" text @click="openDetailDialog(row)">查看</el-button>
            <el-button type="primary" size="small" text @click="openEditDialog(row)">编辑</el-button>
            <el-button type="success" size="small" text @click="handleVerify(row)">通过</el-button>
            <el-button type="danger" size="small" text @click="handleReject(row)">拒绝</el-button>
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
      title="知识原子详情"
      width="680px"
      top="6vh"
    >
      <template v-if="detailAtom">
        <el-descriptions :column="2" border>
          <el-descriptions-item label="ID">
            <code>{{ detailAtom.id }}</code>
          </el-descriptions-item>
          <el-descriptions-item label="类型">
            <el-tag :type="typeTagType(detailAtom.atom_type)">
              {{ typeLabel(detailAtom.atom_type) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="故障分类">{{ detailAtom.category_id || '—' }}</el-descriptions-item>
          <el-descriptions-item label="置信度">
            <span :style="{ color: confidenceColor(detailAtom.confidence) }">
              {{ (detailAtom.confidence * 100).toFixed(0) }}%
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="来源会话" :span="2">
            <code>{{ detailAtom.source_ref }}</code>
          </el-descriptions-item>
          <el-descriptions-item label="提炼时间" :span="2">
            {{ formatDate(detailAtom.created_at) }}
          </el-descriptions-item>
        </el-descriptions>

        <div class="json-section">
          <h4>触发条件</h4>
          <pre class="json-block">{{ JSON.stringify(detailAtom.trigger_json, null, 2) }}</pre>
        </div>
        <div class="json-section">
          <h4>知识内容</h4>
          <pre class="json-block">{{ JSON.stringify(detailAtom.content_json, null, 2) }}</pre>
        </div>
      </template>

      <template #footer>
        <el-button @click="detailDialogVisible = false">关闭</el-button>
        <el-button type="success" @click="detailDialogVisible = false; handleVerify(detailAtom!)">
          审核通过
        </el-button>
        <el-button type="danger" @click="detailDialogVisible = false; handleReject(detailAtom!)">
          拒绝删除
        </el-button>
      </template>
    </el-dialog>

    <!-- 编辑弹窗 -->
    <el-dialog
      v-model="editDialogVisible"
      title="编辑知识原子"
      width="600px"
    >
      <el-form :model="editForm" label-width="80px">
        <el-form-item label="内容描述">
          <el-input
            v-model="editForm.content_full_text"
            type="textarea"
            :rows="6"
            placeholder="完整诊断步骤描述（可被工程师直接参考执行）"
          />
        </el-form-item>
        <el-form-item label="置信度">
          <el-slider
            v-model="editForm.confidence"
            :min="0"
            :max="1"
            :step="0.05"
            :format-tooltip="(v: number) => `${(v * 100).toFixed(0)}%`"
            show-stops
          />
        </el-form-item>
      </el-form>

      <template #footer>
        <el-button @click="editDialogVisible = false">取消</el-button>
        <el-button type="primary" @click="submitEdit">保存修改</el-button>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.knowledge-review {
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

.atom-summary {
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

.mono-text {
  font-family: monospace;
  font-size: 12px;
  color: #606266;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.json-section {
  margin-top: 16px;
}

.json-section h4 {
  margin: 0 0 8px;
  font-size: 14px;
  color: #606266;
}

.json-block {
  background: #f8f9fa;
  border: 1px solid #ebeef5;
  border-radius: 4px;
  padding: 12px;
  font-size: 12px;
  font-family: monospace;
  overflow-x: auto;
  white-space: pre-wrap;
  word-break: break-all;
  margin: 0;
  max-height: 300px;
  overflow-y: auto;
  color: #303133;
}
</style>
