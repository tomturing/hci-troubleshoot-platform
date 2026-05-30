<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { createApiClient, createCaseApi, STATUS_LABELS } from '@hci/shared'
import type { CaseResponse, CaseListResponse } from '@hci/shared'
import { Search, Edit, RefreshLeft } from '@element-plus/icons-vue'
import { ElMessage } from 'element-plus'

const router = useRouter()
const apiClient = createApiClient('/api')
const caseApi = createCaseApi(apiClient)

const tableData = ref<CaseResponse[]>([])
const total = ref(0)
const loading = ref(false)

// 筛选参数
const filters = ref({
  status: '' as string,
  client_id: '' as string,
  case_id: '' as string,
  title: '' as string,
  dateRange: null as [Date, Date] | null,
  page: 1,
  pageSize: 20,
})

/** 加载数据 */
async function loadData() {
  loading.value = true
  try {
    let startTime: string | undefined = undefined
    let endTime: string | undefined = undefined
    if (filters.value.dateRange && filters.value.dateRange.length === 2) {
      startTime = filters.value.dateRange[0].toISOString()
      endTime = filters.value.dateRange[1].toISOString()
    }

    const res = await caseApi.listAll({
      skip: (filters.value.page - 1) * filters.value.pageSize,
      limit: filters.value.pageSize,
      status: filters.value.status || undefined,
      client_id: filters.value.client_id || undefined,
      case_id: filters.value.case_id || undefined,
      title: filters.value.title || undefined,
      start_time: startTime,
      end_time: endTime,
    })
    const data: CaseListResponse = res.data
    tableData.value = data.items
    total.value = data.total
  } catch (e) {
    console.error('加载工单失败', e)
    ElMessage.error('加载工单数据失败')
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  filters.value.page = 1
  loadData()
}

function handleReset() {
  filters.value = {
    status: '',
    client_id: '',
    case_id: '',
    title: '',
    dateRange: null,
    page: 1,
    pageSize: 20,
  }
  loadData()
}

function handleSizeChange(val: number) {
  filters.value.pageSize = val
  filters.value.page = 1
  loadData()
}

function handleCurrentChange(val: number) {
  filters.value.page = val
  loadData()
}

function viewDetail(row: CaseResponse) {
  router.push(`/cases/${row.case_id}`)
}

function formatDate(d: string) {
  return new Date(d).toLocaleString('zh-CN')
}

function getStatusType(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    created: 'warning', confirmed: '', in_progress: '',
    resolved: 'success', closed: 'info', cancelled: 'danger',
  }
  return map[status] || 'info'
}

const statusOptions = [
  { label: '待确认', value: 'created' },
  { label: '已确认', value: 'confirmed' },
  { label: '处理中', value: 'in_progress' },
  { label: '已解决', value: 'resolved' },
  { label: '已关闭', value: 'closed' },
  { label: '已取消', value: 'cancelled' },
]

// ──────────────────────────────────────────────────────────────────────────────
// 工单编辑 Dialog 状态及逻辑
// ──────────────────────────────────────────────────────────────────────────────
const editDialogVisible = ref(false)
const editSaving = ref(false)
const currentEditCaseId = ref('')
const editForm = ref({
  title: '',
  description: '',
  status: 'created',
  priority: 'medium',
  category: '',
  assistant_type: 'htp-agent',
})

const priorityOptions = [
  { label: '高', value: 'high' },
  { label: '中', value: 'medium' },
  { label: '低', value: 'low' },
]

const assistantOptions = [
  { label: 'HTP Agent', value: 'htp-agent' },
  { label: 'OPS Agent', value: 'ops-agent' },
  { label: 'PAI Agent', value: 'pai-agent' },
]

function openEditDialog(row: CaseResponse) {
  currentEditCaseId.value = row.case_id
  editForm.value = {
    title: row.title || '',
    description: row.description || '',
    status: row.status || 'created',
    priority: row.priority || 'medium',
    category: row.category || '',
    assistant_type: row.assistant_type || 'htp-agent',
  }
  editDialogVisible.value = true
}

async function handleSaveEdit() {
  if (!editForm.value.title.trim()) {
    ElMessage.warning('工单标题不能为空')
    return
  }
  editSaving.value = true
  try {
    await caseApi.update(currentEditCaseId.value, {
      title: editForm.value.title.trim(),
      description: editForm.value.description.trim() || null,
      status: editForm.value.status as any,
      priority: editForm.value.priority,
      category: editForm.value.category.trim() || null,
      assistant_type: editForm.value.assistant_type,
    })
    ElMessage.success('编辑工单成功')
    editDialogVisible.value = false
    loadData()
  } catch (e) {
    console.error('编辑工单失败', e)
    ElMessage.error('编辑工单失败，请重试')
  } finally {
    editSaving.value = false
  }
}

onMounted(loadData)
</script>

<template>
  <div class="case-list">
    <!-- 筛选栏 -->
    <el-card class="filter-card">
      <el-form :inline="true" :model="filters" size="default">
        <el-form-item label="工单号">
          <el-input
            v-model="filters.case_id"
            placeholder="输入工单号模糊搜索"
            clearable
            style="width: 170px"
          />
        </el-form-item>
        <el-form-item label="客户端ID">
          <el-input
            v-model="filters.client_id"
            placeholder="输入客户端ID模糊搜索"
            clearable
            style="width: 190px"
          />
        </el-form-item>
        <el-form-item label="标题">
          <el-input
            v-model="filters.title"
            placeholder="输入标题模糊搜索"
            clearable
            style="width: 170px"
          />
        </el-form-item>
        <el-form-item label="状态">
          <el-select v-model="filters.status" placeholder="全部" clearable style="width: 120px">
            <el-option
              v-for="opt in statusOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
         </el-form-item>
         <el-form-item label="创建时间">
           <el-date-picker
             v-model="filters.dateRange"
             type="datetimerange"
             range-separator="至"
             start-placeholder="开始时间"
             end-placeholder="结束时间"
             style="width: 320px"
           />
         </el-form-item>
         <el-form-item>
           <el-button type="primary" @click="handleSearch">
             <el-icon><Search /></el-icon>搜索
           </el-button>
           <el-button @click="handleReset" :icon="RefreshLeft">重置</el-button>
         </el-form-item>
      </el-form>
    </el-card>

    <!-- 数据表格 -->
    <el-card style="margin-top: 16px">
      <el-table :data="tableData" v-loading="loading" stripe style="width: 100%">
        <el-table-column prop="case_id" label="工单号" width="160" />
        <el-table-column prop="client_id" label="客户端ID" width="180" show-overflow-tooltip />
        <el-table-column prop="title" label="标题" show-overflow-tooltip />
        <el-table-column prop="status" label="状态" width="90" align="center">
          <template #default="{ row }">
            <el-tag :type="getStatusType(row.status)" size="small">
              {{ STATUS_LABELS[row.status as keyof typeof STATUS_LABELS] || row.status }}
            </el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="created_at" label="创建时间" width="170">
          <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
        </el-table-column>
        <el-table-column label="操作" width="130" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="viewDetail(row)">
              查看
            </el-button>
            <el-button type="warning" link size="small" :icon="Edit" @click="openEditDialog(row)">
              编辑
            </el-button>
          </template>
        </el-table-column>
      </el-table>

      <!-- 分页 -->
      <div class="pagination-wrap">
        <el-pagination
          v-model:current-page="filters.page"
          v-model:page-size="filters.pageSize"
          :page-sizes="[10, 20, 50]"
          :total="total"
          layout="total, sizes, prev, pager, next"
          @size-change="handleSizeChange"
          @current-change="handleCurrentChange"
        />
      </div>
    </el-card>

    <!-- 编辑工单 Dialog -->
    <el-dialog
      v-model="editDialogVisible"
      :title="`编辑工单 - ${currentEditCaseId}`"
      width="580px"
      destroy-on-close
    >
      <el-form :model="editForm" label-width="90px" style="padding-right: 20px">
        <el-form-item label="工单标题" required>
          <el-input v-model="editForm.title" placeholder="请输入工单标题" maxlength="200" show-word-limit />
        </el-form-item>
        <el-form-item label="工单描述">
          <el-input
            v-model="editForm.description"
            type="textarea"
            :autosize="{ minRows: 3, maxRows: 6 }"
            placeholder="请输入工单描述内容"
          />
        </el-form-item>
        <el-form-item label="工单状态" required>
          <el-select v-model="editForm.status" placeholder="选择状态" style="width: 100%">
            <el-option
              v-for="opt in statusOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="优先级">
          <el-select v-model="editForm.priority" placeholder="选择优先级" style="width: 100%">
            <el-option
              v-for="opt in priorityOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="工单分类">
          <el-input v-model="editForm.category" placeholder="选填，如: vm / storage / network" clearable />
        </el-form-item>
        <el-form-item label="助手类型" required>
          <el-select v-model="editForm.assistant_type" placeholder="选择分配的助手" style="width: 100%">
            <el-option
              v-for="opt in assistantOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
      </el-form>
      <template #footer>
        <div class="dialog-footer">
          <el-button @click="editDialogVisible = false">取消</el-button>
          <el-button type="primary" :loading="editSaving" @click="handleSaveEdit">确认修改</el-button>
        </div>
      </template>
    </el-dialog>
  </div>
</template>

<style scoped>
.filter-card :deep(.el-form-item) {
  margin-bottom: 12px;
  margin-right: 18px;
}

.filter-card :deep(.el-form) {
  display: flex;
  flex-wrap: wrap;
  align-items: center;
}

.pagination-wrap {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}

.dialog-footer {
  display: flex;
  justify-content: flex-end;
  gap: 12px;
}
</style>
