<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import {
  createApiClient,
  createCaseApi,
  createConversationApi,
  createPromptAuditApi,
  createAuditLogApi,
  STATUS_LABELS,
  STATUS_COLORS,
} from '@hci/shared'
import type { CaseResponse, MessageResponse } from '@hci/shared'

const route = useRoute()
const router = useRouter()
const caseId = route.params.caseId as string

const apiClient = createApiClient('/api')
const caseApi = createCaseApi(apiClient)
const conversationApi = createConversationApi(apiClient)
const promptAuditApi = createPromptAuditApi(apiClient)
const auditLogApi = createAuditLogApi(apiClient)

const caseDetail = ref<CaseResponse | null>(null)
const messages = ref<MessageResponse[]>([])
const loading = ref(true)

// AI 上下文标签页数据
const activeTab = ref('dialogue') // dialogue | ai-context
const promptAuditRecords = ref<any[]>([])
const promptAuditLoading = ref(false)

// 工具调用时间线数据
const auditLogs = ref<any[]>([])
const auditLogsLoading = ref(false)
const expandedLogId = ref<string | null>(null)

onMounted(async () => {
  try {
    // 加载工单详情
    const caseRes = await caseApi.getById(caseId)
    caseDetail.value = caseRes.data

    // 加载对话
    const convRes = await apiClient.get(`/conversations/case/${caseId}`)
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

// 加载 AI 上下文数据
async function loadAiContext() {
  if (promptAuditRecords.value.length > 0) return // 已加载

  promptAuditLoading.value = true
  auditLogsLoading.value = true

  try {
    // 加载 PromptAudit 记录
    const res = await promptAuditApi.listByCaseId(caseId, { limit: 100 })
    promptAuditRecords.value = res.data.records

    // 加载工具调用审计日志
    // 先获取该 case 关联的 conversation_id
    const conversationIds = promptAuditRecords.value
      .map((r) => r.conversation_id)
      .filter(Boolean)

    if (conversationIds.length > 0) {
      // 使用第一个 conversation_id 查询工具调用日志
      const sessionId = conversationIds[0]
      const logsRes = await auditLogApi.list({ session_id: sessionId, limit: 100 })
      auditLogs.value = logsRes.data.items
    }
  } catch (e) {
    console.error('加载 AI 上下文失败', e)
  } finally {
    promptAuditLoading.value = false
    auditLogsLoading.value = false
  }
}

// 监听标签页切换
function onTabChange(tabName: string) {
  if (tabName === 'ai-context') {
    loadAiContext()
  }
}

// 格式化日期
function formatDate(d: string | null) {
  if (!d) return '-'
  return new Date(d).toLocaleString('zh-CN')
}

// 获取状态样式
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

// 返回列表
function goBack() {
  router.push('/cases')
}

// 计算最新 PromptAudit 记录
const latestPromptAudit = computed(() => {
  return promptAuditRecords.value[0] || null
})

// 计算 system_prompt_chars 趋势数据（用于简单柱状图）
const promptCharsTrend = computed(() => {
  return promptAuditRecords.value
    .slice(0, 10)
    .reverse()
    .map((r) => ({
      value: r.system_prompt_chars || 0,
      time: formatDate(r.captured_at),
    }))
})

// 计算最大字符数（用于柱状图比例）
const maxPromptChars = computed(() => {
  const max = Math.max(...promptCharsTrend.value.map((t) => t.value), 1)
  return max
})

// 脱敏工具参数
function maskSensitiveParams(args: any): any {
  if (!args || typeof args !== 'object') return args

  const sensitiveKeys = ['password', 'token', 'key', 'secret', 'auth', 'credential', 'api_key']
  const masked: any = {}

  for (const [k, v] of Object.entries(args)) {
    const lowerKey = k.toLowerCase()
    if (sensitiveKeys.some((sk) => lowerKey.includes(sk))) {
      masked[k] = '***'
    } else if (typeof v === 'object' && v !== null) {
      masked[k] = maskSensitiveParams(v)
    } else {
      masked[k] = v
    }
  }

  return masked
}

// 切换日志展开状态
function toggleLogExpand(logId: string) {
  expandedLogId.value = expandedLogId.value === logId ? null : logId
}

// 获取风险等级标签
function getRiskLabel(level: number): { text: string; type: '' | 'success' | 'warning' | 'danger' | 'info' } {
  switch (level) {
    case 1:
      return { text: '低', type: 'success' }
    case 2:
      return { text: '中', type: 'warning' }
    case 3:
      return { text: '高', type: 'danger' }
    default:
      return { text: `未知(${level})`, type: 'info' }
  }
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

      <!-- 标签页：对话记录 和 AI 上下文 -->
      <el-card style="margin-top: 16px">
        <el-tabs v-model="activeTab" @tab-change="onTabChange">
          <!-- 对话记录标签页 -->
          <el-tab-pane label="对话记录" name="dialogue">
            <template #label>
              <span>
                <el-icon><ChatDotRound /></el-icon>
                对话记录 ({{ messages.length }})
              </span>
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
          </el-tab-pane>

          <!-- AI 上下文标签页 -->
          <el-tab-pane label="AI 上下文" name="ai-context">
            <template #label>
              <span>
                <el-icon><Monitor /></el-icon>
                AI 上下文
              </span>
            </template>
            <div v-loading="promptAuditLoading">
              <!-- 无数据 -->
              <div v-if="!latestPromptAudit" class="empty-msg">
                <el-empty description="暂无 AI 上下文数据" />
              </div>

              <div v-else class="ai-context-container">
                <!-- 左栏：关键指标 -->
                <div class="metrics-section">
                  <h4>知识检索指标</h4>
                  <el-descriptions :column="1" border size="small">
                    <el-descriptions-item label="是否命中 SOP">
                      <el-tag :type="latestPromptAudit.has_sop ? 'success' : 'info'" size="small">
                        {{ latestPromptAudit.has_sop ? '是' : '否' }}
                      </el-tag>
                    </el-descriptions-item>
                    <el-descriptions-item label="KB 命中 Chunk 数">
                      {{ latestPromptAudit.kb_chunks_count || 0 }}
                    </el-descriptions-item>
                    <el-descriptions-item label="KB 最高相似度">
                      <el-tag
                        :type="
                          (latestPromptAudit.kb_top_score || 0) > 0.8
                            ? 'success'
                            : (latestPromptAudit.kb_top_score || 0) > 0.5
                              ? 'warning'
                              : 'info'
                        "
                        size="small"
                      >
                        {{ ((latestPromptAudit.kb_top_score || 0) * 100).toFixed(1) }}%
                      </el-tag>
                    </el-descriptions-item>
                    <el-descriptions-item label="对话轮数">
                      {{ latestPromptAudit.message_count || 0 }}
                    </el-descriptions-item>
                    <el-descriptions-item label="System Prompt 字符数">
                      {{ latestPromptAudit.system_prompt_chars || 0 }}
                    </el-descriptions-item>
                    <el-descriptions-item label="用户评分">
                      <span v-if="latestPromptAudit.user_rating">
                        <el-rate
                          :model-value="latestPromptAudit.user_rating"
                          :max="5"
                          disabled
                          show-score
                        />
                      </span>
                      <span v-else>-</span>
                    </el-descriptions-item>
                  </el-descriptions>

                  <!-- System Prompt 字符数趋势 -->
                  <h4 style="margin-top: 16px">Prompt 长度趋势（最近10次）</h4>
                  <div v-if="promptCharsTrend.length === 0" class="empty-trend">
                    暂无趋势数据
                  </div>
                  <div v-else class="trend-chart">
                    <div
                      v-for="(item, idx) in promptCharsTrend"
                      :key="idx"
                      class="trend-bar-item"
                    >
                      <div
                        class="trend-bar"
                        :style="{
                          height: `${(item.value / maxPromptChars) * 100}px`,
                          minHeight: '4px',
                        }"
                      ></div>
                      <div class="trend-label">{{ idx + 1 }}</div>
                    </div>
                  </div>
                  <div class="trend-legend">
                    平均值: {{ Math.round(promptCharsTrend.reduce((a, b) => a + b.value, 0) / promptCharsTrend.length) }} 字符
                  </div>
                </div>

                <!-- 右栏：工具调用时间线 -->
                <div class="timeline-section">
                  <h4>工具调用时间线</h4>
                  <div v-loading="auditLogsLoading">
                    <div v-if="auditLogs.length === 0" class="empty-msg">
                      <el-empty description="暂无工具调用记录" />
                    </div>
                    <div v-else class="timeline-list">
                      <div
                        v-for="log in auditLogs"
                        :key="log.id"
                        class="timeline-item"
                        :class="{ expanded: expandedLogId === log.id }"
                      >
                        <!-- 时间线头部 -->
                        <div class="timeline-header" @click="toggleLogExpand(log.id)">
                          <div class="timeline-icon">
                            <el-icon v-if="log.error"><CircleClose /></el-icon>
                            <el-icon v-else><CircleCheck /></el-icon>
                          </div>
                          <div class="timeline-content">
                            <div class="timeline-title">
                              <span class="tool-name">{{ log.tool_name }}</span>
                              <el-tag :type="getRiskLabel(log.risk_level).type" size="small">
                                {{ getRiskLabel(log.risk_level).text }}
                              </el-tag>
                              <span v-if="log.duration_ms" class="duration">
                                {{ log.duration_ms }}ms
                              </span>
                            </div>
                            <div class="timeline-time">{{ formatDate(log.started_at) }}</div>
                          </div>
                          <div class="timeline-status">
                            <el-tag :type="log.error ? 'danger' : 'success'" size="small">
                              {{ log.error ? '失败' : '成功' }}
                            </el-tag>
                            <el-icon class="expand-icon">
                              <ArrowDown v-if="expandedLogId !== log.id" />
                              <ArrowUp v-else />
                            </el-icon>
                          </div>
                        </div>

                        <!-- 展开详情 -->
                        <div v-if="expandedLogId === log.id" class="timeline-detail">
                          <el-descriptions :column="1" border size="small">
                            <el-descriptions-item label="工具参数">
                              <pre class="code-block">{{ JSON.stringify(maskSensitiveParams(log.tool_args), null, 2) }}</pre>
                            </el-descriptions-item>
                            <el-descriptions-item v-if="log.error" label="错误信息">
                              <span class="error-text">{{ log.error }}</span>
                            </el-descriptions-item>
                            <el-descriptions-item label="Trace ID">
                              <code>{{ log.trace_id || '-' }}</code>
                            </el-descriptions-item>
                          </el-descriptions>
                        </div>
                      </div>
                    </div>
                  </div>
                </div>
              </div>
            </div>
          </el-tab-pane>
        </el-tabs>
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

/* AI 上下文样式 */
.ai-context-container {
  display: grid;
  grid-template-columns: 320px 1fr;
  gap: 20px;
}

.metrics-section h4,
.timeline-section h4 {
  margin: 0 0 12px 0;
  font-size: 14px;
  font-weight: 600;
  color: #303133;
}

/* 趋势图样式 */
.empty-trend {
  padding: 20px;
  text-align: center;
  color: #909399;
}

.trend-chart {
  display: flex;
  align-items: flex-end;
  gap: 4px;
  height: 120px;
  padding: 10px;
  background: #f5f7fa;
  border-radius: 4px;
}

.trend-bar-item {
  flex: 1;
  display: flex;
  flex-direction: column;
  align-items: center;
}

.trend-bar {
  width: 100%;
  background: #409eff;
  border-radius: 2px;
  transition: height 0.3s;
}

.trend-label {
  margin-top: 4px;
  font-size: 10px;
  color: #909399;
}

.trend-legend {
  margin-top: 8px;
  font-size: 12px;
  color: #606266;
  text-align: center;
}

/* 时间线样式 */
.timeline-list {
  max-height: 500px;
  overflow-y: auto;
}

.timeline-item {
  border-bottom: 1px solid #ebeef5;
  padding: 12px 0;
}

.timeline-item:last-child {
  border-bottom: none;
}

.timeline-header {
  display: flex;
  align-items: flex-start;
  gap: 12px;
  cursor: pointer;
}

.timeline-icon {
  width: 24px;
  height: 24px;
  display: flex;
  align-items: center;
  justify-content: center;
  color: #67c23a;
}

.timeline-item .timeline-icon .el-icon {
  color: #67c23a;
}

.timeline-item:has(.el-tag--danger) .timeline-icon .el-icon {
  color: #f56c6c;
}

.timeline-content {
  flex: 1;
}

.timeline-title {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.tool-name {
  font-weight: 600;
  font-size: 14px;
  color: #303133;
}

.duration {
  font-size: 12px;
  color: #909399;
}

.timeline-time {
  font-size: 12px;
  color: #909399;
}

.timeline-status {
  display: flex;
  align-items: center;
  gap: 8px;
}

.expand-icon {
  color: #909399;
  font-size: 12px;
}

.timeline-detail {
  margin-top: 12px;
  margin-left: 36px;
  padding: 12px;
  background: #f5f7fa;
  border-radius: 4px;
}

.code-block {
  margin: 0;
  padding: 8px;
  background: #2c3e50;
  color: #fff;
  border-radius: 4px;
  font-size: 12px;
  max-height: 200px;
  overflow: auto;
}

.error-text {
  color: #f56c6c;
}

@media (max-width: 768px) {
  .ai-context-container {
    grid-template-columns: 1fr;
  }
}
</style>
