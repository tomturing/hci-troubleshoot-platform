<script setup lang="ts">
/**
 * 历史工单面板内容（从 ChatWindow 的抽屉里抽出）。
 *
 * 面板化之后这里只是左栏的一个页签内容，不再自带抽屉外壳：
 * 标题、收起、拖拽宽度都由 PanelShell 统一提供。
 */
import { useChatStore } from '@/stores/chat'
import MessageBubble from './MessageBubble.vue'

const chatStore = useChatStore()

/** 状态中文映射 */
function statusLabel(status: string): string {
  const map: Record<string, string> = {
    created: '待确认',
    confirmed: '已确认',
    in_progress: '处理中',
    resolved: '已解决',
    closed: '已关闭',
    cancelled: '已取消',
  }
  return map[status] || status
}

/** 状态颜色映射 */
function statusType(status: string): '' | 'success' | 'warning' | 'danger' | 'info' {
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

/** 格式化时间 */
function formatDate(d: string): string {
  return new Date(d).toLocaleString('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
  })
}

function openTerminalReplay() {
  if (chatStore.historyCase) chatStore.openTerminalReplay(chatStore.historyCase.case_id)
}

function backToList() {
  chatStore.historyCase = null
  chatStore.historyMessages = []
}
</script>

<template>
  <div class="history-container">
    <!-- 工单列表 -->
    <div v-if="!chatStore.historyCase" class="history-list">
      <div
        v-for="c in chatStore.existingCases"
        :key="c.case_id"
        class="history-item"
        @click="chatStore.loadHistoryMessages(c)"
      >
        <div class="history-item-header">
          <span class="history-case-id">{{ c.case_id }}</span>
          <el-tag :type="statusType(c.status)" size="small">{{ statusLabel(c.status) }}</el-tag>
        </div>
        <div class="history-item-title">{{ c.title }}</div>
        <div class="history-item-time">{{ formatDate(c.created_at) }}</div>
      </div>
      <div v-if="chatStore.existingCases.length === 0" class="history-empty">暂无历史工单</div>
    </div>

    <!-- 消息预览 -->
    <div v-else class="history-detail">
      <div class="history-detail-header">
        <el-button text size="small" @click="backToList">← 返回列表</el-button>
        <div class="history-detail-info">
          <strong>{{ chatStore.historyCase.case_id }}</strong>
          <span class="history-detail-title">{{ chatStore.historyCase.title }}</span>
          <el-tag :type="statusType(chatStore.historyCase.status)" size="small">
            {{ statusLabel(chatStore.historyCase.status) }}
          </el-tag>
        </div>
        <el-button size="small" type="primary" plain @click="openTerminalReplay">
          终端历史
        </el-button>
      </div>

      <div v-loading="chatStore.historyLoading" class="history-messages">
        <MessageBubble v-for="msg in chatStore.historyMessages" :key="msg.id" :message="msg" />
      </div>

      <div
        v-if="chatStore.historyCase && !['closed', 'cancelled'].includes(chatStore.historyCase.status)"
        class="history-action"
      >
        <el-button type="primary" size="small" @click="chatStore.switchToCase(chatStore.historyCase!)">
          切换到此工单
        </el-button>
      </div>
    </div>
  </div>
</template>

<style scoped>
.history-container {
  flex: 1;
  min-height: 0;
  display: flex;
  overflow: hidden;
}

.history-list {
  flex: 1;
  overflow-y: auto;
  padding: 8px;
}

.history-item {
  padding: 10px 12px;
  border-radius: 8px;
  cursor: pointer;
  transition: background-color 0.15s;
  border: 1px solid transparent;
}

.history-item:hover {
  background: #f5f7fa;
  border-color: #e4e7ed;
}

.history-item-header {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-bottom: 4px;
}

.history-case-id {
  font-family: var(--el-font-family-mono, monospace);
  font-size: 12px;
  color: #606266;
}

.history-item-title {
  font-size: 13px;
  color: #303133;
  line-height: 1.5;
  display: -webkit-box;
  -webkit-line-clamp: 2;
  -webkit-box-orient: vertical;
  overflow: hidden;
}

.history-item-time {
  margin-top: 4px;
  font-size: 12px;
  color: #909399;
}

.history-empty {
  padding: 32px 12px;
  text-align: center;
  font-size: 13px;
  color: #909399;
}

.history-detail {
  flex: 1;
  min-width: 0;
  display: flex;
  flex-direction: column;
}

.history-detail-header {
  display: flex;
  align-items: center;
  gap: 8px;
  padding: 8px 12px;
  border-bottom: 1px solid #e4e7ed;
  flex-wrap: wrap;
}

.history-detail-info {
  flex: 1;
  min-width: 0;
  display: flex;
  align-items: center;
  gap: 8px;
  font-size: 13px;
  overflow: hidden;
}

.history-detail-title {
  color: #606266;
  white-space: nowrap;
  overflow: hidden;
  text-overflow: ellipsis;
}

.history-messages {
  flex: 1;
  overflow-y: auto;
  padding: 12px;
  display: flex;
  flex-direction: column;
  gap: 10px;
  background: #f0f2f5;
}

.history-action {
  padding: 10px 12px;
  border-top: 1px solid #e4e7ed;
  display: flex;
  justify-content: flex-end;
}
</style>
