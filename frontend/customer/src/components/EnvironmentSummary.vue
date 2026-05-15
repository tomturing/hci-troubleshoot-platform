<script setup lang="ts">
import { ref, computed } from 'vue'
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

// 展开/折叠状态
const expanded = ref(false)

// SSH 连接状态
const isSshConnected = computed(() => chatStore.sshConnectionState === 'connected')

// 刷新数据
async function handleRefresh() {
  if (!isSshConnected.value) return
  await chatStore.refreshEnvironmentData()
}

// 发送到助手（将环境数据摘要追加到输入框）
function handleSendToAssistant() {
  if (!chatStore.environmentContext) return

  const summary = buildEnvironmentSummary(chatStore.environmentContext)
  chatStore.setAssistantDraftText(summary)
}

// 构建环境数据摘要
function buildEnvironmentSummary(ctx: any): string {
  const lines: string[] = ['请基于以下环境数据进行分析：']

  if (ctx.env_info) {
    lines.push('\n【集群信息】')
    lines.push(`集群版本: ${ctx.env_info.hci_version || '未知'}`)
    if (ctx.env_info.cluster_name) {
      lines.push(`集群名称: ${ctx.env_info.cluster_name}`)
    }
    lines.push(`节点数量: ${ctx.env_info.host_count || '未知'}`)
  }

  if (ctx.alert_logs && ctx.alert_logs.length > 0) {
    lines.push('\n【告警列表】')
    ctx.alert_logs.slice(0, 5).forEach((alert: any) => {
      const parts = [`[${alert.level || 'WARNING'}]`]
      if (alert.time) parts.push(alert.time)
      if (alert.target) parts.push(`对象: ${alert.target}`)
      if (alert.type) parts.push(`事件: ${alert.type}`)
      if (alert.host) parts.push(`主机: ${alert.host}`)
      if (alert.vm) parts.push(`VM: ${alert.vm}`)
      if (alert.description) parts.push(`描述: ${alert.description}`)
      lines.push(parts.join(' | '))
    })
    if (ctx.alert_logs.length > 5) {
      lines.push(`... 共 ${ctx.alert_logs.length} 条告警`)
    }
  }

  if (ctx.task_logs && ctx.task_logs.length > 0) {
    lines.push('\n【任务状态】')
    ctx.task_logs.slice(0, 5).forEach((task: any) => {
      const parts = [`[${task.status || '未知'}]`]
      if (task.time) parts.push(task.time)
      if (task.type) parts.push(`行为: ${task.type}`)
      if (task.host) parts.push(`主机: ${task.host}`)
      if (task.vm) parts.push(`VM: ${task.vm}`)
      if (task.target) parts.push(`对象: ${task.target}`)
      if (task.errcode_tracing) parts.push(`错误码: ${task.errcode_tracing}`)
      if (task.trace_id) parts.push(`trace_id: ${task.trace_id}`)
      if (task.description) parts.push(`描述: ${task.description}`)
      lines.push(parts.join(' | '))
    })
    if (ctx.task_logs.length > 5) {
      lines.push(`... 共 ${ctx.task_logs.length} 条任务`)
    }
  }

  return lines.join('\n')
}

// 计算摘要信息
const summaryText = computed(() => {
  const ctx = chatStore.environmentContext
  if (!ctx) return '无数据'

  const parts: string[] = []

  if (ctx.env_info) {
    const version = ctx.env_info.hci_version && ctx.env_info.hci_version !== '未知' ? ctx.env_info.hci_version : null
    const hostCount = ctx.env_info.host_count && ctx.env_info.host_count !== '未知' ? ctx.env_info.host_count : null
    if (version || hostCount) {
      parts.push(`集群信息: ${version || '版本待采集'}, ${hostCount || '?'}节点`)
    } else {
      parts.push('集群信息: 待采集')
    }
  }

  if (ctx.alert_logs) {
    parts.push(`告警列表: ${ctx.alert_logs.length} 条活跃`)
  }

  if (ctx.task_logs) {
    // status: '失败'（来自整数 3）| '完成'（来自整数 2）
    const failed = ctx.task_logs.filter((t: any) => t.status === '失败').length
    const total = ctx.task_logs.length
    parts.push(`任务状态: ${failed} 条失败 / 共 ${total} 条`)
  }

  return parts.join(' | ')
})

// 采集状态
const collectionState = computed(() => chatStore.collectionState)
</script>

<template>
  <div class="env-summary-card" v-if="chatStore.environmentContext">
    <!-- 折叠状态 -->
    <div class="card-header" @click="expanded = !expanded">
      <div class="header-left">
        <span class="card-icon">📦</span>
        <span class="card-title">环境数据（已采集）</span>
        <el-tag v-if="collectionState === 'success'" size="small" type="success" effect="plain">✅</el-tag>
        <el-tag v-else-if="collectionState === 'collecting'" size="small" type="warning" effect="plain">采集中</el-tag>
        <el-tag v-else-if="collectionState === 'error'" size="small" type="danger" effect="plain">失败</el-tag>
      </div>
      <div class="header-right">
        <el-button text size="small" @click.stop="expanded = !expanded">
          {{ expanded ? '折叠' : '展开' }}
        </el-button>
      </div>
    </div>

    <!-- 摘要（折叠时显示） -->
    <div class="card-summary" v-if="!expanded">
      {{ summaryText }}
    </div>

    <!-- 详细内容（展开时显示） -->
    <div class="card-detail" v-else>
      <!-- 集群信息 -->
      <div class="detail-section" v-if="chatStore.environmentContext?.env_info">
        <div class="section-header">
          <span class="section-icon">📦</span>
          <span class="section-title">集群信息</span>
        </div>
        <div class="section-content">
          <!-- 仅展示有实际值（非"未知"占位）的字段 -->
          <div v-if="chatStore.environmentContext.env_info.hci_version && chatStore.environmentContext.env_info.hci_version !== '未知'">
            集群版本: {{ chatStore.environmentContext.env_info.hci_version }}
          </div>
          <div v-if="chatStore.environmentContext.env_info.cluster_name && chatStore.environmentContext.env_info.cluster_name !== '未知'">
            集群名称: {{ chatStore.environmentContext.env_info.cluster_name }}
          </div>
          <div v-if="chatStore.environmentContext.env_info.host_count && chatStore.environmentContext.env_info.host_count !== '未知'">
            节点数量: {{ chatStore.environmentContext.env_info.host_count }}
          </div>
          <!-- 所有字段均为"未知"时显示提示 -->
          <div
            v-if="(!chatStore.environmentContext.env_info.hci_version || chatStore.environmentContext.env_info.hci_version === '未知')
              && (!chatStore.environmentContext.env_info.cluster_name || chatStore.environmentContext.env_info.cluster_name === '未知')
              && (!chatStore.environmentContext.env_info.host_count || chatStore.environmentContext.env_info.host_count === '未知')"
            class="no-data-hint"
          >
            集群数据暂未采集（SSH 连接后自动采集）
          </div>
        </div>
      </div>

      <!-- 告警列表 -->
      <div class="detail-section" v-if="chatStore.environmentContext?.alert_logs?.length > 0">
        <div class="section-header">
          <span class="section-icon">🔔</span>
          <span class="section-title">告警列表 ({{ chatStore.environmentContext.alert_logs.length }} 条活跃)</span>
        </div>
        <div class="section-content">
          <div
            v-for="(alert, idx) in chatStore.environmentContext.alert_logs.slice(0, 10)"
            :key="idx"
            class="alert-item"
          >
            <el-tag :type="alert.level === 'CRITICAL' ? 'danger' : 'warning'" size="small">
              {{ alert.level === 'CRITICAL' ? '紧急' : '普通' }}
            </el-tag>
            <span class="alert-time" v-if="alert.time">{{ alert.time }}</span>
            <span v-if="alert.target" class="alert-field">对象: {{ alert.target }}</span>
            <span v-if="alert.type" class="alert-field">事件: {{ alert.type }}</span>
            <span v-if="alert.host" class="alert-field">主机: {{ alert.host }}</span>
            <span v-if="alert.vm" class="alert-field">VM: {{ alert.vm }}</span>
            <span v-if="alert.description" class="alert-message">{{ alert.description }}</span>
          </div>
          <div v-if="chatStore.environmentContext.alert_logs.length > 10" class="more-hint">
            ... 共 {{ chatStore.environmentContext.alert_logs.length }} 条告警
          </div>
        </div>
      </div>

      <!-- 任务状态 -->
      <div class="detail-section" v-if="chatStore.environmentContext?.task_logs?.length > 0">
        <div class="section-header">
          <span class="section-icon">📋</span>
          <span class="section-title">任务状态 (最近 {{ chatStore.environmentContext.task_logs.length }} 条)</span>
        </div>
        <div class="section-content">
          <div
            v-for="(task, idx) in chatStore.environmentContext.task_logs.slice(0, 10)"
            :key="idx"
            class="task-item"
          >
            <el-tag :type="task.status === '失败' ? 'danger' : 'success'" size="small">
              {{ task.status || '未知' }}
            </el-tag>
            <span class="task-time" v-if="task.time">{{ task.time }}</span>
            <span v-if="task.type" class="task-field">行为: {{ task.type }}</span>
            <span v-if="task.host" class="task-field">主机: {{ task.host }}</span>
            <span v-if="task.vm" class="task-field">VM: {{ task.vm }}</span>
            <span v-if="task.target" class="task-field">对象: {{ task.target }}</span>
            <span v-if="task.errcode_tracing" class="task-errcode">错误码: {{ task.errcode_tracing }}</span>
            <span v-if="task.trace_id" class="task-traceid">trace_id: {{ task.trace_id }}</span>
            <span v-if="task.description" class="task-desc">{{ task.description }}</span>
          </div>
          <div v-if="chatStore.environmentContext.task_logs.length > 10" class="more-hint">
            ... 共 {{ chatStore.environmentContext.task_logs.length }} 条任务
          </div>
        </div>
      </div>

      <!-- 操作按钮 -->
      <div class="card-actions">
        <el-tooltip
          :content="isSshConnected ? '重新采集环境数据' : '需要 SSH 连接才能刷新'"
          placement="top"
        >
          <el-button
            size="small"
            :disabled="!isSshConnected"
            @click="handleRefresh"
          >
            刷新数据
          </el-button>
        </el-tooltip>
        <el-button size="small" type="primary" @click="handleSendToAssistant">
          发送到助手
        </el-button>
      </div>
    </div>
  </div>

  <!-- 无数据提示 -->
  <div class="env-no-data" v-else-if="chatStore.currentCase && chatStore.hasActiveCase">
    <div class="no-data-content">
      <span class="no-data-icon">⚠️</span>
      <span class="no-data-title">未采集环境数据</span>
      <p class="no-data-desc">
        您可以：
        <br />• 手动描述环境信息
        <br />• 点击「终端」连接 SSH 后采集
      </p>
    </div>
  </div>
</template>

<style scoped>
.env-summary-card {
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  margin: 8px 0;
  overflow: hidden;
}

.card-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  padding: 12px 16px;
  background: #f5f7fa;
  cursor: pointer;
  transition: background 0.2s;
}

.card-header:hover {
  background: #e9ecf0;
}

.header-left {
  display: flex;
  align-items: center;
  gap: 8px;
}

.card-icon {
  font-size: 16px;
}

.card-title {
  font-size: 14px;
  font-weight: 500;
  color: #303133;
}

.card-summary {
  padding: 12px 16px;
  font-size: 13px;
  color: #606266;
  line-height: 1.6;
}

.card-detail {
  padding: 12px 16px;
}

.detail-section {
  margin-bottom: 16px;
}

.detail-section:last-of-type {
  margin-bottom: 12px;
}

.section-header {
  display: flex;
  align-items: center;
  gap: 6px;
  margin-bottom: 8px;
  font-size: 13px;
  font-weight: 500;
  color: #303133;
}

.section-icon {
  font-size: 14px;
}

.section-content {
  padding-left: 20px;
  font-size: 12px;
  color: #606266;
  line-height: 1.8;
}

.alert-item,
.task-item {
  display: flex;
  align-items: flex-start;
  flex-wrap: wrap;
  gap: 6px;
  margin-bottom: 6px;
}

.alert-message,
.task-desc {
  flex-basis: 100%;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  color: #909399;
  font-size: 11px;
}

.alert-field,
.task-field,
.task-errcode,
.task-traceid {
  font-size: 11px;
  color: #606266;
}

.task-errcode,
.task-traceid {
  color: #f56c6c;
}

.alert-time,
.task-time {
  font-size: 11px;
  color: #c0c4cc;
}

.more-hint {
  font-size: 11px;
  color: #909399;
  margin-top: 4px;
}

.card-actions {
  display: flex;
  justify-content: flex-end;
  gap: 8px;
  padding-top: 12px;
  border-top: 1px solid #e4e7ed;
}

/* 无数据提示 */
.env-no-data {
  background: #fff;
  border: 1px solid #e4e7ed;
  border-radius: 8px;
  margin: 8px 0;
  padding: 16px;
}

.no-data-content {
  text-align: center;
}

.no-data-icon {
  font-size: 24px;
}

.no-data-title {
  font-size: 14px;
  font-weight: 500;
  color: #303133;
  margin: 8px 0;
}

.no-data-desc {
  font-size: 12px;
  color: #909399;
  line-height: 1.8;
}

/* 集群数据暂无提示 */
.no-data-hint {
  font-size: 12px;
  color: #909399;
  font-style: italic;
}
</style>