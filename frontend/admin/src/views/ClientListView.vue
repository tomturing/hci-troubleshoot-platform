<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { createApiClient, createCaseApi } from '@hci/shared'
import type { ClientInfo } from '@hci/shared'

const apiClient = createApiClient('/api')
const caseApi = createCaseApi(apiClient)

const clients = ref<ClientInfo[]>([])
const total = ref(0)
const loading = ref(true)

onMounted(async () => {
  try {
    const res = await caseApi.clients()
    clients.value = res.data.items
    total.value = res.data.total
  } catch (e) {
    console.error('加载客户端列表失败', e)
  } finally {
    loading.value = false
  }
})

function formatDate(d: string | null) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN')
}
</script>

<template>
  <div class="client-list">
    <el-card>
      <template #header>
        <span>客户端列表 ({{ total }})</span>
      </template>
      <el-table :data="clients" v-loading="loading" stripe style="width: 100%">
        <el-table-column type="index" width="60" label="#" />
        <el-table-column prop="client_id" label="客户端ID" show-overflow-tooltip />
        <el-table-column prop="case_count" label="工单数量" width="120" align="center">
          <template #default="{ row }">
            <el-tag>{{ row.case_count }}</el-tag>
          </template>
        </el-table-column>
        <el-table-column prop="last_case_at" label="最近工单时间" width="200">
          <template #default="{ row }">{{ formatDate(row.last_case_at) }}</template>
        </el-table-column>
      </el-table>
    </el-card>
  </div>
</template>
