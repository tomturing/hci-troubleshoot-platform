<script setup lang="ts">
import { ref, computed } from 'vue'
import { useChatStore } from '@/stores/chat'

const chatStore = useChatStore()

// 展开/折叠状态
const expanded = ref(false)

// 刷新数据
async function handleRefresh() {
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
    lines.push(`集群版本: ${ctx.env_info.cluster_version || '未知'}`)
    lines.push(`节点数量: ${ctx.env_info.node_count || '未知'}`)
    if (ctx.env_info.storage_pools) {
      lines.push(`存储池: ${ctx.env_info.storage_pools}`)
    }
  }

  if (ctx.alert_logs && ctx.alert_logs.length > 0) {
    lines.push('\n【告警列表】')
    ctx.alert_logs.slice(0, 5).forEach((alert: any) => {
      lines.push(`[${alert.level || 'INFO'}] ${alert.message || alert.content} @${alert.timestamp || ''}`)
    })
    if (ctx.alert_logs.length > 5) {
      lines.push(`... 共 ${ctx.alert_logs.length} 条告警`)
    }
  }

  if (ctx.task_logs && ctx.task_logs.length > 0) {
    lines.push('\n【任务状态】')
    ctx.task_logs.slice(0, 5).forEach((task: any) => {
      lines.push(`[${task.status || 'UNKNOWN'}] ${task.name || task.job_id} @${task.start_time || ''}`)
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
    parts.push(`集群信息: ${ctx.env_info.cluster_version || '未知'}, ${ctx.env_info.node_count || '未知'}节点`)
  }

  if (ctx.alert_logs) {
    parts.push(`告警列表: ${ctx.alert_logs.length} 条活跃`)
  }

  if (ctx.task_logs) {
    const failed = ctx.task_logs.filter((t: any) => t.status === 'FAILED').length
    const running = ctx.task_logs.filter((t: any) => t.status === 'RUNNING').length
    parts.push(`任务状态: ${failed} 条失败 / ${running} 条运行中`)
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
          <div v-if="chatStore.environmentContext.env_info.cluster_version">
            集群版本: {{ chatStore.environmentContext.env_info.cluster_version }}
          </div>
          <div v-if="chatStore.environmentContext.env_info.node_count">
            节点数量: {{ chatStore.environmentContext.env_info.node_count }}
          </div>
          <div v-if="chatStore.environmentContext.env_info.storage_pools">
            存储池: {{ chatStore.environmentContext.env_info.storage_pools }}
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
            <el-tag :type="alert.level === 'CRITICAL' ? 'danger' : alert.level === 'WARNING' ? 'warning' : 'info'" size="small">
              {{ alert.level || 'INFO' }}
            </el-tag>
            <span class="alert-message">{{ alert.message || alert.content }}</span>
            <span class="alert-time" v-if="alert.timestamp">@{{ alert.timestamp }}</span>
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
            <el-tag :type="task.status === 'FAILED' ? 'danger' : task.status === 'RUNNING' ? 'warning' : 'success'" size="small">
              {{ task.status || 'UNKNOWN' }}
            </el-tag>
            <span class="task-name">{{ task.name || task.job_id }}</span>
            <span class="task-time" v-if="task.start_time">@{{ task.start_time }}</span>
          </div>
          <div v-if="chatStore.environmentContext.task_logs.length > 10" class="more-hint">
            ... 共 {{ chatStore.environmentContext.task_logs.length }} 条任务
          </div>
        </div>
      </div>

      <!-- 操作按钮 -->
      <div class="card-actions">
        <el-button size="small" @click="handleRefresh">
          刷新数据
        </el-button>
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
  align-items: center;
  gap: 8px;
  margin-bottom: 4px;
}

.alert-message,
.task-name {
  flex: 1;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
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
</style>