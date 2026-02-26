<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { createApiClient, createCaseApi, STATUS_LABELS } from '@hci/shared'
import type { CaseStatsResponse, ClientListResponse, CaseResponse } from '@hci/shared'

const apiClient = createApiClient('/api')
const caseApi = createCaseApi(apiClient)

const stats = ref<CaseStatsResponse>({ total: 0, by_status: {} })
const clients = ref<ClientListResponse>({ items: [], total: 0 })
const recentCases = ref<CaseResponse[]>([])
const loading = ref(true)

onMounted(async () => {
  try {
    const [statsRes, clientsRes, casesRes] = await Promise.all([
      caseApi.stats(),
      caseApi.clients(),
      caseApi.listAll({ limit: 5 }),
    ])
    stats.value = statsRes.data
    clients.value = clientsRes.data
    recentCases.value = casesRes.data.items
  } catch (e) {
    console.error('加载仪表盘数据失败', e)
  } finally {
    loading.value = false
  }
})

function formatDate(d: string) {
  return new Date(d).toLocaleString('zh-CN')
}
</script>

<template>
  <div v-loading="loading" class="dashboard">
    <!-- 统计卡片 -->
    <el-row :gutter="16" class="stat-cards">
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-value">{{ stats.total }}</div>
            <div class="stat-label">总工单数</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-value text-warning">{{ stats.by_status['created'] || 0 }}</div>
            <div class="stat-label">待确认</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-value text-primary">
              {{ (stats.by_status['confirmed'] || 0) + (stats.by_status['in_progress'] || 0) }}
            </div>
            <div class="stat-label">处理中</div>
          </div>
        </el-card>
      </el-col>
      <el-col :span="6">
        <el-card shadow="hover">
          <div class="stat-card">
            <div class="stat-value text-success">{{ stats.by_status['closed'] || 0 }}</div>
            <div class="stat-label">已关闭</div>
          </div>
        </el-card>
      </el-col>
    </el-row>

    <el-row :gutter="16" style="margin-top: 16px">
      <!-- 最近工单 -->
      <el-col :span="14">
        <el-card>
          <template #header>
            <span>最近工单</span>
          </template>
          <el-table :data="recentCases" size="small" stripe>
            <el-table-column prop="case_id" label="工单号" width="160" />
            <el-table-column prop="title" label="标题" show-overflow-tooltip />
            <el-table-column prop="status" label="状态" width="90">
              <template #default="{ row }">
                <el-tag :type="getStatusType(row.status)" size="small">
                  {{ STATUS_LABELS[row.status as keyof typeof STATUS_LABELS] || row.status }}
                </el-tag>
              </template>
            </el-table-column>
            <el-table-column prop="created_at" label="创建时间" width="170">
              <template #default="{ row }">{{ formatDate(row.created_at) }}</template>
            </el-table-column>
          </el-table>
        </el-card>
      </el-col>

      <!-- 客户端统计 -->
      <el-col :span="10">
        <el-card>
          <template #header>
            <span>活跃客户端 ({{ clients.total }})</span>
          </template>
          <el-table :data="clients.items.slice(0, 10)" size="small" stripe>
            <el-table-column prop="client_id" label="客户端ID" show-overflow-tooltip />
            <el-table-column prop="case_count" label="工单数" width="80" align="center" />
          </el-table>
        </el-card>
      </el-col>
    </el-row>
  </div>
</template>

<script lang="ts">
function getStatusType(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
  const map: Record<string, '' | 'success' | 'warning' | 'danger' | 'info'> = {
    created: 'warning',
    confirmed: '',
    in_progress: '',
    resolved: 'success',
    closed: 'info',
    cancelled: 'danger',
  }
  return map[status] || 'info'
}
</script>

<style scoped>
.stat-cards {
  margin-bottom: 8px;
}

.stat-card {
  text-align: center;
  padding: 8px 0;
}

.stat-value {
  font-size: 32px;
  font-weight: 700;
  color: #303133;
}

.stat-label {
  font-size: 14px;
  color: #909399;
  margin-top: 4px;
}

.text-warning {
  color: #e6a23c;
}

.text-primary {
  color: #409eff;
}

.text-success {
  color: #67c23a;
}
</style>
