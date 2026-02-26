<script setup lang="ts">
import { ref, onMounted } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { createApiClient, createCaseApi, createConversationApi, STATUS_LABELS, STATUS_COLORS } from '@hci/shared'
import type { CaseResponse, MessageResponse } from '@hci/shared'

const route = useRoute()
const router = useRouter()
const caseId = route.params.caseId as string

const apiClient = createApiClient('/api')
const caseApi = createCaseApi(apiClient)
const conversationApi = createConversationApi(apiClient)

const caseDetail = ref<CaseResponse | null>(null)
const messages = ref<MessageResponse[]>([])
const loading = ref(true)

onMounted(async () => {
  try {
    // 加载工单详情
    const caseRes = await caseApi.getById(caseId)
    caseDetail.value = caseRes.data

    // 加载对话
    const convRes = await apiClient.get(`/api/conversations/case/${caseId}`)
    const conversations = convRes.data as any[]
    if (conversations.length > 0) {
      const msgRes = await conversationApi.getMessages(conversations[0].conversation_id)
      messages.value = msgRes.data
    }
  } catch (e) {
    console.error('加载工单详情失败', e)
  } finally {
    loading.value = false
  }
})

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

function goBack() {
  router.push('/cases')
}
</script>

<template>
  <div v-loading="loading" class="case-detail">
    <el-page-header @back="goBack" :title="'返回列表'">
      <template #content>
        <span class="page-header-title">工单 {{ caseId }}</span>
      </template>
    </el-page-header>

    <template v-if="caseDetail">
      <!-- 基本信息 -->
      <el-card style="margin-top: 16px">
        <template #header>基本信息</template>
        <el-descriptions :column="2" border>
          <el-descriptions-item label="工单号">{{ caseDetail.case_id }}</el-descriptions-item>
          <el-descriptions-item label="状态">
            <el-tag :type="getStatusType(caseDetail.status)" size="small">
              {{ STATUS_LABELS[caseDetail.status] || caseDetail.status }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="客户端ID">{{ caseDetail.client_id }}</el-descriptions-item>
          <el-descriptions-item label="Trace ID">
            <code style="font-size: 12px">{{ caseDetail.trace_id || '-' }}</code>
          </el-descriptions-item>
          <el-descriptions-item label="标题" :span="2">{{ caseDetail.title }}</el-descriptions-item>
          <el-descriptions-item label="描述" :span="2">
            {{ caseDetail.description || '-' }}
          </el-descriptions-item>
          <el-descriptions-item label="创建时间">{{ formatDate(caseDetail.created_at) }}</el-descriptions-item>
          <el-descriptions-item label="关闭时间">
            {{ caseDetail.closed_at ? formatDate(caseDetail.closed_at) : '-' }}
          </el-descriptions-item>
        </el-descriptions>
      </el-card>

      <!-- 对话记录 -->
      <el-card style="margin-top: 16px">
        <template #header>
          对话记录 ({{ messages.length }} 条消息)
        </template>
        <div v-if="messages.length === 0" class="empty-msg">
          <el-empty description="暂无对话记录" />
        </div>
        <div v-else class="message-list">
          <div
            v-for="msg in messages"
            :key="msg.message_id"
            class="msg-item"
            :class="`msg-${msg.role}`"
          >
            <div class="msg-header">
              <el-tag
                :type="msg.role === 'user' ? '' : msg.role === 'assistant' ? 'success' : 'info'"
                size="small"
              >
                {{ msg.role === 'user' ? '用户' : msg.role === 'assistant' ? 'AI' : '系统' }}
              </el-tag>
              <span class="msg-time">{{ formatDate(msg.created_at) }}</span>
            </div>
            <div class="msg-content">{{ msg.content }}</div>
          </div>
        </div>
      </el-card>
    </template>
  </div>
</template>

<style scoped>
.page-header-title {
  font-size: 16px;
  font-weight: 600;
}

.message-list {
  max-height: 500px;
  overflow-y: auto;
}

.msg-item {
  padding: 12px;
  border-bottom: 1px solid #f0f0f0;
}

.msg-item:last-child {
  border-bottom: none;
}

.msg-header {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 6px;
}

.msg-time {
  font-size: 12px;
  color: #909399;
}

.msg-content {
  font-size: 14px;
  line-height: 1.6;
  white-space: pre-wrap;
  word-break: break-word;
  color: #303133;
}

.msg-assistant .msg-content {
  background: #f0f9eb;
  padding: 10px;
  border-radius: 6px;
}

.msg-user .msg-content {
  background: #ecf5ff;
  padding: 10px;
  border-radius: 6px;
}

.empty-msg {
  padding: 20px 0;
}
</style>
