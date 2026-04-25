<script setup lang="ts">
import { ref, onMounted, computed } from 'vue'
import { useRoute, useRouter } from 'vue-router'
import { ElMessage } from 'element-plus'
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
import TerminalReplay from '@/components/TerminalReplay.vue'

const route = useRoute()
const router = useRouter()
const caseId = route.params.caseId as string

const apiClient = createApiClient('/api')
const caseApi = createCaseApi(apiClient)
const conversationApi = createConversationApi(apiClient)
const promptAuditApi = createPromptAuditApi(apiClient)
const auditLogApi = createAuditLogApi(apiClient)

// 鉴权头（admin 接口需要）
const internalToken = import.meta.env.VITE_INTERNAL_API_TOKEN || 'hci-dev-internal-token'
const authHeader = { Authorization: `Bearer ${internalToken}` }

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

// ── 关联 KBD 状态 ──
interface KbdInfo { id: number; support_id: string; title: string }
const resolvedKbdId = ref<number | null>(null)
const resolvedKbdInfo = ref<KbdInfo | null>(null)
// 编辑弹窗
const editKbdDialogVisible = ref(false)
const editKbdInputId = ref<number | null>(null)
const editKbdPreview = ref<KbdInfo | null>(null)
const editKbdPreviewLoading = ref(false)
const editKbdSaving = ref(false)

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
      // 提取最新 conversation 的 resolved_kbd_entry_id
      const latestConv = conversations[0]
      resolvedKbdId.value = latestConv.resolved_kbd_entry_id ?? null
      if (resolvedKbdId.value !== null) {
        await loadKbdInfo(resolvedKbdId.value)
      }
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

// ── 关联 KBD 功能 ──
async function loadKbdInfo(kbdId: number) {
  try {
    const resp = await fetch(`/api/admin/kbd/${kbdId}`, { headers: authHeader })
    if (!resp.ok) return
    const data = await resp.json()
    resolvedKbdInfo.value = { id: data.id, support_id: data.support_id, title: data.title }
  } catch {
    // 静默失败，不影响页面主流程
  }
}

function openEditKbdDialog() {
  editKbdInputId.value = resolvedKbdId.value
  editKbdPreview.value = resolvedKbdInfo.value
  editKbdDialogVisible.value = true
}

async function previewKbdById() {
  if (editKbdInputId.value === null || editKbdInputId.value === undefined) {
    editKbdPreview.value = null
    return
  }
  editKbdPreviewLoading.value = true
  try {
    const resp = await fetch(`/api/admin/kbd/${editKbdInputId.value}`, { headers: authHeader })
    if (!resp.ok) {
      editKbdPreview.value = null
      ElMessage.warning(`未找到 KBD-${editKbdInputId.value}`)
      return
    }
    const data = await resp.json()
    editKbdPreview.value = { id: data.id, support_id: data.support_id, title: data.title }
  } catch {
    editKbdPreview.value = null
    ElMessage.error('查询 KBD 信息失败')
  } finally {
    editKbdPreviewLoading.value = false
  }
}

async function saveResolvedKbd() {
  editKbdSaving.value = true
  try {
    const resp = await fetch(`/api/conversations/admin/cases/${encodeURIComponent(caseId)}/resolved_kbd`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json', ...authHeader },
      body: JSON.stringify({ kbd_entry_id: editKbdInputId.value ?? null }),
    })
    if (!resp.ok) throw new Error(`HTTP ${resp.status}`)
    const result = await resp.json()
    resolvedKbdId.value = result.new_kbd_entry_id
    resolvedKbdInfo.value = result.kbd_info ?? null
    editKbdDialogVisible.value = false
    ElMessage.success(result.changed ? '关联 KBD 已更新' : '未发生变化')
  } catch {
    ElMessage.error('保存失败，请重试')
  } finally {
    editKbdSaving.value = false
  }
}

function clearResolvedKbd() {
  editKbdInputId.value = null
  editKbdPreview.value = null
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
          <el-descriptions-item label="关联 KBD" :span="2">
            <span v-if="resolvedKbdInfo">
              <el-tag type="success" size="small" style="margin-right: 6px">{{ resolvedKbdInfo.support_id }}</el-tag>
              {{ resolvedKbdInfo.title }}
            </span>
            <span v-else-if="resolvedKbdId !== null" style="color: #909399">KBD-{{ resolvedKbdId }}（信息加载失败）</span>
            <span v-else style="color: #909399">未关联（AI 未确认根因 KBD）</span>
            <el-button size="small" text type="primary" style="margin-left: 8px" @click="openEditKbdDialog">
              修正
            </el-button>
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

          <!-- 终端历史标签页 -->
          <el-tab-pane label="终端历史" name="terminal-history">
            <template #label>
              <span>
                <el-icon><Monitor /></el-icon>
                终端历史
              </span>
            </template>
            <TerminalReplay :case-id="caseId" />
          </el-tab-pane>
        </el-tabs>
      </el-card>
    </template>
  </div>

  <!-- ── 关联 KBD 编辑弹窗 ── -->
  <el-dialog v-model="editKbdDialogVisible" title="修正关联 KBD" width="480px" :close-on-click-modal="false">
    <div class="edit-kbd-body">
      <p class="edit-kbd-hint">输入 KBD 条目 ID 进行绑定，或清空以解除关联。</p>
      <div class="edit-kbd-row">
        <el-input-number
          v-model="editKbdInputId"
          :min="1"
          :controls="false"
          placeholder="输入 KBD ID"
          style="width: 160px"
          @change="editKbdPreview = null"
        />
        <el-button size="small" :loading="editKbdPreviewLoading" @click="previewKbdById">查询</el-button>
        <el-button size="small" type="danger" text @click="clearResolvedKbd">清除</el-button>
      </div>
      <div v-if="editKbdPreview" class="kbd-preview">
        <el-tag type="success" size="small">{{ editKbdPreview.support_id }}</el-tag>
        <span class="kbd-preview-title">{{ editKbdPreview.title }}</span>
      </div>
      <div v-else-if="editKbdInputId === null" class="kbd-preview-empty">
        保存后将解除 KBD 关联，AI 未能确认根因。
      </div>
    </div>
    <template #footer>
      <el-button @click="editKbdDialogVisible = false">取消</el-button>
      <el-button type="primary" :loading="editKbdSaving" @click="saveResolvedKbd">保存</el-button>
    </template>
  </el-dialog>
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

/* 关联 KBD 编辑弹窗 */
.edit-kbd-hint {
  font-size: 13px;
  color: #606266;
  margin: 0 0 12px 0;
}

.edit-kbd-row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 12px;
}

.kbd-preview {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  background: #f0f9eb;
  border-radius: 4px;
  border: 1px solid #b3e19d;
}

.kbd-preview-title {
  font-size: 13px;
  color: #303133;
}

.kbd-preview-empty {
  font-size: 13px;
  color: #909399;
  padding: 8px 12px;
  background: #fafafa;
  border-radius: 4px;
  border: 1px solid #ebeef5;
}
</style>
