<script setup lang="ts">
import { ref, onMounted, reactive, watch } from 'vue'
import { Monitor, Document, Search, Refresh, CopyDocument, CircleCheck, Warning, Cpu, DataAnalysis } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { ElMessage } from 'element-plus'

const apiClient = createApiClient('/api')

// ===== 视图选项卡 =====
const activeTab = ref('grafana')

// ==========================================
// 1. Grafana 监控面板 (Grafana iframe)
// ==========================================
const grafanaUrl = ref('')
const grafanaReady = ref(false)
const monitorLoading = ref(true)

async function detectGrafana() {
  const hostname = window.location.hostname
  const protocol = window.location.protocol
  const port = window.location.port ? `:${window.location.port}` : ''

  let baseUrl = ''
  if (hostname.startsWith('admin.')) {
    const grafanaHost = hostname.replace('admin.', 'grafana.')
    baseUrl = `${protocol}//${grafanaHost}`
  } else if (hostname === 'localhost' || hostname === '127.0.0.1') {
    baseUrl = 'http://localhost:3000'
  } else {
    baseUrl = `${protocol}//${hostname}${port}/grafana`
  }

  // 拼接特定 Dashboard UID 路径，并使用 kiosk 模式隐藏 Grafana 的导航和 Sign In 布局，实现免登录嵌入体验
  grafanaUrl.value = `${baseUrl}/d/hci-overview?orgId=1&kiosk`
  grafanaReady.value = true
  monitorLoading.value = false
}

function openGrafana() {
  // 新窗口打开时去掉 kiosk 模式，方便用户正常交互
  const fullUrl = grafanaUrl.value.replace('&kiosk', '')
  window.open(fullUrl, '_blank')
}

// ==========================================
// 1.5. Langfuse 控制台 (Langfuse iframe)
// ==========================================
const langfuseUrl = ref('')
const langfuseReady = ref(false)
const langfuseLoading = ref(true)

async function detectLangfuse() {
  const hostname = window.location.hostname
  const protocol = window.location.protocol
  const port = window.location.port ? `:${window.location.port}` : ''

  let baseUrl = ''
  if (hostname.startsWith('admin.')) {
    const langfuseHost = hostname.replace('admin.', 'langfuse.')
    baseUrl = `${protocol}//${langfuseHost}`
  } else if (hostname === 'localhost' || hostname === '127.0.0.1') {
    baseUrl = 'http://localhost:13000'
  } else {
    baseUrl = `${protocol}//${hostname}${port}/langfuse`
  }

  langfuseUrl.value = baseUrl
  langfuseReady.value = true
  langfuseLoading.value = false
}

function openLangfuse() {
  window.open(langfuseUrl.value, '_blank')
}

// ==========================================
// 2. 审计日志 (Prompt Audit Logs)
// ==========================================
const auditLoading = ref(false)
const auditLogs = ref<any[]>([])
const totalLogs = ref(0)

const filterForm = reactive({
  caseId: '',
  traceId: '',
})

const pagination = reactive({
  page: 1,
  pageSize: 20,
})

async function fetchAuditLogs() {
  auditLoading.value = true
  try {
    const offset = (pagination.page - 1) * pagination.pageSize
    const res = await apiClient.get('/audit-logs/prompts', {
      params: {
        case_id: filterForm.caseId.trim() || undefined,
        trace_id: filterForm.traceId.trim() || undefined,
        limit: pagination.pageSize,
        offset: offset,
      },
    })
    auditLogs.value = res.data.items || []
    totalLogs.value = res.data.total || 0
  } catch (e: any) {
    console.error('加载审计日志失败', e)
    ElMessage.error(e.response?.data?.detail || '加载审计日志失败')
  } finally {
    auditLoading.value = false
  }
}

function handleSearch() {
  pagination.page = 1
  fetchAuditLogs()
}

function handleReset() {
  filterForm.caseId = ''
  filterForm.traceId = ''
  pagination.page = 1
  fetchAuditLogs()
}

function handlePageChange(page: number) {
  pagination.page = page
  fetchAuditLogs()
}

function handlePageSizeChange(size: number) {
  pagination.pageSize = size
  pagination.page = 1
  fetchAuditLogs()
}

// ==========================================
// 3. 详情抽屉 (Detail Drawer)
// ==========================================
const drawerVisible = ref(false)
const activeDrawerTab = ref('messages')
const currentLog = ref<any>(null)

function viewLogDetails(row: any) {
  currentLog.value = row
  drawerVisible.value = true
  activeDrawerTab.value = 'messages'
}

function getMessages(log: any) {
  return log?.payload?.messages || []
}

function getSystemPromptId(log: any) {
  return log?.system_prompt_id || '默认模板'
}

function getModelName(log: any) {
  return log?.payload?.model || '-'
}

function getTokenCount(log: any) {
  return log?.payload?.token_count || '-'
}

async function copyText(text: string) {
  try {
    await navigator.clipboard.writeText(text)
    ElMessage.success('复制成功')
  } catch (err) {
    ElMessage.error('复制失败，请手动选择复制')
  }
}

function copyJsonPayload() {
  if (currentLog.value?.payload) {
    copyText(JSON.stringify(currentLog.value.payload, null, 2))
  }
}

function getTruncatedContent(content: string) {
  if (!content) return ''
  return content.length > 60 ? content.slice(0, 60) + '...' : content
}

function formatDate(dateStr: string) {
  if (!dateStr) return '-'
  return new Date(dateStr).toLocaleString('zh-CN')
}

// ==========================================
// 4. 信号抽取异常复盘 (Signal Failure Extraction)
// ==========================================
const signalFailureLoading = ref(false)
const signalFailures = ref<any[]>([])
const totalSignalFailures = ref(0)

const signalFilterForm = reactive({
  supportId: '',
  stage: '',
  reason: '',
  traceId: '',
})

const signalPagination = reactive({
  page: 1,
  pageSize: 20,
})

async function fetchSignalFailures() {
  signalFailureLoading.value = true
  try {
    const offset = (signalPagination.page - 1) * signalPagination.pageSize
    const res = await apiClient.get('/v1/signal-assets/failures', {
      params: {
        support_id: signalFilterForm.supportId.trim() || undefined,
        stage: signalFilterForm.stage || undefined,
        reason: signalFilterForm.reason.trim() || undefined,
        trace_id: signalFilterForm.traceId.trim() || undefined,
        limit: signalPagination.pageSize,
        offset: offset,
      },
    })
    signalFailures.value = res.data.items || []
    totalSignalFailures.value = res.data.total || 0
  } catch (e: any) {
    console.error('加载信号抽取失败记录失败', e)
    ElMessage.error(e.response?.data?.detail || '加载信号抽取失败记录失败')
  } finally {
    signalFailureLoading.value = false
  }
}

function handleSignalSearch() {
  signalPagination.page = 1
  fetchSignalFailures()
}

function handleSignalReset() {
  signalFilterForm.supportId = ''
  signalFilterForm.stage = ''
  signalFilterForm.reason = ''
  signalFilterForm.traceId = ''
  signalPagination.page = 1
  fetchSignalFailures()
}

function handleSignalPageChange(page: number) {
  signalPagination.page = page
  fetchSignalFailures()
}

function handleSignalPageSizeChange(size: number) {
  signalPagination.pageSize = size
  signalPagination.page = 1
  fetchSignalFailures()
}

const failureDrawerVisible = ref(false)
const currentFailure = ref<any>(null)
const activeFailureDrawerTab = ref('error')

function viewFailureDetails(row: any) {
  currentFailure.value = row
  failureDrawerVisible.value = true
  activeFailureDrawerTab.value = 'error'
}

function copyFailurePayload() {
  if (currentFailure.value?.detail_payload) {
    copyText(JSON.stringify(currentFailure.value.detail_payload, null, 2))
  }
}

function stageTagType(stage: string): 'info' | 'warning' | 'danger' | 'success' {
  switch (stage) {
    case 'count':
      return 'info'
    case 'classify':
      return 'warning'
    case 'modeling':
      return 'danger'
    case 'verification':
      return 'success'
    default:
      return 'info'
  }
}

function stageLabel(stage: string): string {
  switch (stage) {
    case 'count':
      return '① 计数阶段'
    case 'classify':
      return '② 分类阶段'
    case 'modeling':
      return '③ 建模阶段'
    case 'verification':
      return '④ 验证门禁'
    default:
      return stage || '未知阶段'
  }
}

onMounted(() => {
  detectGrafana()
  detectLangfuse()
  fetchAuditLogs()
})

watch(activeTab, (tab) => {
  if (tab === 'audit') {
    fetchAuditLogs()
  } else if (tab === 'signal_failure') {
    fetchSignalFailures()
  }
})
</script>

<template>
  <div class="observability-container">
    <el-card class="observability-card">
      <el-tabs v-model="activeTab" class="observability-tabs">
      <!-- ===== Tab 1: Grafana ===== -->
      <el-tab-pane name="grafana">
        <template #label>
          <span class="tab-label">
            <el-icon><Monitor /></el-icon>
            Grafana
          </span>
        </template>
        <div v-loading="monitorLoading" class="monitor-tab-content">
          <div class="monitor-actions-bar">
            <span class="monitor-info-text">嵌入式 Grafana 实时度量面板</span>
            <el-button type="primary" size="small" @click="openGrafana">
              <el-icon style="margin-right: 4px"><Monitor /></el-icon>
              在新窗口打开 Grafana
            </el-button>
          </div>
          <div class="iframe-wrapper">
            <iframe
              v-if="grafanaReady"
              :src="grafanaUrl"
              width="100%"
              height="100%"
              frameborder="0"
              allowfullscreen
            />
          </div>
          <el-alert
            type="info"
            :closable="false"
            class="monitor-alert"
            description="如果 Grafana 监控面板加载失败，请确保本地 observability 容器组已启动，且已开启匿名访问支持。"
          />
        </div>
      </el-tab-pane>

      <!-- ===== Tab 1.5: Langfuse ===== -->
      <el-tab-pane name="langfuse">
        <template #label>
          <span class="tab-label">
            <el-icon><Cpu /></el-icon>
            Langfuse
          </span>
        </template>
        <div v-loading="langfuseLoading" class="monitor-tab-content">
          <div class="monitor-actions-bar" style="border-left-color: #67c23a;">
            <span class="monitor-info-text">嵌入式 Langfuse LLM 可观测性控制台</span>
            <el-button type="success" size="small" @click="openLangfuse">
              <el-icon style="margin-right: 4px"><Cpu /></el-icon>
              在新窗口打开 Langfuse
            </el-button>
          </div>
          <div class="iframe-wrapper">
            <iframe
              v-if="langfuseReady"
              :src="langfuseUrl"
              width="100%"
              height="100%"
              frameborder="0"
              allowfullscreen
            />
          </div>
          <el-alert
            type="info"
            :closable="false"
            class="monitor-alert"
            description="如果 Langfuse 控制台加载失败，请确保本地 observability 容器组已启动并正常运行。"
          />
        </div>
      </el-tab-pane>

      <!-- ===== Tab 2: 审查日志 ===== -->
      <el-tab-pane name="audit">
        <template #label>
          <span class="tab-label">
            <el-icon><Document /></el-icon>
            审查日志
          </span>
        </template>
        <div v-loading="auditLoading" class="audit-tab-content">
          <!-- 查询过滤器 -->
          <div class="filter-wrapper">
            <el-form :inline="true" :model="filterForm" size="default">
              <el-form-item label="工单号">
                <el-input v-model="filterForm.caseId" placeholder="精确检索 Case ID" clearable @keyup.enter="handleSearch" />
              </el-form-item>
              <el-form-item label="Trace ID">
                <el-input v-model="filterForm.traceId" placeholder="精确检索 Trace ID" clearable @keyup.enter="handleSearch" />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :icon="Search" @click="handleSearch">查询</el-button>
                <el-button :icon="Refresh" @click="handleReset">重置</el-button>
              </el-form-item>
            </el-form>
          </div>

          <!-- 主表格 -->
          <div class="table-wrapper">
            <el-table :data="auditLogs" stripe border style="width: 100%" size="default">
              <el-table-column prop="started_at" label="记录时间" width="180">
                <template #default="{ row }">
                  {{ formatDate(row.started_at) }}
                </template>
              </el-table-column>
              <el-table-column prop="case_id" label="工单号" width="160" show-overflow-tooltip />
              <el-table-column prop="conversation_id" label="会话 ID" width="320" show-overflow-tooltip />
              <el-table-column prop="turn_index" label="轮次" width="80" align="center" />
              <el-table-column label="Prompt 模板" width="130" show-overflow-tooltip>
                <template #default="{ row }">
                  <el-tag size="small" type="info">{{ getSystemPromptId(row) }}</el-tag>
                </template>
              </el-table-column>
              <el-table-column label="模型" width="160" show-overflow-tooltip>
                <template #default="{ row }">
                  {{ getModelName(row) }}
                </template>
              </el-table-column>
              <el-table-column label="估计 Token" width="110" align="right">
                <template #default="{ row }">
                  <span class="token-value">{{ getTokenCount(row) }}</span>
                </template>
              </el-table-column>
              <el-table-column prop="duration_ms" label="耗时" width="110" align="right">
                <template #default="{ row }">
                  <span v-if="row.duration_ms" class="duration-value">{{ row.duration_ms }} ms</span>
                  <span v-else class="muted">-</span>
                </template>
              </el-table-column>
              <el-table-column label="状态" width="100" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.error" type="danger" size="small" :icon="Warning">失败</el-tag>
                  <el-tag v-else type="success" size="small" :icon="CircleCheck">成功</el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="trace_id" label="Trace ID" min-width="180" show-overflow-tooltip />
              <el-table-column label="操作" width="120" fixed="right" align="center">
                <template #default="{ row }">
                  <el-button type="primary" size="small" link @click="viewLogDetails(row)">
                    查看详情
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 分页栏 -->
          <div class="pagination-wrapper">
            <el-pagination
              v-model:current-page="pagination.page"
              v-model:page-size="pagination.pageSize"
              :page-sizes="[10, 20, 50, 100]"
              layout="total, sizes, prev, pager, next, jumper"
              :total="totalLogs"
              @current-change="handlePageChange"
              @size-change="handlePageSizeChange"
            />
          </div>
        </div>
      </el-tab-pane>

      <!-- ===== Tab 3: 信号抽取 ===== -->
      <el-tab-pane name="signal_failure">
        <template #label>
          <span class="tab-label">
            <el-icon><DataAnalysis /></el-icon>
            信号抽取
          </span>
        </template>
        <div v-loading="signalFailureLoading" class="audit-tab-content">
          <!-- 查询过滤器 -->
          <div class="filter-wrapper">
            <el-form :inline="true" :model="signalFilterForm" size="default">
              <el-form-item label="案例 / KBD">
                <el-input
                  v-model="signalFilterForm.supportId"
                  placeholder="检索 Support ID 或 KBD"
                  clearable
                  @keyup.enter="handleSignalSearch"
                />
              </el-form-item>
              <el-form-item label="抽取阶段">
                <el-select
                  v-model="signalFilterForm.stage"
                  placeholder="全部阶段"
                  clearable
                  style="width: 155px"
                  @change="handleSignalSearch"
                >
                  <el-option label="① 计数阶段 (count)" value="count" />
                  <el-option label="② 分类阶段 (classify)" value="classify" />
                  <el-option label="③ 建模阶段 (modeling)" value="modeling" />
                  <el-option label="④ 验证门禁 (verification)" value="verification" />
                </el-select>
              </el-form-item>
              <el-form-item label="失败原因">
                <el-input
                  v-model="signalFilterForm.reason"
                  placeholder="如 CLOSED_VARIABLE_MISSING"
                  clearable
                  @keyup.enter="handleSignalSearch"
                />
              </el-form-item>
              <el-form-item label="Trace ID">
                <el-input
                  v-model="signalFilterForm.traceId"
                  placeholder="检索 Trace ID"
                  clearable
                  @keyup.enter="handleSignalSearch"
                />
              </el-form-item>
              <el-form-item>
                <el-button type="primary" :icon="Search" @click="handleSignalSearch">查询</el-button>
                <el-button :icon="Refresh" @click="handleSignalReset">重置</el-button>
              </el-form-item>
            </el-form>
          </div>

          <!-- 主表格 -->
          <div class="table-wrapper">
            <el-table :data="signalFailures" stripe border style="width: 100%" size="default">
              <el-table-column prop="created_at" label="记录时间" width="180">
                <template #default="{ row }">
                  {{ formatDate(row.created_at) }}
                </template>
              </el-table-column>
              <el-table-column label="案例 ID" width="120" align="center">
                <template #default="{ row }">
                  <el-tag v-if="row.support_id" size="small" type="primary" effect="plain">
                    {{ row.support_id }}
                  </el-tag>
                  <span v-else class="muted">-</span>
                </template>
              </el-table-column>
              <el-table-column prop="kbd_title" label="KBD 标题" min-width="200" show-overflow-tooltip>
                <template #default="{ row }">
                  <span>{{ row.kbd_title || `KBD #${row.kbd_id || '-'}` }}</span>
                </template>
              </el-table-column>
              <el-table-column label="抽取阶段" width="140" align="center">
                <template #default="{ row }">
                  <el-tag :type="stageTagType(row.stage)" size="small">
                    {{ stageLabel(row.stage) }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column label="失败原因" width="220" show-overflow-tooltip>
                <template #default="{ row }">
                  <el-tag type="danger" size="small" effect="light">
                    {{ row.reason }}
                  </el-tag>
                </template>
              </el-table-column>
              <el-table-column prop="raw_content_preview" label="异常原文 / 候选摘要" min-width="240" show-overflow-tooltip />
              <el-table-column prop="trace_id" label="Trace ID" width="180" show-overflow-tooltip>
                <template #default="{ row }">
                  <el-tooltip content="点击复制 Trace ID" placement="top">
                    <span class="copyable-trace" @click="copyText(row.trace_id)">
                      {{ row.trace_id }}
                    </span>
                  </el-tooltip>
                </template>
              </el-table-column>
              <el-table-column label="操作" width="110" fixed="right" align="center">
                <template #default="{ row }">
                  <el-button type="primary" size="small" link @click="viewFailureDetails(row)">
                    查看详情
                  </el-button>
                </template>
              </el-table-column>
            </el-table>
          </div>

          <!-- 分页 -->
          <div class="pagination-wrapper">
            <el-pagination
              v-model:current-page="signalPagination.page"
              v-model:page-size="signalPagination.pageSize"
              :page-sizes="[10, 20, 50, 100]"
              layout="total, sizes, prev, pager, next, jumper"
              :total="totalSignalFailures"
              @size-change="handleSignalPageSizeChange"
              @current-change="handleSignalPageChange"
            />
          </div>
        </div>
      </el-tab-pane>
    </el-tabs>
    </el-card>

    <!-- ===== 详情侧拉抽屉 ===== -->
    <el-drawer
      v-model="drawerVisible"
      title="Prompt 审计日志详情"
      size="680px"
      destroy-on-close
    >
      <div v-if="currentLog" class="drawer-detail-container">
        <!-- 基础元数据信息 -->
        <el-descriptions :column="2" border size="small" class="meta-descriptions">
          <el-descriptions-item label="工单 ID">{{ currentLog.case_id }}</el-descriptions-item>
          <el-descriptions-item label="会话 ID">{{ currentLog.conversation_id }}</el-descriptions-item>
          <el-descriptions-item label="轮次">{{ currentLog.turn_index }}</el-descriptions-item>
          <el-descriptions-item label="模板版本">{{ getSystemPromptId(currentLog) }}</el-descriptions-item>
          <el-descriptions-item label="基座模型">{{ getModelName(currentLog) }}</el-descriptions-item>
          <el-descriptions-item label="估计 Token">{{ getTokenCount(currentLog) }}</el-descriptions-item>
          <el-descriptions-item label="总耗时">{{ currentLog.duration_ms ? `${currentLog.duration_ms} ms` : '-' }}</el-descriptions-item>
          <el-descriptions-item label="Trace ID">{{ currentLog.trace_id || '-' }}</el-descriptions-item>
        </el-descriptions>

        <!-- 异常告警 -->
        <el-alert
          v-if="currentLog.error"
          type="error"
          title="执行异常错误日志"
          :description="currentLog.error"
          :closable="false"
          style="margin: 16px 0"
        />

        <!-- 内容 Tab 切换：Messages 列表 / Payload 源码 -->
        <el-tabs v-model="activeDrawerTab" style="margin-top: 16px">
          <!-- Messages 折叠列表 -->
          <el-tab-pane label="对话上下文明细" name="messages">
            <div class="messages-accordion-list">
              <div v-if="getMessages(currentLog).length === 0" class="no-messages-tip">
                暂无 Prompt 消息内容
              </div>
              <el-collapse v-else accordion>
                <el-collapse-item
                  v-for="(msg, index) in getMessages(currentLog)"
                  :key="index"
                  :name="index"
                >
                  <template #title>
                    <div class="message-item-header">
                      <el-tag
                        size="small"
                        :type="msg.role === 'system' ? 'danger' : msg.role === 'user' ? 'success' : 'primary'"
                        class="role-tag"
                      >
                        {{ msg.role.toUpperCase() }}
                      </el-tag>
                      <span class="message-preview-text">{{ getTruncatedContent(msg.content) }}</span>
                    </div>
                  </template>
                  <div class="message-item-body">
                    <div class="copy-action-row">
                      <el-button type="primary" size="small" plain :icon="CopyDocument" @click="copyText(msg.content)">
                        复制此消息
                      </el-button>
                    </div>
                    <pre class="formatted-message-text">{{ msg.content }}</pre>
                  </div>
                </el-collapse-item>
              </el-collapse>
            </div>
          </el-tab-pane>

          <!-- Payload JSON 源码 -->
          <el-tab-pane label="Payload JSON 源码" name="payload">
            <div class="payload-json-viewer">
              <div class="copy-action-row">
                <el-button type="primary" size="small" plain :icon="CopyDocument" @click="copyJsonPayload">
                  复制 Payload
                </el-button>
              </div>
              <pre class="json-code-block">{{ JSON.stringify(currentLog.payload, null, 2) }}</pre>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-drawer>

    <!-- ===== 信号抽取异常复盘详情抽屉 ===== -->
    <el-drawer
      v-model="failureDrawerVisible"
      title="信号抽取异常复盘详情"
      size="700px"
      destroy-on-close
    >
      <div v-if="currentFailure" class="drawer-detail-container">
        <!-- 基础元数据信息 -->
        <el-descriptions :column="2" border size="small" class="meta-descriptions">
          <el-descriptions-item label="案例 Support ID">
            <el-tag v-if="currentFailure.support_id" size="small" type="primary">{{ currentFailure.support_id }}</el-tag>
            <span v-else>-</span>
          </el-descriptions-item>
          <el-descriptions-item label="KBD ID">{{ currentFailure.kbd_id ? `#${currentFailure.kbd_id}` : '-' }}</el-descriptions-item>
          <el-descriptions-item label="KBD 标题" :span="2">{{ currentFailure.kbd_title || '-' }}</el-descriptions-item>
          <el-descriptions-item label="抽取阶段">
            <el-tag :type="stageTagType(currentFailure.stage)" size="small">
              {{ stageLabel(currentFailure.stage) }}
            </el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="失败原因">
            <el-tag type="danger" size="small">{{ currentFailure.reason }}</el-tag>
          </el-descriptions-item>
          <el-descriptions-item label="Trace ID" :span="2">
            <span class="copyable-trace" @click="copyText(currentFailure.trace_id)">
              {{ currentFailure.trace_id }}
            </span>
          </el-descriptions-item>
          <el-descriptions-item label="记录时间" :span="2">{{ formatDate(currentFailure.created_at) }}</el-descriptions-item>
        </el-descriptions>

        <!-- 异常提示 -->
        <el-alert
          type="error"
          :title="`异常分类：${currentFailure.reason}`"
          :description="(currentFailure.detail_payload || {}).error_message || '抽取链路在当前阶段未达成收敛门禁约束，已沉淀入库供复盘。'"
          :closable="false"
          style="margin: 16px 0"
        />

        <!-- 内容 Tab 切换 -->
        <el-tabs v-model="activeFailureDrawerTab" style="margin-top: 8px">
          <el-tab-pane label="诊断与错误详情" name="error">
            <div class="failure-error-detail">
              <div v-if="(currentFailure.detail_payload || {}).validation_issues?.length" class="detail-block">
                <div class="detail-title">门禁校验未通过项：</div>
                <ul class="issues-list">
                  <li v-for="(issue, idx) in currentFailure.detail_payload.validation_issues" :key="idx" class="issue-item">
                    {{ issue }}
                  </li>
                </ul>
              </div>
              <div v-if="(currentFailure.detail_payload || {}).feedback" class="detail-block">
                <div class="detail-title">自愈回路反馈信息：</div>
                <pre class="formatted-message-text">{{ currentFailure.detail_payload.feedback }}</pre>
              </div>
              <div v-if="!(currentFailure.detail_payload || {}).validation_issues?.length && !(currentFailure.detail_payload || {}).feedback" class="muted-block">
                <pre class="formatted-message-text">{{ JSON.stringify(currentFailure.detail_payload, null, 2) }}</pre>
              </div>
            </div>
          </el-tab-pane>

          <el-tab-pane label="异常上下文原文" name="raw">
            <div class="raw-content-wrapper">
              <div class="copy-action-row">
                <el-button type="primary" size="small" plain :icon="CopyDocument" @click="copyText(currentFailure.raw_content)">
                  复制原文
                </el-button>
              </div>
              <pre class="formatted-message-text">{{ currentFailure.raw_content || '（空文本）' }}</pre>
            </div>
          </el-tab-pane>

          <el-tab-pane label="原始 Payload JSON" name="payload">
            <div class="payload-json-viewer">
              <div class="copy-action-row">
                <el-button type="primary" size="small" plain :icon="CopyDocument" @click="copyFailurePayload">
                  复制 Payload
                </el-button>
              </div>
              <pre class="json-code-block">{{ JSON.stringify(currentFailure.detail_payload, null, 2) }}</pre>
            </div>
          </el-tab-pane>
        </el-tabs>
      </div>
    </el-drawer>
  </div>
</template>

<style scoped>
.observability-container {
  padding: 0 !important;
  margin: 0 !important;
}

.observability-card {
  margin: 0 !important;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.05);
  border-radius: 4px;
  border: 1px solid #ebeef5;
}

.observability-card :deep(.el-card__body) {
  padding: 20px;
}

.observability-tabs :deep(.el-tabs__content) {
  padding-top: 16px;
}

.tab-label {
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 14px;
  font-weight: 500;
}

.monitor-tab-content, .audit-tab-content {
  display: flex;
  flex-direction: column;
  gap: 16px;
}

/* 监控面板样式 */
.monitor-actions-bar {
  display: flex;
  justify-content: space-between;
  align-items: center;
  padding: 8px 16px;
  background-color: #f5f7fa;
  border-radius: 6px;
  border-left: 4px solid #409eff;
}

.monitor-info-text {
  font-size: 14px;
  color: #606266;
  font-weight: 500;
}

.iframe-wrapper {
  width: 100%;
  height: calc(100vh - 310px);
  min-height: 520px;
  border: 1px solid #dcdfe6;
  border-radius: 6px;
  overflow: hidden;
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.05);
}

.iframe-wrapper iframe {
  display: block;
}

.monitor-alert {
  margin-top: 4px;
}

/* 审计日志样式 */
.filter-wrapper {
  background-color: #fafafa;
  padding: 16px 16px 4px 16px;
  border-radius: 6px;
  border: 1px solid #ebeef5;
}

.table-wrapper {
  box-shadow: 0 2px 12px 0 rgba(0, 0, 0, 0.05);
  border-radius: 6px;
  overflow: hidden;
}

.token-value {
  font-family: 'Consolas', monospace;
  font-weight: 600;
  color: #333;
}

.duration-value {
  font-family: 'Consolas', monospace;
  color: #67c23a;
  font-weight: 600;
}

.pagination-wrapper {
  display: flex;
  justify-content: flex-end;
  margin-top: 8px;
}

.muted {
  color: #c0c4cc;
}

/* 抽屉内详情样式 */
.drawer-detail-container {
  display: flex;
  flex-direction: column;
  height: 100%;
}

.meta-descriptions {
  margin-bottom: 8px;
}

.message-item-header {
  display: flex;
  align-items: center;
  gap: 12px;
  width: 100%;
  padding-right: 20px;
}

.role-tag {
  min-width: 75px;
  text-align: center;
}

.message-preview-text {
  font-size: 13px;
  color: #606266;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

.message-item-body {
  position: relative;
  background-color: #fafafa;
  padding: 12px;
  border-radius: 6px;
  border: 1px solid #ebeef5;
}

.copy-action-row {
  display: flex;
  justify-content: flex-end;
  margin-bottom: 8px;
}

.formatted-message-text {
  font-family: 'Consolas', 'Monaco', monospace;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 13px;
  line-height: 1.6;
  color: #2c3e50;
  margin: 0;
  max-height: 380px;
  overflow-y: auto;
  padding: 12px;
  background: #282c34;
  color: #abb2bf;
  border-radius: 6px;
}

.payload-json-viewer {
  display: flex;
  flex-direction: column;
}

.json-code-block {
  font-family: 'Consolas', 'Monaco', monospace;
  white-space: pre-wrap;
  word-break: break-all;
  font-size: 13px;
  line-height: 1.5;
  background-color: #282c34;
  color: #abb2bf;
  padding: 16px;
  border-radius: 6px;
  margin: 0;
  max-height: 500px;
  overflow-y: auto;
}

.no-messages-tip {
  text-align: center;
  padding: 32px 0;
  color: #909399;
}

.copyable-trace {
  font-family: 'Consolas', monospace;
  font-size: 12px;
  cursor: pointer;
  color: #409eff;
  transition: color 0.15s ease;
}

.copyable-trace:hover {
  text-decoration: underline;
  color: #66b1ff;
}

.failure-error-detail {
  display: flex;
  flex-direction: column;
  gap: 12px;
}

.detail-block {
  background-color: #fafafa;
  border-radius: 6px;
  border: 1px solid #ebeef5;
  padding: 12px;
}

.detail-title {
  font-size: 13px;
  font-weight: 600;
  color: #303133;
  margin-bottom: 8px;
}

.issues-list {
  margin: 0;
  padding-left: 20px;
  color: #f56c6c;
  font-size: 13px;
  line-height: 1.6;
}

.issue-item {
  margin-bottom: 4px;
}
</style>
