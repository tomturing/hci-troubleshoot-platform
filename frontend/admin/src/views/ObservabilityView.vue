<script setup lang="ts">
import { ref, onMounted, reactive, watch } from 'vue'
import { Monitor, Document, Search, Refresh, CopyDocument, CircleCheck, Warning } from '@element-plus/icons-vue'
import { createApiClient } from '@hci/shared'
import { ElMessage } from 'element-plus'

const apiClient = createApiClient('/api')

// ===== 视图选项卡 =====
const activeTab = ref('monitor')

// ==========================================
// 1. 监控面板 (Grafana iframe)
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

onMounted(() => {
  detectGrafana()
  fetchAuditLogs()
})

watch(activeTab, (tab) => {
  if (tab === 'audit') {
    fetchAuditLogs()
  }
})
</script>

<template>
  <div class="observability-container">
    <el-card class="observability-card">
      <el-tabs v-model="activeTab" class="observability-tabs">
      <!-- ===== Tab 1: 监控面板 ===== -->
      <el-tab-pane name="monitor">
        <template #label>
          <span class="tab-label">
            <el-icon><Monitor /></el-icon>
            监控面板
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
            description="如果监控面板加载失败，请确保本地 observability 容器组已启动，且已开启匿名访问支持。"
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
</style>
