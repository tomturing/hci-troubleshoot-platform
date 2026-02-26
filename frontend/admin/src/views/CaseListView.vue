<script setup lang="ts">
import { ref, onMounted, watch } from 'vue'
import { useRouter } from 'vue-router'
import { createApiClient, createCaseApi, STATUS_LABELS } from '@hci/shared'
import type { CaseResponse, CaseListResponse } from '@hci/shared'

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
  page: 1,
  pageSize: 20,
})

/** 加载数据 */
async function loadData() {
  loading.value = true
  try {
    const res = await caseApi.listAll({
      skip: (filters.value.page - 1) * filters.value.pageSize,
      limit: filters.value.pageSize,
      status: filters.value.status || undefined,
      client_id: filters.value.client_id || undefined,
    })
    const data: CaseListResponse = res.data
    tableData.value = data.items
    total.value = data.total
  } catch (e) {
    console.error('加载工单失败', e)
  } finally {
    loading.value = false
  }
}

function handleSearch() {
  filters.value.page = 1
  loadData()
}

function handleReset() {
  filters.value = { status: '', client_id: '', page: 1, pageSize: 20 }
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

onMounted(loadData)
</script>

<template>
  <div class="case-list">
    <!-- 筛选栏 -->
    <el-card class="filter-card">
      <el-form :inline="true" :model="filters" size="default">
        <el-form-item label="状态">
          <el-select v-model="filters.status" placeholder="全部" clearable style="width: 140px">
            <el-option
              v-for="opt in statusOptions"
              :key="opt.value"
              :label="opt.label"
              :value="opt.value"
            />
          </el-select>
        </el-form-item>
        <el-form-item label="客户端ID">
          <el-input
            v-model="filters.client_id"
            placeholder="输入客户端ID"
            clearable
            style="width: 220px"
          />
        </el-form-item>
        <el-form-item>
          <el-button type="primary" @click="handleSearch">
            <el-icon><Search /></el-icon>搜索
          </el-button>
          <el-button @click="handleReset">重置</el-button>
        </el-form-item>
      </el-form>
    </el-card>

    <!-- 数据表格 -->
    <el-card style="margin-top: 16px">
      <el-table :data="tableData" v-loading="loading" stripe style="width: 100%">
        <el-table-column prop="case_id" label="工单号" width="170" />
        <el-table-column prop="client_id" label="客户端ID" width="200" show-overflow-tooltip />
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
        <el-table-column label="操作" width="100" fixed="right">
          <template #default="{ row }">
            <el-button type="primary" link size="small" @click="viewDetail(row)">
              查看
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
  </div>
</template>

<style scoped>
.filter-card :deep(.el-form-item) {
  margin-bottom: 0;
}

.pagination-wrap {
  display: flex;
  justify-content: flex-end;
  margin-top: 16px;
}
</style>
